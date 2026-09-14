"""网络后端的接口定义与共用辅助。

用 ``typing.Protocol`` 而不是抽象基类:两种后端不共享实现,只共享**契约**,
鸭子类型就够了,也避免为了继承把 Qt 依赖引进纯 Python 那一边。

放在这里的都是"两种后端必须完全一致"的部分:回调签名、重试退避公式、
限速器与响应校验。改这里的任何一处,都要同步跑
``tests/test_backend_contract.py`` —— 它专门守着两个后端不跑偏。
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..core.errors import ApiError, NetworkError
from ..core.http import BASE_BACKOFF, MAX_BACKOFF, RETRYABLE_STATUS

__all__ = [
    "ApiRequest",
    "BILI_COOKIE_DOMAIN",
    "DownloadHandle",
    "ErrorCallback",
    "FetchHandle",
    "HttpBackend",
    "JsonCallback",
    "ProgressCallback",
    "RateLimiter",
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
