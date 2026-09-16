"""网络后端的接口定义与共用辅助。

用 ``typing.Protocol`` 而不是抽象基类:两种后端不共享实现,只共享**契约**,
鸭子类型就够了,也避免为了继承把 Qt 依赖引进纯 Python 那一边。

放在这里的都是"两种后端必须完全一致"的部分:回调签名、重试退避公式、
限速器与响应校验。改这里的任何一处,都要同步跑
``tests/test_backend_contract.py`` —— 它专门守着两个后端不跑偏。
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..core.errors import ApiError, NetworkError
from ..core.http import BASE_BACKOFF, MAX_BACKOFF, RETRYABLE_STATUS
from ..core.redact import redact_headers, redact_text, redact_url

__all__ = [
    "ApiRequest",
    "BILI_COOKIE_DOMAIN",
    "DownloadHandle",
    "ErrorCallback",
    "FetchHandle",
    "HttpBackend",
    "JsonCallback",
    "LOG_BODY_PREVIEW_BYTES",
    "ProgressCallback",
    "RateLimiter",
    "RequestLogger",
    "RETRYABLE_STATUS",
    "SuccessCallback",
    "backoff_delay",
    "check_payload",
    "is_blank_cookie",
]

#: 本项目保存会话 Cookie 时使用的域。两个后端必须用同一个值,否则一边写进去、
#: 另一边读出来(或反过来)就会静默丢凭据。
BILI_COOKIE_DOMAIN = ".bilibili.com"

#: JSON 接口成功回调:参数是校验过 ``code`` 的响应体整体(含 ``data`` 字段)。
JsonCallback = Callable[[dict[str, Any]], None]
#: 失败回调:参数是 :class:`~bilibili_music.core.errors.BiliMusicError` 或其子类实例。
ErrorCallback = Callable[[Exception], None]
#: 下载进度回调:参数是 ``(已收字节数, 总字节数)``;总长未知时第二个参数为 0。
ProgressCallback = Callable[[int, int], None]
#: 下载完成回调:参数是落盘后的最终文件路径(Qt 后端传 ``Path``,urllib 后端也传 ``Path``)。
SuccessCallback = Callable[[Any], None]


class FetchHandle(Protocol):
    """一次请求的句柄,可用来取消。"""

    def cancel(self) -> None:
        """取消这次请求。取消后两个回调都不应再被触发。"""
        ...


class DownloadHandle(Protocol):
    """一次下载的句柄。"""

    @property
    def bytes_written(self) -> int:
        """已写入的字节数(Qt 后端由 ``downloadProgress`` 更新;urllib 后端恒为 0)。"""
        ...

    def cancel(self) -> None:
        """取消这次下载;临时 ``.part`` 文件由后端负责清理。"""
        ...


class HttpBackend(Protocol):
    """网络后端契约。

    两个实现 :class:`~bilibili_music.net.client.QtNetworkClient` 与
    :class:`~bilibili_music.net.urllib_client.UrllibClient` 必须逐条满足,
    派生出的实际差异只有一处:**回调运行在哪个线程**。所以使用方一律按
    "回调可能不在主线程"来写才安全(见 :mod:`..audio.resolver` 的做法)。
    """

    @property
    def cookie_names(self) -> list[str]:
        """当前持有的 Cookie 名(诊断用)。必须是属性,两个后端必须一致。"""
        ...

    def session_cookies(self) -> dict[str, str]:
        """导出 B站 作用域下的会话 Cookie(名字→取值),供 :mod:`..core.session` 落盘。

        只导出**名字与取值**:domain / expires 一律不带。过期由服务端判定 ——
        本地记一个可能算错的过期点,只会把"其实还有效"的凭据误判成失效。

        非 B站 域的 Cookie 必须排除,否则一份"会话文件"里会混进别的站点凭据。

        Returns:
            名字到取值的映射;没有可用 Cookie 时是空字典。

        Note:
            返回值含**凭据取值**,按 ``AGENTS.md`` 第 5 节第 10 条,禁止打印或写日志。
        """
        ...

    def set_session_cookies(self, cookies: dict[str, str]) -> None:
        """把一份会话 Cookie 灌进 jar(域固定为 ``.bilibili.com``、路径 ``/``、``secure``)。

        是**合并**而不是覆盖:匿名预热拿到的 ``buvid3`` / ``b_nut`` 要留着 ——
        实测缺了它们更容易被风控。重复名字以传入值为准。

        Args:
            cookies: 名字到取值的映射;空字典是合法的空操作。
        """
        ...

    def clear_session_cookies(self) -> None:
        """清掉 jar 里所有 B站 域的 Cookie(登出用)。

        是**连匿名 Cookie 一起清**:登出之后不该再留着任何旧会话痕迹。代价是紧接着的
        请求会缺 ``buvid3`` / ``b_nut``(而两个后端的 ``warm_up()`` 默认不会重复预热),
        所以调用方应当在登出后立刻 ``warm_up(force=True)`` 重新拿一份匿名身份。
        """
        ...

    def warm_up(self, *, force: bool = False) -> None:
        """预热会话,取得风控所需的 Cookie(``buvid3`` / ``b_nut``)。

        Args:
            force: 为真时即使已预热过也重新访问主页换一份新鲜 Cookie
                (命中 412/429 重试前就该这么做)。
        """
        ...

    def get_json(
        self,
        url: str,
        *,
        on_success: JsonCallback,
        on_error: ErrorCallback,
    ) -> FetchHandle:
        """GET 一个 JSON 接口。业务 ``code`` 非 0 时通过 ``on_error`` 抛 :class:`ApiError`。"""
        ...

    def get_bytes(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: ErrorCallback,
        headers: dict[str, str] | None = None,
    ) -> FetchHandle:
        """GET 原始字节(封面、字幕等)。

        Args:
            headers: 额外覆盖的请求头,会合并进默认 API 头(同键以它为准)。
        """
        ...

    def download(
        self,
        url: str,
        *,
        sink: Any,
        on_success: SuccessCallback,
        on_error: ErrorCallback,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadHandle:
        """流式下载写入 ``sink``(:class:`~bilibili_music.core.cache.DownloadSink`)。

        成功时 ``on_success`` 收到落盘后的最终路径。``sink`` 由后端负责打开与收尾,
        失败时必须 ``abort()`` 掉临时的 ``.part`` 文件。
        """
        ...

    def close(self) -> None:
        """释放资源,取消所有在飞请求。"""
        ...


@dataclass(slots=True)
class ApiRequest:
    """一个待发送的 API 请求(尚未发出)。

    存在的意义是把"请求长什么样"变成一个可比较、可断言的对象,
    便于在测试里检查两种后端构造出的 URL 与请求头完全一致。
    """

    url: str
    headers: dict[str, str]


def is_blank_cookie(name: str, value: str) -> bool:
    """判断一份 Cookie 是不是"空白项"(名字或取值只有空白)。

    两个后端在**写入**与**导出**两侧都必须用这一条口径,否则会出现"空白取值被当成有效
    凭据存进去、又原样导出来"的不一致 —— 那会让"有没有凭据"这类判断出现假阳性
    (这一条是被 ``test_set_session_cookies_ignores_blank_entries`` 抓出来的:
    最初写成 ``not value``,而 ``"   "`` 在 Python 里是真值)。

    Args:
        name: Cookie 名。
        value: Cookie 取值。

    Returns:
        名字或取值去掉首尾空白后为空时为 ``True``。
    """
    return not name.strip() or not value.strip()


def check_payload(payload: bytes, url: str) -> dict[str, Any]:
    """把响应体解析成 JSON,并校验业务 ``code``。

    非 0 的 ``code`` 转成 :class:`ApiError` 抛出,与后端无关,所以放在共用处。
    这样两个后端对"什么算失败"的判断不可能出现分歧。

    Args:
        payload: 已解压的响应体字节。**必须先解压** —— 传压缩字节会在这里报 JSON 解析失败。
        url: 请求地址,只用于拼错误消息。

    Returns:
        解析出的完整响应对象(含 ``code`` / ``message`` / ``data``,不是只取 ``data``)。

    Raises:
        NetworkError: 不是合法 UTF-8 JSON,或顶层不是对象。
        ApiError: 业务 ``code`` 非 0。
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = payload[:120]
        raise NetworkError(f"响应不是合法 JSON: {url} 前 120 字节={preview!r}") from exc
    if not isinstance(data, dict):
        raise NetworkError(f"响应结构异常(顶层不是对象): {url}")
    if data.get("code") != 0:
        raise ApiError(int(data.get("code", -1)), str(data.get("message", "")), url=url)
    return data


def backoff_delay(attempt: int) -> float:
    """第 ``attempt`` 次重试前应等待的秒数(指数退避 + 抖动)。

    加 0~0.4s 随机抖动是为了避免多个请求重试时"同频共振",
    退避完又同时打过去,那等于没有退避。

    Args:
        attempt: 已重试次数,从 0 开始(0 表示第一次重试前)。

    Returns:
        等待秒数,上限为 :data:`~bilibili_music.core.http.MAX_BACKOFF`。
    """
    return min(MAX_BACKOFF, BASE_BACKOFF * (2**attempt)) + random.uniform(0, 0.4)


class RateLimiter:
    """请求间最小间隔限速器。

    B站风控对短时间内的连续请求很敏感,主动限速比被 412 再退避更划算。

    刻意不做成阻塞式:只提供 :meth:`wait_time` 让调用方自己决定怎么等 ——
    Qt 后端用 ``QTimer`` 推迟派发,urllib 后端才用 ``time.sleep``。
    如果在这里 ``sleep``,Qt 后端就会阻塞事件循环。
    """

    def __init__(self, min_interval: float) -> None:
        """记录最小间隔。

        Args:
            min_interval: 两次请求之间至少间隔多少秒;``<= 0`` 表示不限速(测试用)。
        """
        self.min_interval = min_interval
        self._last = 0.0

    def touch(self) -> None:
        """登记"刚刚发了一次请求",作为下一次计算的起点。

        必须在请求**发出时**(而不是响应回来时)调用,否则慢响应会让限速形同虚设。
        """
        self._last = time.monotonic()

    def wait_time(self) -> float:
        """还需要等多久才能发下一个请求。"""
        if self.min_interval <= 0:
            return 0.0
        elapsed = time.monotonic() - self._last
        return max(0.0, self.min_interval - elapsed)


# ============================== 请求日志

#: 失败时记录响应体前多少字节。
#:
#: 取 2 KiB:足够看清 B站 的错误 JSON 与 412 风控页面的开头,又小到不会把日志撑起来。
#: **正常响应一律不记** —— 响应体里可能有用户名、收藏夹标题这类用户内容。
LOG_BODY_PREVIEW_BYTES = 2048


def _body_preview(body: bytes | None) -> str:
    """把响应体截成一行预览。

    先按字节截断再解码(可能把一个 UTF-8 字符切开,所以用 ``errors="replace"``),
    最后把换行与连续空白压成单个空格 —— 一条日志必须占一行,否则 ``grep`` 就没用了。

    Args:
        body: 已解码的响应体字节;``None`` 或空字节串返回空串。

    Returns:
        单行、已脱敏的预览文本。
    """
    if not body:
        return ""
    text = body[:LOG_BODY_PREVIEW_BYTES].decode("utf-8", "replace")
    return " ".join(redact_text(text).split())


@dataclass(slots=True)
class RequestLogger:
    """把一次请求的生命周期写成"事件 + 字段"的日志行。

    两种后端**共用这一个实现**,于是"两个后端的日志字段必须一致"这条要求由构造满足,
    而不是靠两边各写一遍再祈祷它们不漂移
    (``tests/test_backend_contract.py`` 守着这条)。

    DEBUG 的请求行只记**头的名字**不记取值:排查 403/412 时真正要看的是
    ``Referer`` / ``Origin`` / UA 有没有带上(README 的实测笔记全是这些),而取值本身
    可能含凭据。真需要看取值时看失败行 —— 失败会把脱敏后的完整请求头打出来。

    Args:
        logger: 目标 logger;由后端传自己的模块 logger,日志里因此能看出是哪个后端。
        backend: 后端标识(``"qt"`` / ``"urllib"``),写进每一行。
    """

    logger: logging.Logger
    backend: str

    def debug_enabled(self) -> bool:
        """当前是否真的会记录 DEBUG 明细。

        给调用方一个"要不要为了日志去算点东西"的判断口(当前没有用到:请求路径本来
        就被限速器压在 ≥0.8s 一次,那点开销可以忽略)。

        Returns:
            有效级别低于或等于 ``DEBUG`` 时为 ``True``。
        """
        return self.logger.isEnabledFor(logging.DEBUG)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        cookie_names: Sequence[str] = (),
    ) -> None:
        """记一条"请求已发出"(``DEBUG``)。

        Args:
            url: 完整请求地址;取值会被抹掉。
            method: HTTP 方法。
            headers: 本次实际发出的请求头(只取名字)。
            cookie_names: 当前 jar 里的 Cookie 名(只记个数)。
        """
        self._emit(
            logging.DEBUG,
            "请求",
            [
                ("后端", self.backend),
                ("方法", method),
                ("地址", redact_url(url)),
                ("头名", ",".join(sorted(headers)) if headers else "无"),
                ("cookie", str(len(cookie_names))),
            ],
        )

    def response(
        self,
        url: str,
        *,
        status: int,
        length: int | None = None,
        elapsed_ms: int | None = None,
        encoding: str = "",
    ) -> None:
        """记一条"响应已到"(``DEBUG``)。

        Args:
            url: 请求地址(与请求行成对出现,便于并发请求互相穿插时对号)。
            status: HTTP 状态码。
            length: 已收到的(解压前)字节数;未知时省略。
            elapsed_ms: 从发出到收完的毫秒数;用于看出"变慢"是不是风控的前兆。
            encoding: ``Content-Encoding`` 取值;空串表示没压缩。
        """
        fields = [
            ("后端", self.backend),
            ("地址", redact_url(url)),
            ("状态", str(status)),
        ]
        if length is not None:
            fields.append(("长度", str(length)))
        if elapsed_ms is not None:
            fields.append(("耗时", f"{elapsed_ms}ms"))
        if encoding:
            fields.append(("编码", encoding))
        self._emit(logging.DEBUG, "响应", fields)

    def failure(
        self,
        url: str,
        *,
        exc: BaseException | None = None,
        status: int | None = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        note: str = "",
    ) -> None:
        """记一条彻底失败的请求(``ERROR``)。

        这里是**唯一**会落盘响应体与请求头取值的地方(都已脱敏),因为失败现场需要
        这些证据:412 是 B站 的页面还是空体、请求到底带没带 ``Referer``,事后无法从
        别处推出来。

        Args:
            url: 请求地址。
            exc: 导致失败的异常。
            status: HTTP 状态码;连接根本没建立时为 ``None``。
            body: 响应体原文(只取前 :data:`LOG_BODY_PREVIEW_BYTES` 字节)。
            headers: 本次发出的请求头(经 :func:`redact_headers` 后落盘)。
            note: 补充说明,例如"JSON 接口" / "下载音轨"。
        """
        fields = [("后端", self.backend), ("地址", redact_url(url))]
        if note:
            fields.append(("说明", note))
        if status is not None:
            fields.append(("状态", str(status)))
        if headers:
            redacted = redact_headers(headers)
            fields.append(("请求头", str(redacted)))
        preview = _body_preview(body)
        if preview:
            fields.append(("正文", preview))
        if exc is not None:
            fields.append(("异常", f"{type(exc).__name__}: {exc}"))
        self._emit(logging.ERROR, "失败", fields)

    def retry(
        self,
        url: str,
        *,
        attempt: int,
        total: int,
        wait_s: float,
        status: int | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """记一条退避重试(``WARNING``)。

        风控问题的复盘主力:同一份请求在什么节奏下、被打回几次,只有这里记。

        Args:
            url: 请求地址。
            attempt: 这是第几次重试(从 1 开始)。
            total: 最多尝试几次(含首次)。
            wait_s: 这次退避要等多少秒。
            status: 触发重试的 HTTP 状态码(412 / 429 / 5xx)。
            exc: 触发重试的异常;与 ``status`` 二选一。
        """
        fields = [
            ("后端", self.backend),
            ("地址", redact_url(url)),
            ("重试", f"{attempt}/{total}"),
            ("等待", f"{wait_s:.2f}s"),
        ]
        if status is not None:
            fields.append(("状态", str(status)))
        if exc is not None:
            fields.append(("原因", f"{type(exc).__name__}: {exc}"))
        self._emit(logging.WARNING, "重试", fields)

    def _emit(self, level: int, event: str, fields: Sequence[tuple[str, str]]) -> None:
        """拼出 ``事件 名字=取值 名字=取值`` 并写一条日志。

        先做级别短路:``DEBUG`` 关闭时连字符串都不拼 —— 请求路径再短也是热路径。

        Args:
            level: 日志级别。
            event: 事件名(``请求`` / ``响应`` / ``失败`` / ``重试``)。
            fields: 字段名与取值,顺序即输出顺序。
        """
        if not self.logger.isEnabledFor(level):
            return
        suffix = " ".join(f"{name}={value}" for name, value in fields)
        self.logger.log(level, "%s %s", event, suffix)
