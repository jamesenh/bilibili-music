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
from http.cookiejar import Cookie, CookieJar
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
from ..core.logging_setup import get_logger
from .base import (
    BILI_COOKIE_DOMAIN,
    DownloadHandle,
    ErrorCallback,
    FetchHandle,
    LOG_BODY_PREVIEW_BYTES,
    ProgressCallback,
    RateLimiter,
    RequestLogger,
    SuccessCallback,
    backoff_delay,
    check_payload,
    is_blank_cookie,
)

#: 本模块的日志器。命名空间由 ``core.logging_setup`` 统一决定。
_LOGGER = get_logger(__name__)

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
        #: 请求日志。两种后端共用 ``net.base.RequestLogger``,
        #: 所以“两边日志字段一致”由构造保证而不是靠人盯(见 ``tests/test_backend_contract.py``)。
        self._log = RequestLogger(_LOGGER, backend="urllib")
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

    def session_cookies(self) -> dict[str, str]:
        """导出 B站 作用域下的会话 Cookie(名字→取值)。

        只取 B站 域的 Cookie,理由与 Qt 后端一致(见
        :meth:`~bilibili_music.net.client.QtNetworkClient.session_cookies`):
        混进别的站点凭据既无意义又多一份泄露面。

        Returns:
            名字到取值的映射;没有可用 Cookie 时是空字典。

        Note:
            返回值含凭据取值,禁止打印或写日志(``AGENTS.md`` 第 5 节第 10 条)。
        """
        exported: dict[str, str] = {}
        for cookie in self._cookie_jar:
            if not (cookie.domain or "").endswith("bilibili.com"):
                continue
            if not is_blank_cookie(cookie.name, cookie.value or ""):
                exported[cookie.name] = cookie.value
        return exported

    def clear_session_cookies(self) -> None:
        """清掉 jar 里所有 B站 域的 Cookie(登出用)。

        用 ``CookieJar.clear(domain, path, name)`` 逐个删而不是 ``clear()`` 一把清空:
        两者当前效果相同(本客户端只会碰 B站),但按域删写明了意图,
        万一以后为别的东西复用了同一个 jar,也不会顺手把别人的凭据删掉。

        Note:
            与 :meth:`set_session_cookies` 一样,只应在**没有请求在飞**时调用。
        """
        for cookie in list(self._cookie_jar):
            if (cookie.domain or "").endswith("bilibili.com"):
                self._cookie_jar.clear(cookie.domain, cookie.path, cookie.name)

    def set_session_cookies(self, cookies: dict[str, str]) -> None:
        """把一份会话 Cookie 灌进 jar(合并,不覆盖匿名预热拿到的 buvid)。

        ``http.cookiejar.Cookie`` 的字段比 Qt 那边啰嗦,但同样是"域固定
        ``.bilibili.com`` / 路径 ``/`` / ``secure``",两边必须成对,否则
        "Qt 写进去、urllib 读出来"会静默丢凭据。

        Args:
            cookies: 名字到取值的映射;空字典是合法的空操作。

        Note:
            ``CookieJar`` 不是线程安全的。本方法只应在**还没有请求在飞**的时候调用
            (应用里就是启动恢复会话、登录成功这两处),不要在回调里并发调用。
        """
        for name, value in cookies.items():
            if is_blank_cookie(name, value):
                continue
            self._cookie_jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=BILI_COOKIE_DOMAIN,
                    domain_specified=True,
                    domain_initial_dot=True,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )

    # ------------------------------------------------------------ 同步核心

    def _wait_for_slot(self) -> None:
        """限速等待。"""
        delay = self._limiter.wait_time()
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
        """尽力读一点出错响应的正文,只为日志留证。

        ``HTTPError`` 本身就是一个可读的响应对象。读之前不知道里面是 412 的 HTML 还是
        一行 JSON,两种都是风控复盘的关键证据。读失败(连接已断、流不可读)只返回空字节串
        —— 这里是日志路径,不能因为它再抛一个异常出来。

        Args:
            exc: ``urllib`` 抛出的 HTTP 错误。

        Returns:
            响应体前 :data:`~bilibili_music.net.base.LOG_BODY_PREVIEW_BYTES` 字节;
            读不出来时是空字节串。
        """
        try:
            return exc.read(LOG_BODY_PREVIEW_BYTES) or b""
        except Exception:  # noqa: BLE001 - 取证失败不能拖垮控制流
            return b""
        finally:
            # 无论如何都要关掉:现在不关的话,重试与新请求会一直堆着这些半死的连接
            exc.close()

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
        url = request.full_url
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
                body = self._read_error_body(exc)
                if exc.code not in RETRYABLE_STATUS and exc.code < 500:
                    # 不可重试的失败(403/404 等):证据就在手上,就地记 ERROR
                    error = NetworkError(f"HTTP {exc.code} {url}")
                    self._log.failure(
                        url, status=exc.code, exc=error, body=body, headers=request.headers,
                        note="不可重试",
                    )
                    raise error from exc
                last_error = exc
                if attempt >= self.max_retries - 1:
                    # 重试耗尽:这一次的正文才是真实现场,记在这里
                    error = NetworkError(
                        f"请求失败(已重试 {self.max_retries} 次): {last_error}"
                    )
                    self._log.failure(
                        url, status=exc.code, exc=error, body=body, headers=request.headers,
                        note="重试耗尽",
                    )
                    raise error from exc
                wait_s = backoff_delay(attempt)
                self._log.retry(
                    url, attempt=attempt + 1, total=self.max_retries, wait_s=wait_s, status=exc.code
                )
                # 被风控了,换一份新鲜 Cookie 再试
                self.warm_up(force=True)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                self._limiter.touch()
                last_error = exc
                if attempt >= self.max_retries - 1:
                    error = NetworkError(
                        f"请求失败(已重试 {self.max_retries} 次): {last_error}"
                    )
                    self._log.failure(url, exc=error, headers=request.headers, note="重试耗尽")
                    raise error from exc
                self._log.retry(
                    url,
                    attempt=attempt + 1,
                    total=self.max_retries,
                    wait_s=backoff_delay(attempt),
                    exc=exc,
                )
            if attempt < self.max_retries - 1:
                time.sleep(backoff_delay(attempt))
        # 理论上到不了这里(循环内已经覆盖了所有分支),兜底留给类型检查器
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
        self._log.request(url, headers=headers, cookie_names=self.cookie_names)
        started = time.monotonic()
        with self._open(request) as response:
            status = int(getattr(response, "status", 0) or 0)
            encoding = response.headers.get("Content-Encoding", "")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    error = NetworkError(f"响应过大(超过 {MAX_JSON_BYTES} 字节): {url}")
                    self._log.failure(url, status=status, exc=error, note="JSON 接口")
                    raise error
            payload = b"".join(chunks)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log.response(
                url, status=status, length=total, elapsed_ms=elapsed_ms, encoding=encoding
            )
            try:
                return decompress_all(payload, encoding)
            except NetworkError as exc:
                # 解压失败是“数据坏了”而不是“请求失败”,正文就是现场证据,一并记下
                self._log.failure(
                    url, status=status, exc=exc, body=payload, note="解压失败"
                )
                raise

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
        self._log.request(url, headers=self._media_headers, cookie_names=self.cookie_names)
        started = time.monotonic()
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
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log.response(
                url,
                status=int(getattr(response, "status", 0) or 0),
                length=sink.bytes_written,
                elapsed_ms=elapsed_ms,
            )
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
            """线程池任务:取数据、校验 ``code``、回调。

            失败分两段记日志,分界是"证据在谁手里":

            * 传输层失败(``NetworkError``)的证据是状态码、响应体、请求头,只有
              :meth:`_open` / :meth:`_fetch` 拿得到,所以那条 ``ERROR`` 由它们就地记;
              这里再记一遍只会让一次失败出现两条 ``ERROR``。
            * 其余失败(业务 ``code`` 非 0、不是合法 JSON)的现场是响应体本身,正好在
              这里手上,所以由这里记。
            """
            try:
                payload = self._fetch(url, self._api_headers)
            except _Cancelled:
                raise
            except NetworkError as exc:
                # 证据已由下层记过(状态码/响应体/请求头只有那里拿得到),
                # 这里只负责把失败送回去 —— 一次失败只允许有一条 ERROR
                on_error(exc)
                return
            except Exception as exc:  # noqa: BLE001 - 回调前必须兜住所有失败
                self._log.failure(url, exc=exc, note="JSON 接口")
                on_error(exc)
                return
            try:
                on_success(check_payload(payload, url))
            except Exception as exc:  # noqa: BLE001 - 同上
                self._log.failure(url, exc=exc, body=payload, note="JSON 接口")
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
            """线程池任务:合并请求头、取字节、回调。

            **成功不记 ``DEBUG``**:这条路径只有封面在用,滚一次列表就是几十张图,
            逐张记会把网络日志冲掉。失败仍然记 ``ERROR`` —— "某张封面拿不到"是
            需要能回答的问题。
            """
            try:
                merged = dict(self._api_headers)
                if headers:
                    merged.update(headers)
                on_success(self._fetch(url, merged))
            except _Cancelled:
                raise
            except NetworkError as exc:
                # 传输层失败的 ERROR 已由 _fetch / _open 记过(带状态码与正文),
                # 这里不再重复记一条
                on_error(exc)
                return
            except Exception as exc:  # noqa: BLE001 - 回调前必须兜住所有失败
                self._log.failure(url, exc=exc, note="原始字节")
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
            except NetworkError as exc:
                # 同上:传输层失败不在这里重记
                sink.abort()
                on_error(exc)
            except Exception as exc:  # noqa: BLE001 - 回调前必须兜住所有失败
                sink.abort()
                self._log.failure(url, exc=exc, note="下载音轨(已清理 .part)")
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
