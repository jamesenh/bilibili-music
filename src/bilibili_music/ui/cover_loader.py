"""封面加载:把封面 URL 变成 ``QPixmap``,并丢弃过期的响应。

封面是"锦上添花"的资源,失败一律**静默退回占位图** —— 为一张封面弹窗打断播放是
本末倒置。网络层也是同一条口径:``HttpBackend.get_bytes`` 刻意不做重试。

两层保护:

* ``QPixmapCache``(Qt 自带,有容量上限)—— 来回切歌时不重复下载与解码同一张图
* ``_pending_url`` 记号 —— 快速切歌时**先发的请求会后回来**,不做判断就会把上一首的
  封面贴到当前这首上。这与 ``audio/resolver.py`` 的 ``_alive`` 是同一类问题,
  只是这里只需要比一个 URL。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap, QPixmapCache

__all__ = ["CoverFetcher", "CoverLoader"]


class CoverFetcher(Protocol):
    """取封面字节的契约。

    :meth:`bilibili_music.api.bilibili.BilibiliClient.fetch_cover` 满足它,
    测试也可以塞一个不触网的替身。
    """

    def __call__(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: Callable[[Exception], None],
    ) -> object: ...


class CoverLoader(QObject):
    """按 URL 取封面并转成 ``QPixmap``。

    信号:
        loaded(str, QPixmap): 成功;第一个参数是 URL,便于调用方确认是否仍是当前曲目
        failed(str): 失败(网络错误或数据不是图片)
    """

    loaded = Signal(str, QPixmap)
    failed = Signal(str)

    def __init__(self, fetch: CoverFetcher, parent: QObject | None = None) -> None:
        """绑定取图函数。

        Args:
            fetch: 取封面字节的函数,生产环境传 ``BilibiliClient.fetch_cover``。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._fetch = fetch
        #: 最近一次请求的 URL。**不是**"当前曲目的封面"这种业务概念,只是用来
        #: 判断"回来的这张图还有人要吗"
        self._pending_url = ""

    def load(self, url: str) -> None:
        """请求某张封面;命中缓存时同步发信号。

        Args:
            url: 封面地址;空串表示"这一首没有封面",会立刻发 ``failed``。
        """
        url = (url or "").strip()
        if not url:
            self._pending_url = ""
            self.failed.emit("")
            return

        self._pending_url = url
        cached = QPixmapCache.find(url)
        if cached is not None and not cached.isNull():
            self.loaded.emit(url, cached)
            return

        self._fetch(
            url,
            on_success=lambda data: self._on_bytes(url, data),
            on_error=lambda exc: self._on_error(url, exc),
        )

    def is_current(self, url: str) -> bool:
        """某个 URL 是否仍是最近一次请求的那张(供调用方判断要不要采用结果)。"""
        return bool(url) and url == self._pending_url

    # ------------------------------------------------------------ 内部

    def _on_bytes(self, url: str, data: bytes) -> None:
        """收到图片字节:解码成功才发 ``loaded``,解码失败按失败处理。"""
        if url != self._pending_url:
            return  # 已经切到别的歌了,这张图没人要
        pixmap = QPixmap()
        # loadFromData 对非图片数据返回 False;不能只看 isNull —— 两者都要判,
        # 空数据在部分 Qt 版本上会得到"非空但是 0×0"的位图
        if not data or not pixmap.loadFromData(data) or pixmap.isNull():
            self.failed.emit(url)
            return
        QPixmapCache.insert(url, pixmap)
        self.loaded.emit(url, pixmap)

    def _on_error(self, url: str, exc: Exception) -> None:
        """网络层失败:仍是当前请求才上报,避免旧请求报错干扰新封面。"""
        if url != self._pending_url:
            return
        self.failed.emit(url)
