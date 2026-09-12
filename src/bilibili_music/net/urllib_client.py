"""基于标准库 ``urllib`` 的网络后端(对照实现)。

存在的意义有三点:

1. **不依赖 Qt**,可以在没有事件循环的环境里跑集成测试
2. 作为 ``QNetworkAccessManager`` 出问题时的**退路**,两个后端接口完全一致
3. 作为**可读的参照** —— 同步阻塞的写法比信号驱动的写法更容易一眼看懂"发了什么"

代价:所有等待都跑在线程池里。这也是它作为"对照实现"而非默认后端的原因
—— Qt 原生异步不需要任何线程。
"""

from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlencode

from ..core.errors import NetworkError
from ..core.headers import (
    DEFAULT_UA,
    HOME,
    api_headers,
    decompress_all,
    media_headers,
)
from ..core.http import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_TIMEOUT,
    RETRYABLE_STATUS,
)
from .base import (
    DownloadHandle,
    ErrorCallback,
    FetchHandle,
    ProgressCallback,
    RateLimiter,
    SuccessCallback,
    backoff_delay,
    check_payload,
)

#: 单次同步请求返回的最大字节数,防止异常响应把内存吃满(20 MiB 足够所有 JSON 接口)。
#: 注意:这个上限只作用于 :meth:`UrllibClient._fetch` 那条"先攒后解压"的路径,
#: 流式下载不受它约束 —— 音频是边收边写盘的,不吃内存。
MAX_JSON_BYTES = 20 * 1024 * 1024


class _Cancelled(Exception):
    """内部信号:调用方请求取消。

    刻意不并进 :class:`BiliMusicError` 体系:它不是"失败",而是"这次任务作废了",
    沿调用栈穿透时应当被静默吞掉(见 :meth:`UrllibClient._open` 与 ``job()`` 的捕获),
    而 :class:`BiliMusicError` 会被转成 ``on_error`` 交给界面弹窗。
    """


class UrllibClient:
    """``urllib`` 后端。接口与 :class:`~bilibili_music.net.client.QtNetworkClient` 一致。

    所有回调都在工作线程里被调用,所以回调实现**不得直接修改界面**,应通过
    信号转发到主线程。这一点在 Qt 后端里是反过来的(回调本来就在主线程),
    使用方按"回调可能不在主线程"来写才安全。
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_UA,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_workers: int = 4,
    ) -> None:
        """建好 Cookie jar、opener 与线程池。

        Args:
            user_agent: 覆盖默认浏览器 UA。
            timeout: 单次同步请求超时(秒)。
            max_retries: 含首次请求在内的最大尝试次数。
            min_interval: 请求间最小间隔(秒),见 :class:`RateLimiter`。
            max_workers: 线程池大小。取 4 是因为界面上真正并发的请求极少
                (搜索、详情、音轨、下载各一条),再多只是徒增内存与调度开销。
        """
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = RateLimiter(min_interval)
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._api_headers = api_headers(user_agent=user_agent)
        self._media_headers = media_headers(user_agent=user_agent)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bili-http")
        self._warmed_up = False
        self._closed = False
        # 只用于保护 _warmed_up / _closed 这两个跨线程读写的小标志,
        # 不是线程池,也不用来同步请求 —— 加锁范围务必保持极小。
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 会话管理

    def warm_up(self, *, force: bool = False) -> None:
        """预热会话:先访问主页拿到 ``buvid3`` / ``b_nut``。

        没有 Cookie 时连续请求很容易被判成脚本并返回 412;带上主页下发的
        buvid 后同样的请求可以稳定通过。这是实测结论,不是猜测。

        Args:
            force: 为真时即使已预热过也重新访问主页换一份新鲜 Cookie。
        """
        with self._lock:
            if self._warmed_up and not force:
                return
        request = urllib.request.Request(HOME, headers={"User-Agent": self._api_headers["User-Agent"]})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                response.read(4096)
            with self._lock:
                self._warmed_up = True
        except Exception:
            # 预热失败不致命,正式请求仍会尝试
            pass

    @property
    def cookie_names(self) -> list[str]:
        """当前持有的 Cookie 名(诊断用,不含取值以免泄露凭据)。

        必须是属性而非方法 —— 与 ``QtNetworkClient.cookie_names`` 保持一致,
        否则调用方拿到的会是一个恒为真的绑定方法对象,诊断逻辑会"假通过"。
        """
        return sorted({c.name for c in self._cookie_jar})

    # ------------------------------------------------------------ 同步核心

    def _wait_for_slot(self) -> None:
        """限速等待。"""
        delay = self._limiter.wait_time()
        if delay > 0:
            time.sleep(delay)

    def _open(self, request: urllib.request.Request):
        """带退避重试的请求。命中风控会重新预热再试。

        Args:
            request: 已构造好的同步请求。

        Returns:
            已打开的响应对象,**由调用方负责关闭**(``with`` 语法)。

        Raises:
            _Cancelled: 客户端已 close,请求作废。
            NetworkError: 重试耗尽,或状态码不可重试(如 403/404)。
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            if self._closed:
                raise _Cancelled()
            self._wait_for_slot()
            try:
                response = self._opener.open(request, timeout=self.timeout)
                self._limiter.touch()
                return response
            except urllib.error.HTTPError as exc:
                self._limiter.touch()
                if exc.code not in RETRYABLE_STATUS and exc.code < 500:
                    raise NetworkError(f"HTTP {exc.code} {request.full_url}") from exc
                last_error = exc
                # 被风控了,换一份新鲜 Cookie 再试
                self.warm_up(force=True)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                self._limiter.touch()
                last_error = exc
            if attempt < self.max_retries - 1:
                time.sleep(backoff_delay(attempt))
        raise NetworkError(f"请求失败(已重试 {self.max_retries} 次): {last_error}")

    def _fetch(self, url: str, headers: dict[str, str]) -> bytes:
        """同步 GET 并返回解压后的字节。

        先把响应体攒齐再一次性解压 —— 这里走的是 JSON 路径,响应本来就小,
        攒齐才能用 :func:`decompress_all` 少维护一份解压器状态。

        Args:
            url: 完整请求地址。
            headers: 请求头(已含 UA 与 Referer)。

        Returns:
            解压后的响应体字节。

        Raises:
            _Cancelled: 客户端已 close。
            NetworkError: 请求失败、响应过大或解压失败。
        """
        request = urllib.request.Request(url, headers=headers)
        with self._open(request) as response:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    raise NetworkError(f"响应过大(超过 {MAX_JSON_BYTES} 字节): {url}")
            payload = b"".join(chunks)
            return decompress_all(payload, response.headers.get("Content-Encoding", ""))

    def _download_to_sink(self, url: str, sink: Any, on_progress: ProgressCallback | None) -> None:
        """同步流式下载并写入已打开的 sink。

        边收边写盘,不设总大小上限:音乐区的合集分P可以很长,
        "先攒进内存再落盘"会在长音频上把内存吃满。

        Args:
            url: 音频 CDN 地址(走 media 头,不带 ``Origin``)。
            sink: 已 ``open()`` 的 :class:`~bilibili_music.core.cache.DownloadSink`。
            on_progress: ``(已收字节数, 总字节数)`` 回调;CDN 未给
                ``Content-Length`` 时第二个参数为 0。

        Raises:
            _Cancelled: 客户端已 close。
            NetworkError: 请求失败。
            BiliMusicError: 收完得到 0 字节(由 ``sink.commit()`` 抛出)。
        """
        request = urllib.request.Request(url, headers=self._media_headers)
        with self._open(request) as response:
            raw_length = response.headers.get("Content-Length")
            total = int(raw_length) if raw_length and raw_length.isdigit() else 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                sink.write(chunk)
                if on_progress:
                    on_progress(sink.bytes_written, total)
        sink.commit()

    # ------------------------------------------------------------ 异步接口

    def _submit(self, fn: Callable[[], None], *args) -> "_FutureHandle":
        """把同步任务丢进线程池,并返回统一形状的句柄。

        ``runner`` 会把异常全部吞掉:任务自己负责通过 ``on_error`` 回报失败,
        漏出来的异常如果继续往上抛,只会被线程池记录成一条无人查看的日志。

        Args:
            fn: 无参的同步任务。
            *args: 透传参数(当前调用方均不传,保留以对齐接口)。

        Returns:
            可取消的 :class:`_FutureHandle`;若客户端已 close,直接返回一个
            已标记取消的空句柄(任务根本不会入队)。
        """
        handle = _FutureHandle()
        if self._closed:
            handle._cancelled = True
            return handle

        def runner() -> None:
            """线程池入口:吞掉所有异常,失败由任务内部的回调负责上报。"""
            try:
                fn()
            except _Cancelled:
                pass
            except BaseException:  # 交给回调,避免线程池吞掉异常
                pass

        handle._future = self._pool.submit(runner)
        return handle

    def get_json(
        self,
        url: str,
        *,
        on_success: Callable[[dict[str, Any]], None],
        on_error: ErrorCallback,
    ) -> FetchHandle:
        """GET 一个 JSON 接口,结果通过回调送回。

        Returns:
            可取消的句柄;任务被取消时**两个回调都不会触发**。
        """
        self.warm_up()

        def job() -> None:
            """线程池任务:取数据、校验 ``code``、回调。"""
            try:
                payload = self._fetch(url, self._api_headers)
                on_success(check_payload(payload, url))
            except _Cancelled:
                raise
            except Exception as exc:
                on_error(exc)

        return self._submit(job)

    def get_bytes(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: ErrorCallback,
        headers: dict[str, str] | None = None,
    ) -> FetchHandle:
        """GET 原始字节(封面、字幕等)。

        与 :meth:`get_json` 一样**不做重试**:封面失败就让它失败,尽快显示占位图。
        也刻意没有 ``warm_up()`` —— 这条路径目前只用于静态资源,
        为一张封面去预热会话不划算。

        Args:
            headers: 额外覆盖的请求头,会合并进默认 API 头(同键以它为准)。
        """
        def job() -> None:
            """线程池任务:合并请求头、取字节、回调。"""
            try:
                merged = dict(self._api_headers)
                if headers:
                    merged.update(headers)
                on_success(self._fetch(url, merged))
            except _Cancelled:
                raise
            except Exception as exc:
                on_error(exc)

        return self._submit(job)

    def download(
        self,
        url: str,
        *,
        sink: Any,
        on_success: SuccessCallback,
        on_error: ErrorCallback,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadHandle:
        """异步下载。``sink`` 由本方法负责打开与收尾。

        取消时只清理 ``.part`` 文件,**不触发任何回调**(与 Qt 后端一致)。
        """
        def job() -> None:
            """线程池任务:流式下载并落盘,失败务必 abort。"""
            try:
                with sink:
                    self._download_to_sink(url, sink, on_progress)
                on_success(sink.final_path)
            except _Cancelled:
                sink.abort()
            except Exception as exc:
                sink.abort()
                on_error(exc)

        return self._submit(job)

    def close(self) -> None:
        """停止接受新请求,并取消线程池里尚未开始的任务。

        ``wait=False`` 是有意的:已经在跑的下载不该把退出流程拖住,
        进程结束时未完成的任务会随之下线(它们的 ``.part`` 由 :meth:`AudioCache.clear` 兜底清理)。
        """
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


class _FutureHandle:
    """``concurrent.futures.Future`` 的薄包装,统一成 ``cancel()`` 接口。"""

    def __init__(self) -> None:
        """建一个尚未绑定 Future 的空句柄(可安全取消)。"""
        self._future = None
        self._cancelled = False

    @property
    def bytes_written(self) -> int:
        """已写字节数,恒为 0。

        同步路径的进度由 ``on_progress`` 回调给出,不通过句柄暴露;
        这里返回 0 只是为了让两个后端的句柄形状一致。
        """
        return 0

    def cancel(self) -> None:
        """标记取消并尽力取消未开始的 Future(已在跑的任务无法中断)。"""
        self._cancelled = True
        if self._future is not None:
            self._future.cancel()


__all__ = ["MAX_JSON_BYTES", "UrllibClient"]
