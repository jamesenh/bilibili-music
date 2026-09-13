"""封面加载:把封面 URL 变成 ``QPixmap``。

封面是"锦上添花"的资源,失败一律**静默退回占位图** —— 为一张封面弹窗打断播放是
本末倒置。网络层也是同一条口径:``HttpBackend.get_bytes`` 刻意不做重试。

多层保护:

* ``QPixmapCache``(Qt 自带,有容量上限)—— 来回切歌时不重复下载与解码同一张图
* ``CoverCache``(磁盘,**跨进程**)—— 重开应用时首屏封面直接读本地文件。这一层很关键:
  封面与 API 共用限速器,而取图是串行的,冷启动首屏十几行要十几秒才能填满
* 在飞请求集合 —— 只接受"确实请求过、且还没被清掉"的响应
* **同一时刻只发一张**的取图队列 —— 见下

为什么要排队
------------

搜索结果一屏有十几行,每行都要封面。``BilibiliClient.fetch_cover`` 走
``HttpBackend.get_bytes``,它**和 API 请求共用同一个限速器**;一次性甩几十个请求出去
既是典型的请求突发(风控最敏感的就是这个),也会把紧接着发出的 playurl / search
挤到限速窗口后面。所以这里一次只发一张,收到响应再发下一张 —— 代价是列表封面填得
慢一点(受 0.8s 限速约束),换来的是"取封面永远不耽误取音源"。

**多张封面可以同时在飞**。列表里一屏有十几行,每行都要封面;旧实现只记"最近一次
请求的 URL",第二个请求一发出就把第一个的响应丢掉,列表于是永远只有最后一行能显示
封面。所以"这份图还要不要"不再由加载器判断:

* 列表这类消费者:所有响应都要(哪一行是哪张图由 URL 决定)
* 播放条这类消费者:只有当前曲目的封面才算数,由它自己拿 :meth:`CoverLoader.is_current`
  过滤(``MainWindow`` 就是这么做的)

加载器只负责"请求过什么、清掉没有";"现在还需不需要"是业务判断,不该埋在工具类里。

磁盘缓存的生命周期
------------------

:meth:`CoverLoader.clear` **不动磁盘缓存**:它清的是"排队与在飞的请求",而磁盘上的图
下次启动还要用 —— 列表换一批就顺手把用户攒下的封面删掉,等于把缓存当临时空间用。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap, QPixmapCache

from ..core.cover_cache import CoverCache

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
    ) -> object:
        """请求某个地址的图片字节。

        Args:
            url: 封面地址(调用方传的已是 https)。
            on_success: 成功回调,参数是图片原始字节。
            on_error: 失败回调;**这条路径不重试**。

        Returns:
            后端的请求句柄,本模块不关心它(封面没有"取消"的需求)。
        """
        ...


class CoverLoader(QObject):
    """按 URL 取封面并转成 ``QPixmap``。

    信号:
        loaded(str, QPixmap): 成功;第一个参数是 URL,便于调用方确认这张图是不是还要
        failed(str): 失败(网络错误或数据不是图片);清除(:meth:`load` 传空串)也走它

    Args:
        fetch: 取封面字节的函数,生产环境传 ``BilibiliClient.fetch_cover``。
        parent: Qt 父对象。
        cache: 磁盘缓存;``None`` 表示**不落盘**(只在内存里缓存)。生产环境由主窗口
            注入,测试默认不落盘 —— 否则用例会读写真实用户的缓存目录。
    """

    loaded = Signal(str, QPixmap)
    failed = Signal(str)

    def __init__(
        self,
        fetch: CoverFetcher,
        parent: QObject | None = None,
        *,
        cache: CoverCache | None = None,
    ) -> None:
        """绑定取图函数与磁盘缓存。

        Args:
            fetch: 取封面字节的函数,生产环境传 ``BilibiliClient.fetch_cover``。
            parent: Qt 父对象。
            cache: 磁盘缓存;``None`` 表示不落盘。
        """
        super().__init__(parent)
        self._fetch = fetch
        self._cache = cache
        #: 已经发出、还没回来的请求 URL 集合。它**不是**"当前曲目的封面"这种业务
        #: 概念,只用来判断"这个响应是我们自己请求过的吗"
        self._pending: set[str] = set()
        #: 待取队列(先进先出)与它的成员集合(用来挡住重复入队)
        self._queue: list[str] = []
        self._queued: set[str] = set()
        #: 在飞请求数。硬性上限是 1,见模块 docstring 的"为什么要排队"
        self._in_flight = 0
        #: 防重入标记:``loaded`` 是同步信号,消费者在槽里再调 :meth:`load` 会回到这里
        self._pumping = False
        #: 最近一次请求的 URL,供 :meth:`is_current` 使用
        self._last_url = ""

    def load(self, url: str) -> None:
        """请求某张封面;命中内存或磁盘缓存时同步发信号。

        可以连续调用多次(列表逐行请求),真正发出的请求由内部队列串起来。

        Args:
            url: 封面地址;空串表示"清了"—— 丢弃所有排队与在飞请求并发出
                ``failed("")``,调用方据此把封面退回占位图。
        """
        url = (url or "").strip()
        if not url:
            self.clear()
            self.failed.emit("")
            return

        self._last_url = url
        if url in self._pending or url in self._queued:
            return  # 已经在取或已经排在队里,不重复请求
        cached = self._cached_pixmap(url)
        if cached is not None:
            self.loaded.emit(url, cached)
            return

        self._queue.append(url)
        self._queued.add(url)
        self._pump()

    def is_current(self, url: str) -> bool:
        """某个 URL 是否仍是最近一次请求的那张(供调用方判断要不要采用结果)。

        只对"一次只关心一张图"的消费者有意义(播放条);列表应当按 URL 自己找行。
        """
        return bool(url) and url == self._last_url

    def clear(self) -> None:
        """丢弃所有排队与在飞的请求。

        与 ``load("")`` 的区别是**不发** ``failed``:列表整体换内容时用这个 —— 调用方
        想要的是"别再取那些没人看的图了",而不是"当前封面没了"。在飞的那一个请求拦不住
        (网络层没有取消句柄),但它的响应回来时会因为不在册而被丢掉。
        """
        self._queue.clear()
        self._queued.clear()
        self._pending.clear()
        self._last_url = ""

    # ------------------------------------------------------------ 内部

    def _cached_pixmap(self, url: str) -> QPixmap | None:
        """依次查内存与磁盘缓存;磁盘命中时顺手回填内存缓存。

        磁盘读是**同步**的:单张封面几十到几百 KB,本地读一次远比一次网络往返便宜,
        而同步返回能让"重开应用后的首屏封面"在填列表当刻就贴出来 —— 改成异步反而要多
        等一轮事件循环。这里也不需要线程(界面层禁止起线程,见 ``AGENTS.md`` 第 4 节)。

        Args:
            url: 封面地址(已去空白、非空)。

        Returns:
            可直接贴到界面上的位图;两处都没有时返回 ``None``,由调用方转去请求网络。
        """
        cached = QPixmapCache.find(url)
        if cached is not None and not cached.isNull():
            return cached
        if self._cache is None:
            return None
        data = self._cache.read(url)
        if not data:
            return None
        pixmap = self._decode(data)
        if pixmap is None:
            # 解不出来的缓存文件是坏条目:留着的话每次加载都要白读一遍盘,而且未必
            # 有机会被"重新下载后覆盖"(网络也可能一直失败),所以当场清掉
            self._cache.discard(url)
            return None
        QPixmapCache.insert(url, pixmap)
        return pixmap

    def _decode(self, data: bytes) -> QPixmap | None:
        """把图片字节解成位图。

        解码失败与"空数据"都要判:``loadFromData`` 对非图片数据返回 ``False``,而空数据
        在部分 Qt 版本上会给出"非空但是 0×0"的位图,只看 ``isNull`` 会漏掉。

        Args:
            data: 图片原始字节。

        Returns:
            解码成功且非空的位图;否则 ``None``(调用方按失败处理)。
        """
        if not data:
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(data) or pixmap.isNull():
            return None
        return pixmap

    def _pump(self) -> None:
        """派发下一张封面;同一时刻只允许一个请求在飞。

        队首若已被缓存(Qt 内存缓存是进程级共享的,磁盘缓存更是跨进程),就直接发信号、
        不浪费一次请求。
        """
        if self._pumping or self._in_flight:
            return
        self._pumping = True
        try:
            while self._queue:
                url = self._queue.pop(0)
                self._queued.discard(url)
                cached = self._cached_pixmap(url)
                if cached is not None:
                    self.loaded.emit(url, cached)
                    continue
                self._in_flight += 1
                self._pending.add(url)
                self._fetch(
                    url,
                    on_success=lambda data, target=url: self._on_bytes(target, data),
                    on_error=lambda exc, target=url: self._on_error(target, exc),
                )
                return
        finally:
            self._pumping = False
        # 上面发信号的槽里可能又调了 load(),把新入队的补上
        if self._queue and not self._in_flight:
            self._pump()

    def _on_bytes(self, url: str, data: bytes) -> None:
        """收到图片字节:解码成功才发 ``loaded`` 并落盘,解码失败按失败处理。"""
        self._in_flight = max(0, self._in_flight - 1)
        if url not in self._pending:
            self._pump()  # 已被清掉,但这张回来了,继续排下一张
            return
        self._pending.discard(url)
        pixmap = self._decode(data)
        if pixmap is None:
            self.failed.emit(url)
            self._pump()
            return
        QPixmapCache.insert(url, pixmap)
        if self._cache is not None:
            # 先落盘再发信号:"界面已经贴上这张图"就意味着它已经进了磁盘缓存,
            # 此刻被杀进程也不会白下载一次
            self._cache.store(url, data)
        self.loaded.emit(url, pixmap)
        self._pump()

    def _on_error(self, url: str, exc: Exception) -> None:
        """网络层失败:仍是我们请求过的地址才上报,避免清掉之后又冒出旧错误。"""
        self._in_flight = max(0, self._in_flight - 1)
        if url in self._pending:
            self._pending.discard(url)
            self.failed.emit(url)
        self._pump()
