"""网络相关的共用常量与调优参数。

这个模块**不含任何实现**,只放常量,方便 ``core`` / ``net`` / ``api`` 各层
共享同一套数值而不会造成循环导入。请求头构造见 :mod:`.headers`,
两种后端实现见 :mod:`..net`。

这些数值都是实测调出来的,尤其是限速与退避 —— 见 ``README.md``「坑 3:风控 412
是间歇性的」。**不要为了"提升速度"调小它们**,那只会让 412 来得更频繁。
"""

from __future__ import annotations

from .headers import API_BASE, DEFAULT_UA, HOME, REFERER

__all__ = [
    "API_BASE",
    "BASE_BACKOFF",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MIN_INTERVAL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_UA",
    "DOWNLOAD_CHUNK",
    "HOME",
    "MAX_BACKOFF",
    "REFERER",
    "RETRYABLE_STATUS",
]

#: 需要**退避重试**的 HTTP 状态码。
#:
#: * ``412`` Precondition Failed —— B站风控限速,间歇性出现,同一份请求第 3 次就可能中
#: * ``429`` Too Many Requests —— 显式限速
#:
#: 注意 ``500`` 及以上**不在这里**:两个后端是各自用 ``status >= 500`` 单独判断的,
#: 因为那属于服务端临时故障,重试策略与风控不同(不需要重新预热换 Cookie)。
RETRYABLE_STATUS: frozenset[int] = frozenset({412, 429})

#: 指数退避的基数(秒)。第 n 次重试等待 ``BASE_BACKOFF * 2**n``。
BASE_BACKOFF = 0.6
#: 指数退避的上限(秒),防止重试次数多时等待时间失控。
MAX_BACKOFF = 8.0

#: 单次请求超时(秒)。JSON 接口通常 1s 内返回,20s 只用于兜住极端网络。
DEFAULT_TIMEOUT = 20.0
#: 请求间最小间隔(秒)。B站风控对连续请求敏感,主动限速比被 412 更划算。
DEFAULT_MIN_INTERVAL = 0.8
#: 默认重试次数(含首次请求在内)。再多收益很低,只会拖长失败返回时间。
DEFAULT_MAX_RETRIES = 3
#: 流式下载的读块大小(64 KiB)。
#:
#: 与 :data:`bilibili_music.core.cache.CHUNK_SIZE` 保持一致:这个大小既能让进度回调
#: 足够细,又不会因为过于频繁的写盘系统调用拖慢下载。
DOWNLOAD_CHUNK = 1 << 16
