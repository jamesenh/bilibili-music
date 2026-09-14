"""``QMediaPlayer`` 的薄封装。

对外只暴露"加载本地文件 / 播放 / 暂停 / 跳转 / 调音量"和一组信号,
界面层不需要直接接触 ``QMediaPlayer`` 的枚举与状态机。

封装的意义是把 Qt 那套"PlaybackState / MediaStatus / Error 三套状态互相耦合"的
细节关在里面:界面只关心"在播还是不在播",不必知道 ``EndOfMedia`` 与
``InvalidMedia`` 分别属于哪个枚举。

另外这里还负责**跟随系统默认音频输出设备**。``QAudioOutput`` 的设备绑定是静态的:
它在创建那一刻认下当时的默认设备,之后不会跟着系统设置走,于是用户在播放中换输出设备
(蓝牙耳机 ⇄ 内置扬声器)就会静默无声、且没有任何报错。修法是监听默认设备变化并手动
``setDevice``,具体理由见 :data:`_DEVICE_POLL_INTERVAL_MS` 与
:meth:`PlayerController._sync_output_device`。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from ..core.models import format_duration

__all__ = ["PlayerController"]

#: 默认音量(0.0 ~ 1.0)。0.8 是留了余量的保守值:音乐区素材的响度差异极大,
#: 默认拉满会让部分视频削波,而用户很少主动往回拧。
DEFAULT_VOLUME = 0.8

#: 兜底轮询"系统默认输出设备变没变"的间隔(毫秒)。
#:
#: 为什么不只靠信号:PySide6 6.8.3 的 ``QMediaDevices`` **没有**
#: ``defaultAudioOutputChanged``(实测 ``hasattr`` 为 ``False``,官方文档列出的信号也只有
#: ``audioInputsChanged`` / ``audioOutputsChanged`` / ``videoInputsChanged``),而
#: ``audioOutputsChanged`` 名义上只在"设备列表变化"时触发 —— 用户在两台**都已连接**的
#: 设备之间切换(蓝牙耳机 ⇄ 内置扬声器)时设备集合没变,它未必发得出来。读一次默认设备
#: 只是属性查询,2 秒一次的开销可以忽略,换来的是"用户换了设备我们一定会知道"。
#: 若将来实测确认信号足够可靠,这个兜底可以删掉。
_DEVICE_POLL_INTERVAL_MS = 2000


# ============================== 输出设备判断 ==============================


def _device_key(device: object) -> str:
    """把 ``QAudioDevice`` 归一成可比较的字符串键。

    不直接比较 ``QAudioDevice`` 对象,是因为它的实例在生命周期内会**保留自己的属性**,
    即使物理设备已断开或系统设置已改变 —— 于是两个其实指同一台设备的对象可能不相等,
    一个已经失效的设备看起来也可能"没变"。判断是不是同一台设备只能比 id。

    Args:
        device: ``QAudioDevice``;也接受 ``None``(当作"没有设备")。

    Returns:
        设备 id 的字符串形式;设备为空、或读取 id 失败时返回空串。
    """
    try:
        if device is None or device.isNull():
            return ""
        return bytes(device.id().data()).decode("utf-8", "replace")
    except Exception:
        # 读设备信息失败时退化成"未知设备":空串会让上层倾向于不动输出,而不是崩掉
        return ""


def _should_migrate_output(current_key: str, target_key: str) -> bool:
    """判断是否真的需要把音频输出迁移到新的默认设备。

    两层闸门缺一不可。``target_key`` 为空串表示系统当下没有可用的输出设备,这时**不能**
    调 ``setDevice`` —— 把输出指到空设备等于自断声音(拔掉最后一副耳机就是这种情况)。
    两边相同则是空操作:``audioOutputsChanged`` 会因为"设备列表变化"(插拔、连接、断开)
    而频繁触发,其中多数变化与默认设备无关,不设这道闸就会反复重建底层音频流。

    Args:
        current_key: 输出当前绑定的设备键(见 :func:`_device_key`)。
        target_key: 系统当前默认输出设备的键。

    Returns:
        需要迁移返回 ``True``。
    """
    return bool(target_key) and target_key != current_key


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

        # 输出设备不会自己跟着系统默认设备走,所以这里同时挂上"设备列表变化"信号与一个
        # 兜底轮询 —— 两条路都通向同一个幂等的 _sync_output_device()。
        # QMediaDevices 既要交给 Qt 管生命周期(parent),也要留一个 Python 引用:
        # 实例被回收,信号连接也就跟着没了。
        self._media_devices = QMediaDevices(self)
        self._media_devices.audioOutputsChanged.connect(self._on_audio_outputs_changed)
        self._device_poll = QTimer(self)
        self._device_poll.setInterval(_DEVICE_POLL_INTERVAL_MS)
        self._device_poll.timeout.connect(self._sync_output_device)
        self._device_poll.start()

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

    # ------------------------------------------------------------ 输出设备跟随

    def _on_audio_outputs_changed(self) -> None:
        """音频设备列表变化(插拔、连接、断开):顺手核对一次默认设备是否也变了。

        这个信号**只说明列表变了**,不保证默认设备变了,所以真正的判断放在
        :meth:`_sync_output_device` 里做。
        """
        self._sync_output_device()

    def _sync_output_device(self) -> None:
        """把输出迁移到系统当前默认设备;已经绑对时什么都不做。

        信号与兜底轮询都会调到这里,所以必须是**幂等**的:每次都重新读一次
        ``self._output.device()`` 而不是缓存一份,重复调用自然成为空操作。

        迁移刻意只做"改绑定"这一件事 —— ``setDevice`` 不影响音量、静音等属性,它们原样
        保留。代价是某些平台上底层音频流会被重建、播放状态可能掉出 ``Playing``;这时按
        原位置续播,避免"换个设备就停在半路"。
        """
        target = QMediaDevices.defaultAudioOutput()
        current_key = _device_key(self._output.device())
        if not _should_migrate_output(current_key, _device_key(target)):
            return

        was_playing = self.is_playing
        position = self._player.position()
        self._output.setDevice(target)
        if was_playing and not self.is_playing:
            # 复用 play():它内部带"一次都没 load 过就别动"的保护
            self._player.setPosition(position)
            self.play()
