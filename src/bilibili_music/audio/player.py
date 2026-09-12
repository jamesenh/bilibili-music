"""``QMediaPlayer`` 的薄封装。

对外只暴露"加载本地文件 / 播放 / 暂停 / 跳转 / 调音量"和一组信号,
界面层不需要直接接触 ``QMediaPlayer`` 的枚举与状态机。

封装的意义是把 Qt 那套"PlaybackState / MediaStatus / Error 三套状态互相耦合"的
细节关在里面:界面只关心"在播还是不在播",不必知道 ``EndOfMedia`` 与
``InvalidMedia`` 分别属于哪个枚举。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..core.models import format_duration

__all__ = ["PlayerController"]

#: 默认音量(0.0 ~ 1.0)。0.8 是留了余量的保守值:音乐区素材的响度差异极大,
#: 默认拉满会让部分视频削波,而用户很少主动往回拧。
DEFAULT_VOLUME = 0.8


class PlayerController(QObject):
    """播放器控制中心。

    信号:
        state_changed(bool): 是否正在播放
        position_changed(int, int): 当前毫秒, 总毫秒
        track_finished(): 自然播放结束(可用来做自动下一首)
        error_occurred(str): 播放失败信息
    """

    state_changed = Signal(bool)
    position_changed = Signal(int, int)
    track_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        """建好播放器与音频输出,并接上所有内部槽。

        Args:
            parent: Qt 父对象,交给 Qt 管生命周期。
        """
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._player.setAudioOutput(self._output)
        self._output.setVolume(DEFAULT_VOLUME)

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)

        self._duration_ms = 0
        self._current_path: Path | None = None

    # ------------------------------------------------------------ 属性

    @property
    def current_path(self) -> Path | None:
        """当前已加载的本地文件路径;还没加载过则为 ``None``。"""
        return self._current_path

    @property
    def is_playing(self) -> bool:
        """是否处于播放状态(暂停时为 ``False``)。"""
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def duration_ms(self) -> int:
        """播放器识别出的媒体总时长(毫秒);未加载时保持 0。

        注意这是**播放器解析文件头得到的**时长,可以用来和分P元数据时长对照,
        验证缓存下来的音频没有截断(见 ``scripts/smoke_test.py`` 第 5 步)。
        """
        return self._duration_ms

    @property
    def position_ms(self) -> int:
        """当前播放位置(毫秒);什么都没加载时为 0。

        直接问播放器,而不是复用最近一次 ``position_changed`` 的值:位置信号的
        触发频率由 Qt 决定,**暂停期间不会刷新**,拿它当"当前位置"会读到过期值。
        换音质要按这个位置续播,差一点就能听出来。
        """
        return self._player.position()

    # ------------------------------------------------------------ 操作

    def load(self, path: Path, *, autoplay: bool = True) -> None:
        """加载本地音频文件。

        Args:
            path: 本地音频文件路径(必须已落盘 —— 远程 URL 会被 CDN 的防盗链拒绝)。
            autoplay: 加载后是否立即播放。
        """
        self._current_path = Path(path)
        self._duration_ms = 0
        self._player.setSource(QUrl.fromLocalFile(str(self._current_path)))
        if autoplay:
            self._player.play()

    def play(self) -> None:
        """继续/开始播放。

        还没 ``load()`` 过任何文件时是空操作 —— 否则 Qt 会对着空 source
        报一个用户看不懂的错误。
        """
        if self._current_path is not None:
            self._player.play()

    def pause(self) -> None:
        """暂停播放(保留当前位置,可再 :meth:`play` 续播)。"""
        self._player.pause()

    def toggle(self) -> None:
        """在播放与暂停之间切换(播放按钮的唯一入口)。"""
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        """停止播放并回到起点(窗口关闭时调用,避免后台还在出声)。"""
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        """跳转到指定位置。

        Args:
            position_ms: 目标位置(毫秒);负数会被钳到 0。
        """
        self._player.setPosition(max(0, int(position_ms)))

    def set_volume(self, volume: float) -> None:
        """``volume`` 取值 0.0 ~ 1.0。"""
        self._output.setVolume(min(1.0, max(0.0, float(volume))))

    def volume(self) -> float:
        """当前音量(0.0 ~ 1.0)。"""
        return self._output.volume()

    def set_muted(self, muted: bool) -> None:
        """静音/取消静音(不影响 :meth:`volume` 记录的音量值)。"""
        self._output.setMuted(bool(muted))

    @staticmethod
    def format_ms(ms: int) -> str:
        """把毫秒格式化成界面用的时长文本,复用 :func:`format_duration` 的规则。

        毫秒直接整除成秒,不做四舍五入:进度条走到 0.9s 时显示 ``0:00`` 比显示
        ``0:01`` 更符合"还没到一秒"的直觉。
        """
        return format_duration(ms // 1000)

    # ------------------------------------------------------------ 内部槽

    def _on_position(self, position: int) -> None:
        """播放位置变化:带上已知总时长一起发出去。

        总时长不在这个槽里取,是因为 ``positionChanged`` 触发频率很高(约每秒数次),
        每次都问一次 ``self._player.duration()`` 属于多余调用。
        """
        self.position_changed.emit(position, self._duration_ms)

    def _on_duration(self, duration: int) -> None:
        """媒体时长就绪(通常紧跟 ``setSource`` 之后到达)。

        这里额外补发一次 ``position_changed``:界面上的总时长标签要等这个信号
        才会更新,否则加载完新歌后会短暂显示上一首的时长。
        """
        self._duration_ms = duration
        self.position_changed.emit(self._player.position(), duration)

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        """播放状态变化:折算成界面只需要知道的布尔值。"""
        self.state_changed.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        """媒体状态变化:只在真正放完时发 ``track_finished``。

        用 ``EndOfMedia`` 而不是 ``PlaybackState.StoppedState``:手动 ``stop()``
        也会进 Stopped,拿它触发"自动下一首"会导致暂停后自己跳到下一首。
        """
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.track_finished.emit()

    def _on_error(self, error: QMediaPlayer.Error, error_string: str) -> None:
        """播放出错:忽略 ``NoError``(Qt 在正常加载时也会发一次),其余上报界面。"""
        if error != QMediaPlayer.Error.NoError:
            self.error_occurred.emit(error_string or "播放器出错")
