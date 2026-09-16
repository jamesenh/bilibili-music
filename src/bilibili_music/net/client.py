"""基于 ``QNetworkAccessManager`` 的网络后端(默认实现)。

与 urllib 后端的根本差别:**完全事件驱动,零线程**。

* 请求通过信号回调,不阻塞事件循环,界面在任何时刻都保持响应
* 不需要 ``QThread`` —— 也就不存在跨线程信号转发和线程亲和性问题
* Cookie 由 ``QNetworkCookieJar`` 自动管理
* 限速通过延迟派发实现(不用 sleep 阻塞线程)

三个必须注意的点(都是实测结论):

1. **Qt 不会自动解压 gzip**。即使声明了 ``Accept-Encoding``,``readAll()`` 拿到
   的仍是压缩字节。解压逻辑复用 :mod:`..core.headers`。
2. **QNAM 非线程安全**。实测在子线程复用主线程的 manager 会打印
   "Cannot create children for a parent that is in a different thread",
   且可能随机失败。所以本类必须只在创建它的线程里使用。
3. **412 要重新预热再重试**,和 urllib 后端策略保持一致。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkCookie,
    QNetworkCookieJar,
    QNetworkReply,
    QNetworkRequest,
)

from ..core.errors import NetworkError
from ..core.headers import (
    DEFAULT_UA,
    HOME,
    IncrementalDecoder,
    api_headers,
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
    JsonCallback,
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

#: ``QNetworkRequest`` 上取 HTTP 状态码 / 原因短语的两个属性。
#: ``_REASON`` 只为错误消息更可读,取值本身不参与任何判断。
_STATUS = QNetworkRequest.Attribute.HttpStatusCodeAttribute
_REASON = QNetworkRequest.Attribute.HttpReasonPhraseAttribute

#: 预热未完成时的轮询间隔(毫秒)。
#:
#: 取 25ms 是"足够快"与"不空转"之间的折中:预热通常几百毫秒内完成,
#: 25ms 的粒度让业务请求几乎感觉不到排队;再小就纯属浪费事件循环。
_WARMUP_POLL_MS = 25


class ReplyHandle:
    """``QNetworkReply`` 的句柄,统一成 ``cancel()`` 接口。

    取消后 ``finished`` 仍会触发,所以必须靠 ``cancelled`` 标志让回调静默退出。
    """

    __slots__ = ("reply", "cancelled", "bytes_written")

    def __init__(self, reply: QNetworkReply | None = None) -> None:
        """初始化句柄。

        Args:
            reply: 已发出的请求;下载路径会立刻传进来,请求路径则先造空句柄,
                等 :meth:`QtNetworkClient._start_json` 真正派发时再回填 ——
                这样"排队等待限速"期间调用方也能拿到句柄并取消。
        """
        self.reply = reply
        self.cancelled = False
        self.bytes_written = 0

    def cancel(self) -> None:
        """标记取消并中止在途的 HTTP 请求(可重复调用)。"""
        self.cancelled = True
        if self.reply is not None and not self.reply.isFinished():
            self.reply.abort()


class QtNetworkClient(QObject):
    """``QNetworkAccessManager`` 后端。

    必须在有事件循环的线程里创建(通常是主线程)。所有回调都在主线程被调用,
    因此可以直接更新界面 —— 这是相对 urllib 后端最实际的便利。
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_UA,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        parent: QObject | None = None,
    ) -> None:
        """创建 manager 与 Cookie jar,并预置两套请求头。

        Args:
            user_agent: 覆盖默认浏览器 UA,仅在排查"UA 是否被针对"时才传。
            timeout: 单次请求超时(秒),由 ``setTransferTimeout`` 生效。
            max_retries: 含首次请求在内的最大尝试次数。
            min_interval: 请求间最小间隔(秒),见 :class:`RateLimiter`。
            parent: Qt 父对象,交给 Qt 管生命周期。
        """
        super().__init__(parent)
        self.timeout_ms = int(timeout * 1000)
        self.max_retries = max_retries
        self._limiter = RateLimiter(min_interval)
        #: 请求日志。两种后端共用 ``net.base.RequestLogger``,
        #: 所以“两边日志字段一致”由构造保证而不是靠人盯(见 ``tests/test_backend_contract.py``)。
        self._log = RequestLogger(_LOGGER, backend="qt")
        self._api_headers = api_headers(user_agent=user_agent)
        self._media_headers = media_headers(user_agent=user_agent)
        self._warmed_up = False
        # 预热是否仍在飞行中。为真时业务请求必须排队等待,否则会赶在带 Cookie 的
        # 主页响应之前发出 —— 而实测没有 buvid Cookie 的请求很容易被风控 412。
        self._warmup_pending = False
        self._close_after_warmup = False
        self._closed = False

        self._manager = QNetworkAccessManager(self)
        self._manager.setCookieJar(QNetworkCookieJar(self._manager))

    # ------------------------------------------------------------ 会话管理

    @property
    def cookie_names(self) -> list[str]:
        """当前持有的 Cookie 名(诊断用,不含取值以免泄露凭据)。"""
        jar = self._manager.cookieJar()
        if jar is None:
            return []
        cookies = jar.cookiesForUrl(QUrl(HOME))
        return sorted({bytes(c.name()).decode("utf-8", "replace") for c in cookies})

    def session_cookies(self) -> dict[str, str]:
        """导出 B站 作用域下的会话 Cookie(名字→取值)。

        只取 B站 域的 Cookie:把别的站点凭据混进会话文件既无意义、又多一份泄露面。
        刻意不带 expires —— 过期由服务端判定,见 ``core/session.py`` 的说明。

        Returns:
            名字到取值的映射;没有 jar 或没有 Cookie 时是空字典。

        Note:
            返回值含凭据取值,禁止打印或写日志(``AGENTS.md`` 第 5 节第 10 条)。
        """
        jar = self._manager.cookieJar()
        if jar is None:
            return {}
        exported: dict[str, str] = {}
        for cookie in jar.allCookies():
            domain = cookie.domain() or ""
            if not domain.endswith("bilibili.com"):
                continue
            name = bytes(cookie.name()).decode("utf-8", "replace")
            value = bytes(cookie.value()).decode("utf-8", "replace")
            if not is_blank_cookie(name, value):
                exported[name] = value
        return exported

    def set_session_cookies(self, cookies: dict[str, str]) -> None:
        """把一份会话 Cookie 灌进 jar(合并,不覆盖匿名预热拿到的 buvid)。

        域固定 ``.bilibili.com`` / 路径 ``/`` / ``secure``:与浏览器对这些字段的下发
        方式一致,也正是 :meth:`session_cookies` 的过滤口径,两边必须成对。

        Args:
            cookies: 名字到取值的映射;空字典是合法的空操作。
        """
        jar = self._manager.cookieJar()
        if jar is None or not cookies:
            return
        items: list[QNetworkCookie] = []
        for name, value in cookies.items():
            if is_blank_cookie(name, value):
                continue
            cookie = QNetworkCookie(name.encode("utf-8"), value.encode("utf-8"))
            cookie.setDomain(BILI_COOKIE_DOMAIN)
            cookie.setPath("/")
            cookie.setSecure(True)
            items.append(cookie)
        if items:
            # 用 HOME 作为"来源 URL":jar 只接受域与它匹配的 Cookie,而 HOME 正是预热
            # 用的那个来源,少引入一个变量
            jar.setCookiesFromUrl(items, QUrl(HOME))

    def clear_session_cookies(self) -> None:
        """清掉 jar 里所有 B站 域的 Cookie(登出用)。

        ``QNetworkCookieJar.deleteCookie`` 只删"域 + 路径 + 名字"完全一致的那一条,
        所以逐个遍历再删,而不是指望有什么批量接口。
        """
        jar = self._manager.cookieJar()
        if jar is None:
            return
        for cookie in jar.allCookies():
            if (cookie.domain() or "").endswith("bilibili.com"):
                jar.deleteCookie(cookie)

    def warm_up(self, *, force: bool = False) -> None:
        """预热会话:访问主页,让 ``QNetworkCookieJar`` 收下 ``buvid3`` / ``b_nut``。

        这是异步的,所以 **业务请求必须等它完成**。本方法会把
        ``_warmup_pending`` 置真,期间 :meth:`_schedule` 只排队不派发;
        主页响应到达后才放行。这是实测结论的直接落地:没有 buvid Cookie 时
        连续请求很容易被判成脚本并返回 412。

        Args:
            force: 为真时即使已预热过也重新访问主页换一份新鲜 Cookie,
                命中 412/429 准备重试时就该这么做。
        """
        if self._warmup_pending:
            return
        if self._warmed_up and not force:
            return
        if self._closed:
            return
        self._warmed_up = True
        self._warmup_pending = True
        reply = self._manager.get(self._make_request(HOME, self._api_headers))

        def done() -> None:
            """主页响应到达:收工,放行排队的业务请求。"""
            reply.readAll()  # 丢弃响应体,目的只是收 Cookie
            reply.deleteLater()
            self._warmup_pending = False
            if self._close_after_warmup:
                self._closed = True

        reply.finished.connect(done)

    # ------------------------------------------------------------ 构造请求

    def _make_request(self, url: str, headers: dict[str, str]) -> QNetworkRequest:
        """按给定请求头构造 ``QNetworkRequest``。

        请求头用 ``setRawHeader`` 而不是 ``setHeader``:后者的已知头枚举覆盖不全,
        且这里要的就是"浏览器发什么我发什么"。

        Raises:
            UnicodeEncodeError: 请求头含非 latin-1 字符。HTTP 头本身不允许
                这类字符,在构造阶段就炸掉比发出去被服务端拒更好定位。
        """
        request = QNetworkRequest(QUrl(url))
        for name, value in headers.items():
            request.setRawHeader(name.encode("latin-1"), value.encode("latin-1"))
        request.setTransferTimeout(self.timeout_ms)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        return request

    @staticmethod
    def _status_of(reply: QNetworkReply) -> int:
        """读 HTTP 状态码;属性缺失(如连接根本没建立)时返回 0。"""
        value = reply.attribute(_STATUS)
        return int(value) if value is not None else 0

    def _describe(self, reply: QNetworkReply) -> str:
        """拼一句可直接放进错误消息的"状态码 + 原因 + URL"。"""
        status = self._status_of(reply)
        reason = reply.attribute(_REASON) or ""
        return f"HTTP {status} {reason} {reply.url().toString()}"

    def _schedule(self, fn: Callable[[], None], *, attempt: int = 0, extra_delay_ms: int = 0) -> None:
        """派发一个请求:先等预热完成,再按限速要求延后。

        用定时器轮询而不是 sleep —— 后者会阻塞事件循环,那正是我们要避免的事。
        所有派发(首次请求与风控重试)都走这里,时序逻辑只有一处。

        Args:
            fn: 真正发起请求的无参函数。
            attempt: 已重试次数,仅用于把重试计数透传给 ``fn`` 的闭包。
            extra_delay_ms: 额外的退避延迟(毫秒),与限速取较大者。
        """
        if self._closed:
            return
        if self._warmup_pending:
            # 预热还没回来,推迟 25ms 再看
            QTimer.singleShot(
                _WARMUP_POLL_MS,
                lambda: self._schedule(fn, attempt=attempt, extra_delay_ms=extra_delay_ms),
            )
            return
        delay = max(int(self._limiter.wait_time() * 1000), extra_delay_ms)
        if delay <= 0:
            fn()
        else:
            QTimer.singleShot(delay, fn)

    # ------------------------------------------------------------ JSON 接口

    def get_json(
        self,
        url: str,
        *,
        on_success: JsonCallback,
        on_error: ErrorCallback,
    ) -> FetchHandle:
        """GET 一个 JSON 接口,结果通过回调送回。

        请求本身不在这里发出,而是交给 :meth:`_schedule` 排队等预热与限速。

        Returns:
            可取消的句柄;取消后即使响应回来也不会触发任何回调。
        """
        self.warm_up()
        handle = ReplyHandle()
        self._schedule(
            lambda: self._start_json(url, on_success, on_error, attempt=0, handle=handle)
        )
        return handle

    def _start_json(
        self,
        url: str,
        on_success: JsonCallback,
        on_error: ErrorCallback,
        *,
        attempt: int,
        handle: ReplyHandle,
    ) -> None:
        """真正发出 JSON 请求,并挂上流式解码与重试逻辑。

        Args:
            attempt: 已重试次数;达到 ``max_retries`` 后不再重试而是回报错误。
            handle: 调用方持有的句柄,用来感知取消。
        """
        if self._closed or handle.cancelled:
            return
        self._log.request(url, headers=self._api_headers, cookie_names=self.cookie_names)
        started = time.monotonic()
        reply = self._manager.get(self._make_request(url, self._api_headers))
        handle.reply = reply
        buffer = bytearray()
        # Content-Encoding 要等响应头到了才知道,这里按需切换解码器
        state = {"decoder": IncrementalDecoder("")}

        def read_available() -> None:
            """收到一块响应体:按需建解码器并累积解压后的明文。"""
            incoming = bytes(reply.readAll())
            if not incoming:
                return
            decoder = state["decoder"]
            if not decoder.active:
                encoding = bytes(reply.rawHeader("Content-Encoding")).decode("latin-1")
                decoder = state["decoder"] = IncrementalDecoder(encoding)
            chunk = decoder.feed(incoming) if decoder.active else incoming
            if chunk:
                buffer.extend(chunk)

        def on_finished() -> None:
            """响应结束:冲刷解压器,判断重试,否则交给 ``check_payload`` 校验。"""
            self._limiter.touch()
            try:
                read_available()
                decoder = state["decoder"]
                if decoder.active:
                    tail = decoder.finish()
                    if tail:
                        buffer.extend(tail)
                if handle.cancelled:
                    return
                status = self._status_of(reply)
                if status in RETRYABLE_STATUS or status >= 500:
                    if attempt + 1 < self.max_retries:
                        # 退避只算一次:日志里记的等待时间必须就是真的等多久
                        wait_s = backoff_delay(attempt)
                        self._log.retry(
                            url,
                            attempt=attempt + 1,
                            total=self.max_retries,
                            wait_s=wait_s,
                            status=status,
                        )
                        self.warm_up(force=True)  # 换一份新鲜 Cookie 再试
                        self._schedule(
                            lambda: self._start_json(
                                url, on_success, on_error, attempt=attempt + 1, handle=handle
                            ),
                            attempt=attempt + 1,
                            extra_delay_ms=int(wait_s * 1000),
                        )
                        return
                    error = NetworkError(
                        f"请求失败(已重试 {self.max_retries} 次): {self._describe(reply)}"
                    )
                    self._log.failure(
                        url,
                        status=status,
                        exc=error,
                        body=bytes(buffer),
                        headers=self._api_headers,
                        note="重试耗尽",
                    )
                    on_error(error)
                    return
                if reply.error() != QNetworkReply.NetworkError.NoError:
                    error = NetworkError(f"{reply.errorString()} ({self._describe(reply)})")
                    self._log.failure(
                        url,
                        status=status,
                        exc=error,
                        body=bytes(buffer),
                        headers=self._api_headers,
                        note="连接失败",
                    )
                    on_error(error)
                    return
                payload = bytes(buffer)
                # 业务码非 0 会在这里抛 ApiError,由下面的 except 记成失败
                result = check_payload(payload, url)
                self._log.response(
                    url,
                    status=status,
                    length=len(payload),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    encoding=state["decoder"].encoding if state["decoder"].active else "",
                )
                on_success(result)
            except Exception as exc:  # 解析失败也要回到回调,不能抛进事件循环
                # 走到这里的都还没记过失败(上面每个失败分支都自己 return 了),
                # 所以这一次失败只会有一条 ERROR
                self._log.failure(
                    url,
                    status=self._status_of(reply),
                    exc=exc,
                    body=bytes(buffer),
                    headers=self._api_headers,
                    note="解析失败",
                )
                on_error(exc)
            finally:
                reply.deleteLater()

        reply.readyRead.connect(read_available)
        reply.finished.connect(on_finished)

    # ------------------------------------------------------------ 原始字节

    def get_bytes(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: ErrorCallback,
        headers: dict[str, str] | None = None,
    ) -> FetchHandle:
        """GET 原始字节(封面、字幕等)。

        与 :meth:`get_json` 的区别是不校验业务 ``code``,也不解析 JSON;
        其余(排队、限速、解压)完全一致。这条路径**不做重试** ——
        封面加载失败重试意义不大,不如尽快让界面显示占位图。
        """
        self.warm_up()
        handle = ReplyHandle()

        def start() -> None:
            """真正发出请求并挂上解码与完成回调。"""
            merged = dict(self._api_headers)
            if headers:
                merged.update(headers)
            reply = self._manager.get(self._make_request(url, merged))
            handle.reply = reply
            buffer = bytearray()
            state = {"decoder": IncrementalDecoder("")}

            def read_available() -> None:
                """收到一块响应体:按需建解码器并累积解压后的明文。"""
                incoming = bytes(reply.readAll())
                if not incoming:
                    return
                decoder = state["decoder"]
                if not decoder.active:
                    encoding = bytes(reply.rawHeader("Content-Encoding")).decode("latin-1")
                    decoder = state["decoder"] = IncrementalDecoder(encoding)
                chunk = decoder.feed(incoming) if decoder.active else incoming
                if chunk:
                    buffer.extend(chunk)

            def on_finished() -> None:
                """响应结束:冲刷解压器后把完整字节交给回调。

                这条路径**成功不记 DEBUG**:只有封面在用它,滚一次列表就是几十张图,
                逐张记会把网络日志冲掉。失败仍然记 ``ERROR``。
                """
                self._limiter.touch()
                try:
                    read_available()
                    decoder = state["decoder"]
                    if decoder.active:
                        buffer.extend(decoder.finish())
                    if handle.cancelled:
                        return
                    status = self._status_of(reply)
                    if status in RETRYABLE_STATUS or status >= 500:
                        error = NetworkError(f"请求失败: {self._describe(reply)}")
                        self._log.failure(
                            url,
                            status=status,
                            exc=error,
                            body=bytes(buffer),
                            headers=merged,
                            note="原始字节",
                        )
                        on_error(error)
                        return
                    if reply.error() != QNetworkReply.NetworkError.NoError:
                        error = NetworkError(reply.errorString())
                        self._log.failure(
                            url, status=status, exc=error, headers=merged, note="原始字节"
                        )
                        on_error(error)
                        return
                    on_success(bytes(buffer))
                except Exception as exc:  # noqa: BLE001 - 解析失败也要回到回调
                    self._log.failure(url, exc=exc, note="原始字节")
                    on_error(exc)
                finally:
                    reply.deleteLater()

            reply.readyRead.connect(read_available)
            reply.finished.connect(on_finished)

        self._schedule(start)
        return handle

    # ------------------------------------------------------------ 流式下载

    def download(
        self,
        url: str,
        *,
        sink: Any,
        on_success: SuccessCallback,
        on_error: ErrorCallback,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadHandle:
        """流式下载音频。

        走 CDN,不参与 API 限速,也不声明 gzip(音视频本身已压缩)。
        边收边写盘,内存占用与文件大小无关 —— 两小时的合集也不会吃内存。

        Args:
            sink: :class:`~bilibili_music.core.cache.DownloadSink`,由本方法负责打开与收尾。
            on_success: 接收 ``sink.commit()`` 返回的最终路径。
            on_error: 失败时收到 :class:`NetworkError`;``.part`` 文件已被清理。
            on_progress: 可选,CDN 未给 ``Content-Length`` 时第二个参数为 0。
        """
        sink.open()
        self._log.request(url, headers=self._media_headers, cookie_names=self.cookie_names)
        started = time.monotonic()
        reply = self._manager.get(self._make_request(url, self._media_headers))
        handle = ReplyHandle(reply)

        def read_available() -> None:
            """收到一块音频数据:直接落盘,不做任何缓冲或解压。"""
            data = bytes(reply.readAll())
            if data:
                sink.write(data)

        def on_progress_signal(received: int, total: int) -> None:
            """转发 Qt 的下载进度,同时把字节数记在句柄上供外部查询。"""
            handle.bytes_written = received
            if on_progress:
                on_progress(received, total)

        def on_finished() -> None:
            """下载结束:成功则 commit,任何失败路径都必须 abort 清掉 ``.part``。"""
            try:
                read_available()  # 收尾,避免最后一块丢失
                if handle.cancelled:
                    sink.abort()
                    return
                status = self._status_of(reply)
                if status in RETRYABLE_STATUS or status >= 500:
                    error = NetworkError(f"下载失败: {self._describe(reply)}")
                    self._log.failure(
                        url,
                        status=status,
                        exc=error,
                        headers=self._media_headers,
                        note="下载音轨(已清理 .part)",
                    )
                    sink.abort()
                    on_error(error)
                    return
                if reply.error() != QNetworkReply.NetworkError.NoError:
                    error = NetworkError(f"{reply.errorString()} ({self._describe(reply)})")
                    self._log.failure(
                        url,
                        status=status,
                        exc=error,
                        headers=self._media_headers,
                        note="下载音轨(已清理 .part)",
                    )
                    sink.abort()
                    on_error(error)
                    return
                final = sink.commit()
                # commit 真的成功了才记“响应” —— 否则日志里会出现“成功了但没落盘”
                self._log.response(
                    url,
                    status=status,
                    length=sink.bytes_written,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                on_success(final)
            except Exception as exc:  # noqa: BLE001 - 任何失败都要清掉 .part
                sink.abort()
                self._log.failure(url, exc=exc, note="下载音轨(已清理 .part)")
                on_error(exc)
            finally:
                reply.deleteLater()

        reply.readyRead.connect(read_available)
        reply.downloadProgress.connect(on_progress_signal)
        reply.finished.connect(on_finished)
        return handle

    # ------------------------------------------------------------ 收尾

    def close(self) -> None:
        """停止接受新请求。

        若预热仍在飞行中,先让它在途完成再关闭 —— 我们不想留下取消不掉的
        半截连接。已发出的业务请求各自由其句柄取消。
        """
        self._close_after_warmup = True
        if not self._warmup_pending:
            self._closed = True


def create_backend(**kwargs) -> QtNetworkClient:
    """工厂:默认返回 Qt 后端。

    Args:
        **kwargs: 原样透传给 :class:`QtNetworkClient` 的构造参数。

    Returns:
        新建的 Qt 后端实例。
    """
    return QtNetworkClient(**kwargs)


__all__ = ["QtNetworkClient", "ReplyHandle", "create_backend"]
