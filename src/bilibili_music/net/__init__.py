"""网络后端。两种实现可互换:

* :mod:`~bilibili_music.net.client` —— ``QNetworkAccessManager``(默认,Qt 原生异步)
* :mod:`~bilibili_music.net.urllib_client` —— 标准库 ``urllib``(无 Qt 依赖)

两者都提供完全相同的异步回调接口,所以上层(``api`` / ``audio`` / ``ui``)
不需要知道用的是哪一个,也便于在没有 Qt 的环境里跑测试。

唯一的实质差异是**回调在哪个线程执行**:Qt 后端的回调在主线程,urllib 后端在工作
线程(见各自的类 docstring)。使用方按"回调可能不在主线程"来写,才能在后端之间自由切换。
"""

from .base import (
    ApiRequest,
    DownloadHandle,
    ErrorCallback,
    FetchHandle,
    HttpBackend,
    JsonCallback,
    ProgressCallback,
    RateLimiter,
    SuccessCallback,
)
from .client import QtNetworkClient, create_backend
from .urllib_client import UrllibClient

__all__ = [
    "ApiRequest",
    "DownloadHandle",
    "ErrorCallback",
    "FetchHandle",
    "HttpBackend",
    "JsonCallback",
    "ProgressCallback",
    "QtNetworkClient",
    "RateLimiter",
    "SuccessCallback",
    "UrllibClient",
    "create_backend",
]
