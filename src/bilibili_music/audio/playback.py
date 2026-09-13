"""播放编排:把队列、音源解析与播放器串成一条能自动前进的流水线。

这一层回答的是"谁决定下一首放什么、什么时候换歌":``core.queue`` 只算顺序与模式,
``audio.resolver`` 只把一首歌变成文件,``audio.player`` 只会放文件 —— 把三者连起来、
并处理"播完自动前进""切歌时丢弃过期回调""换音质保留进度"的地方就是这里。

**界面不许自己拼这套逻辑**(``AGENTS.md`` 第 4 节:UI 只做展示与事件转发)。
这些逻辑原先散在 ``ui/main_window.py::_on_track_finished`` 里,搬出来之后界面只需要
把用户操作转成方法调用、把信号渲染成界面状态。

依赖为什么以参数注入
--------------------

:class:`PlaybackController` 不自己 ``new`` 解析器与播放器。真实的
:class:`~bilibili_music.audio.resolver.AudioResolver` 要触网,真实的
:class:`~bilibili_music.audio.player.PlayerController` 会构造 ``QMediaPlayer``
(在没有 ``QCoreApplication`` 时会打印告警),两者都不适合放进单元测试。

替身需要提供与 ``PlayerController`` **同名的那四个信号**(``state_changed`` /
``position_changed`` / ``track_finished`` / ``error_occurred``)以及
``load`` / ``play`` / ``pause`` / ``toggle`` / ``stop`` / ``seek`` / ``position_ms`` /
``set_volume``;解析器替身需要有 ``resolve`` 与 ``cancel``。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QObject, Signal

from ..core.queue import PlayMode, PlayQueue, QueueItem
from .player import PlayerController
from .resolver import AudioResolver, ResolvedAudio

__all__ = [
    "PlaybackController",
    "RESUME_TAIL_MARGIN_MS",
]

#: 换音质续播时,seek 目标与新媒体时长之间至少留出的余量(毫秒)。
#:
#: 不同档位的时长会有零头差异。若把位置直接 seek 到新媒体的末尾,播放器会立刻
#: 判定"已播完"并触发自动下一首 —— 用户看到的现象是"一换音质就跳歌"。
RESUME_TAIL_MARGIN_MS = 1000


class PlaybackController(QObject):
    """播放队列的编排者。

    信号:
        track_changed(object): 当前项(:class:`QueueItem`)变了,界面据此换标题与高亮
        audio_ready(object): 音源已就绪并开始播放,携带 :class:`ResolvedAudio`
        queue_changed(): 队列内容变了(增删、整体替换),界面重新渲染列表
        mode_changed(object): 播放模式变了,携带 :class:`PlayMode`
        state_changed(bool): 是否正在播放(转发自播放器)
        position_changed(int, int): 当前毫秒, 总毫秒(转发自播放器)
        progress(int, int): 缓存下载进度 ``(已收字节, 总字节)``
        error_occurred(str): 解析或播放失败
        stopped(): 播放因"没有下一项"而停止(顺序模式播完末项、队列被删空等)

    Args:
        resolver: 音源解析器,生产环境传 :class:`AudioResolver`。
        player: 播放器,生产环境传 :class:`PlayerController`。
        queue: 队列;``None`` 表示新建一个(播放模式为顺序播放)。
        parent: Qt 父对象,交给 Qt 管生命周期。
    """

    track_changed = Signal(object)
    audio_ready = Signal(object)
    queue_changed = Signal()
    mode_changed = Signal(object)
    state_changed = Signal(bool)
    position_changed = Signal(int, int)
    progress = Signal(int, int)
    error_occurred = Signal(str)
    stopped = Signal()

    def __init__(
        self,
        resolver: AudioResolver,
        player: PlayerController,
        *,
        queue: PlayQueue | None = None,
        parent: QObject | None = None,
    ) -> None:
        """接上解析器与播放器的信号;此时不播放任何内容。

        Args:
            resolver: 音源解析器。
            player: 播放器。
            queue: 复用的队列;``None`` 时新建。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.resolver = resolver
        self.player = player
        self.queue = queue if queue is not None else PlayQueue()

        #: 用户指定的音质档位;``None`` 表示自动选最高码率
        self._quality_id: int | None = None
        #: 待恢复的播放位置(毫秒);0 表示不需要恢复。换音质时用它把进度续上
        self._resume_position_ms = 0
        #: 当前正在播的分P序号。多P合集播到第 3P 时,队列项记的仍是入队时的那个分P,
        #: 所以"到底在播哪一P"只能由编排层自己记(见 :attr:`page_index`)
        self._page_index = 1

        player.state_changed.connect(self._on_state_changed)
        player.position_changed.connect(self._on_position_changed)
        player.track_finished.connect(self._on_track_finished)
        player.error_occurred.connect(self._on_player_error)

    # ------------------------------------------------------------ 只读视图

    @property
    def current(self) -> QueueItem | None:
        """当前应播的项;队列为空时为 ``None``。"""
        return self.queue.current

    @property
    def quality_id(self) -> int | None:
        """当前指定的音质档位;``None`` 表示自动选最高。"""
        return self._quality_id

    @property
    def page_index(self) -> int:
        """当前正在播的分P序号。

        它**不一定**等于 ``current.page_index``:多P合集播到第 3P 时,队列项记的仍是
        入队时的分P。界面要显示"P3"应当读 :class:`ResolvedAudio`(它带真实分P),
        这个属性主要服务于状态判断与测试。
        """
        return self._page_index

    @property
    def is_playing(self) -> bool:
        """是否正在播放。"""
        return self.player.is_playing

    # ------------------------------------------------------------ 队列操作

    def play_queue(self, items: Sequence[QueueItem], *, start: int = 0) -> None:
        """用一份新列表整体替换队列并开始播放。

        双击搜索结果走的就是这条:整个结果列表成为队列,从双击那一行开始播。

        Args:
            items: 新的队列内容。
            start: 从第几项开始播;越界会被夹到两端。
        """
        self.queue.replace(items, start=start)
        self.queue_changed.emit()
        if self.current is None:
            # 队列为空时必须取消在飞的解析,否则它回来后会开始放一首已经被
            # 换掉的歌 —— 用户看到的是"清空了队列却自己响起来"
            self.resolver.cancel()
            self.player.stop()
            return
        self._start_current()

    def enqueue(self, item: QueueItem) -> None:
        """把一项加到队列末尾("加入队列")。

        队列原本为空时会直接开始播放 —— 否则用户点了"加入队列"却什么都不发生。
        """
        was_empty = len(self.queue) == 0
        self.queue.append(item)
        self.queue_changed.emit()
        if was_empty:
            self._start_current()

    def enqueue_next(self, item: QueueItem) -> None:
        """把一项插到当前项后面("下一首播放")。队列为空时等价于 :meth:`enqueue`。"""
        was_empty = len(self.queue) == 0
        self.queue.insert_next(item)
        self.queue_changed.emit()
        if was_empty:
            self._start_current()

    def jump_to(self, index: int) -> bool:
        """跳到队列里的第 ``index`` 项并播放。

        Args:
            index: 目标项在队列内容里的下标。

        Returns:
            是否跳成功;下标越界返回 ``False`` 且不影响当前播放。
        """
        if self.queue.jump_to(index) is None:
            return False
        self._start_current()
        return True

    def remove_at(self, index: int) -> None:
        """从队列移除一项。

        移除的若是**正在播**的那一项,就接着播顶上来的一项(没有剩项则停止)——
        否则界面上的"当前项"会和实际出声的那首歌不一致,后续操作会一路错下去。

        Args:
            index: 要移除的项在队列内容里的下标。
        """
        if not 0 <= index < len(self.queue):
            return
        was_current = index == self.queue.current_index
        self.queue.remove_at(index)
        self.queue_changed.emit()
        if not was_current:
            return
        if self.current is None:
            self.player.stop()
            self.stopped.emit()
        else:
            self._start_current()

    def clear(self) -> None:
        """清空队列、取消在飞解析并停止播放。"""
        self.resolver.cancel()
        self.queue.clear()
        self.player.stop()
        self.queue_changed.emit()
        self.stopped.emit()

    def set_mode(self, mode: PlayMode) -> None:
        """切换播放模式。

        只影响**下一次**前进:当前正在播的那一首不受影响(队列层保证了这一点)。

        Args:
            mode: 目标模式。

        Raises:
            ValueError: ``mode`` 不是合法的 :class:`PlayMode` 取值。
        """
        self.queue.set_mode(mode)
        self.mode_changed.emit(self.queue.mode)

    # ------------------------------------------------------------ 播放控制

    def next(self) -> bool:
        """手动下一首。

        Returns:
            是否切到了新的一项;顺序模式已在末项时为 ``False``(界面据此不做任何事)。
        """
        if self.queue.next_item() is None:
            return False
        self._start_current()
        return True

    def previous(self) -> bool:
        """手动上一首。

        Returns:
            是否切到了上一项;非循环模式已在第一项时为 ``False``。
        """
        if self.queue.previous_item() is None:
            return False
        self._start_current()
        return True

    def toggle(self) -> None:
        """在播放与暂停之间切换。"""
        self.player.toggle()

    def pause(self) -> None:
        """暂停(保留位置)。"""
        self.player.pause()

    def resume(self) -> None:
        """继续播放。"""
        self.player.play()

    def stop(self) -> None:
        """停止播放,但**保留队列与当前项**(与 :meth:`clear` 区分)。

        必须同时取消在飞解析:否则刚停掉,几秒后那个请求回来还是会开始放。
        """
        self.resolver.cancel()
        self.player.stop()

    def seek(self, position_ms: int) -> None:
        """跳转到指定位置。

        同时丢弃"待恢复的续播位置":用户手动拖过进度条之后,再让换音质的续播
        把它拽回去就说不通了。
        """
        self._resume_position_ms = 0
        self.player.seek(position_ms)

    def set_volume(self, volume: float) -> None:
        """设置音量。

        Args:
            volume: 0.0 ~ 1.0;越界由播放器自己钳制。
        """
        self.player.set_volume(volume)

    def set_quality(self, quality_id: int | None) -> None:
        """设定音质档位,并立刻按新档位重播当前曲目。

        ``None`` 表示恢复"自动选最高码率"。重播会**保留播放位置**:先记下当前
        位置,待新媒体时长就绪后再 seek 回去(见 :meth:`_on_position_changed`)。

        Args:
            quality_id: 目标档位 id,如 ``30280``;``None`` 表示自动。
        """
        if quality_id == self._quality_id:
            return
        self._quality_id = quality_id
        if self.current is None:
            return
        # 必须显式带上当前分P:合集播到第 5P 时换音质,不能把用户踢回该项的入队分P
        self._start_current(position_ms=self.player.position_ms, page_index=self._page_index)

    def play_page(self, page_index: int) -> bool:
        """切到当前视频的另一个分P播放。

        多P合集在队列里只占一行,而它内部有 N 首独立的歌(领域铁律),所以"换这一行
        内部的哪一首"不改动队列,只把播放目标挪到另一个分P上。切换沿用
        :meth:`_start_current` 这条既有路径:取消在飞的解析、按**该分P自己的** ``cid``
        重新解析(缓存键因此也带上那个 ``cid``)、加载本地新媒体文件并自动播放 ——
        行为与手动换歌完全一致,也与"播放中换音质"同一套策略(不做特殊暂停/续播处理,
        新分P从头播)。

        Args:
            page_index: 目标分P序号,从 1 开始。

        Returns:
            是否切成功;没有当前项、或该视频的分P列表里没有这个序号时返回 ``False``
            且不影响当前播放。**已经在这一P上**时返回 ``True`` 但不重新解析 ——
            重播会把正在放的那一首打断,而菜单里点到当前行本就是想"关掉菜单"。
        """
        item = self.current
        if item is None:
            return False
        if item.video.page(page_index) is None:
            return False
        target = int(page_index)
        if target == self._page_index:
            return True
        self._start_current(page_index=target)
        return True

    # ------------------------------------------------------------ 内部

    def _start_current(self, *, position_ms: int = 0, page_index: int | None = None) -> None:
        """解析并播放当前项。

        Args:
            position_ms: 解析完成后要恢复到的位置;0 表示从头播。
            page_index: 要播的分P序号;``None`` 表示用队列项自己记的那个分P
                (换歌、跳转都走这条),合集内部播完一P继续下一P时才显式传值。
        """
        item = self.current
        if item is None:
            return
        self._page_index = item.page_index if page_index is None else int(page_index)
        self._resume_position_ms = max(0, int(position_ms))
        self.track_changed.emit(item)
        self.resolver.resolve(
            item.video,
            page_index=self._page_index,
            quality_id=self._quality_id,
            on_success=self._on_resolved,
            on_error=self._on_resolve_failed,
            on_progress=self._on_progress,
        )

    def _on_resolved(self, resolved: ResolvedAudio) -> None:
        """音源就绪:开始播放并把结果交给界面。"""
        self.player.load(resolved.path, autoplay=True)
        self.audio_ready.emit(resolved)

    def _on_resolve_failed(self, exc: Exception) -> None:
        """解析失败:报给界面,**不**自动跳到下一首。

        自动跳过失败项会把"这一首取不到音源"变成静默跳歌,用户不知道发生了什么;
        让他看到错误再自己决定更诚实。
        """
        self.error_occurred.emit(str(exc))

    def _on_progress(self, done: int, total: int) -> None:
        """转发缓存下载进度。"""
        self.progress.emit(done, total)

    def _on_track_finished(self) -> None:
        """自然播完:先看多P合集内部有没有下一分P,再按播放模式前进。

        分P前进必须排在队列前进**之前**:一个 200P 合集在队列里只占一行,但它内部
        有 200 首歌(领域铁律)。"视频内部也是一条队列"这条规则队列层不认识,
        所以两步的先后只能由编排层决定。

        单曲循环要**先**排除掉:"重播当前这一首"指的就是当前这个分P,不能被
        "合集里还有下一P"顶掉。
        """
        item = self.current
        if item is None:
            self.stopped.emit()
            return
        if self.queue.mode is PlayMode.REPEAT_ONE:
            # 显式带上当前分P:走通用路径会退回"队列项记的那个分P",于是合集播到
            # 第 2P 时按单曲循环反而跳回第 1P
            self._start_current(page_index=self._page_index)
            return
        next_page = self._page_index + 1
        if item.video.page(next_page) is not None:
            self._start_current(page_index=next_page)
            return
        if self.queue.next_item(auto=True) is None:
            self.stopped.emit()
            return
        self._start_current()

    def _on_position_changed(self, position: int, duration: int) -> None:
        """转发播放位置,并在需要时补上换音质/换源后的续播位置。

        续播必须等到 ``duration > 0`` 才做:``load()`` 之后新媒体时长还没解析
        出来,这时 seek 会被播放器忽略或夹到 0。位置还要**钳到新时长之内**,因为
        不同档位的时长会有零头差异,直接 seek 到末尾会让播放器立刻判定播完。
        """
        if self._resume_position_ms > 0 and duration > 0:
            target = min(
                self._resume_position_ms,
                max(0, duration - RESUME_TAIL_MARGIN_MS),
            )
            self._resume_position_ms = 0
            self.player.seek(target)
        self.position_changed.emit(position, duration)

    def _on_state_changed(self, playing: bool) -> None:
        """转发播放状态。"""
        self.state_changed.emit(playing)

    def _on_player_error(self, message: str) -> None:
        """转发播放器错误。"""
        self.error_occurred.emit(message)
