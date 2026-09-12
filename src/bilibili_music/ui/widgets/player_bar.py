"""底部播放条:传输控件、进度、音量、播放模式与音质。

控件**不自己保存播放状态**。``set_*`` 方法由上层把状态推下来,信号把用户操作送上去,
这样"当前在播什么、在播第几秒"只有一个真源(``audio.playback``),不会出现
"界面显示在播、实际已经停了"这种两边各存一份状态导致的错位。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...core.models import format_duration
from ...core.queue import PlayMode
from ..icons import Palette, get_icon, palette

__all__ = [
    "BUTTON_ICON_SIZE",
    "MODE_CYCLE",
    "QUALITY_CHOICES",
    "PlayerBar",
]

#: 播放控件图标的逻辑尺寸。按钮给 32×32,留出内边距。
BUTTON_ICON_SIZE = 20

#: 封面缩略图的边长(正方形)。
COVER_SIZE = 56

#: 音质下拉框的固定选项 ``(显示文本, quality_id)``。
#:
#: 刻意**不**为每首歌请求一次 playurl 去问"这个视频到底有哪些档位":那会让切歌多出
#: 一次网络往返。``AudioResolver`` 本来就处理了"指定档位这次没有"的情况(退回最高
#: 码率),所以固定列表足够用,而且不会因为档位缺失就让选项忽有忽无。
QUALITY_CHOICES: tuple[tuple[str, int | None], ...] = (
    ("音质:自动", None),
    ("音质:192K", 30280),
    ("音质:132K", 30232),
    ("音质:64K", 30216),
)

#: 点一次模式按钮的切换顺序。
MODE_CYCLE: tuple[PlayMode, ...] = (
    PlayMode.SEQUENCE,
    PlayMode.REPEAT_ALL,
    PlayMode.REPEAT_ONE,
    PlayMode.SHUFFLE,
)

#: 每种模式用的图标与中文提示。
#:
#: 顺序播放与列表循环共用 ``repeat`` 图标,靠**颜色**区分(灰=不循环,强调色=循环):
#: 为"不循环"单独画一个图标才是真正的浪费。
MODE_ICONS: dict[PlayMode, tuple[str, str]] = {
    PlayMode.SEQUENCE: ("repeat", "顺序播放(点击切换)"),
    PlayMode.REPEAT_ALL: ("repeat", "列表循环(点击切换)"),
    PlayMode.REPEAT_ONE: ("repeat-one", "单曲循环(点击切换)"),
    PlayMode.SHUFFLE: ("shuffle", "随机播放(点击切换)"),
}


class PlayerBar(QWidget):
    """底部播放条。

    信号:
        play_toggled(): 播放/暂停按钮被点
        next_requested(): 请求下一首
        previous_requested(): 请求上一首
        mode_changed(object): 模式已切换,携带新的 :class:`PlayMode`
        quality_changed(object): 音质已选,携带 ``quality_id``(``None`` 表示自动)
        seek_requested(int): 用户拖完进度条,携带目标毫秒
        volume_changed(float): 音量滑块变化,携带 0.0 ~ 1.0
    """

    play_toggled = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    mode_changed = Signal(object)
    quality_changed = Signal(object)
    seek_requested = Signal(int)
    volume_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好所有控件并接上内部信号。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._palette = palette("light")
        self._mode = PlayMode.SEQUENCE
        #: 拖动进度条期间不要被播放位置回写打断
        self._seeking = False
        self._duration_ms = 0
        #: 记住播放态与封面,**只为了换主题时能按新配色重绘**。它们不是播放状态的
        #: 真源(真源在 audio.playback),这里存的是"上次画成什么样"
        self._playing = False
        self._cover: QPixmap | None = None

        self._build_ui()
        self.set_playing(False)
        self.set_mode(self._mode)

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        """组装封面、曲目信息、传输控件、进度与音量。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(10)

        self.cover = QLabel()
        self.cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.cover)
        self.set_cover(None)

        info = QVBoxLayout()
        info.setSpacing(2)
        self.title_label = QLabel("未播放")
        self.title_label.setStyleSheet("font-weight: 600;")
        # 标题过长时自己省略,而不是把播放条撑宽
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setStyleSheet(f"color: {self._palette.muted};")
        info.addWidget(self.title_label)
        info.addWidget(self.subtitle_label)
        info.addStretch(1)
        root.addLayout(info, 1)

        controls = QVBoxLayout()
        controls.setSpacing(4)
        controls.addLayout(self._build_transport_row())
        controls.addLayout(self._build_progress_row())
        root.addLayout(controls, 2)

        root.addLayout(self._build_volume_box())

    def _build_transport_row(self) -> QHBoxLayout:
        """建"模式 / 上一首 / 播放 / 下一首 / 音质"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(6)

        self.mode_button = self._icon_button("repeat", "顺序播放(点击切换)")
        self.mode_button.clicked.connect(self._on_mode_clicked)

        self.previous_button = self._icon_button("prev", "上一首")
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.play_button = self._icon_button("play", "播放")
        self.play_button.clicked.connect(self.play_toggled.emit)

        self.next_button = self._icon_button("next", "下一首")
        self.next_button.clicked.connect(self.next_requested.emit)

        for button in (
            self.mode_button,
            self.previous_button,
            self.play_button,
            self.next_button,
        ):
            row.addWidget(button)

        row.addStretch(1)

        self.quality_combo = QComboBox()
        for label, quality_id in QUALITY_CHOICES:
            self.quality_combo.addItem(label, quality_id)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        row.addWidget(self.quality_combo)
        return row

    def _build_progress_row(self) -> QHBoxLayout:
        """建"当前时间 / 进度条 / 总时长"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(6)

        self.position_label = QLabel("0:00")
        self.duration_label = QLabel("0:00")
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderPressed.connect(self._on_slider_pressed)
        self.position_slider.sliderReleased.connect(self._on_slider_released)

        row.addWidget(self.position_label)
        row.addWidget(self.position_slider, 1)
        row.addWidget(self.duration_label)
        return row

    def _build_volume_box(self) -> QVBoxLayout:
        """建音量标签 + 滑块。"""
        box = QVBoxLayout()
        box.setSpacing(2)
        self.volume_label = QLabel("音量")
        self.volume_label.setStyleSheet(f"color: {self._palette.muted};")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(110)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        box.addWidget(self.volume_label)
        box.addWidget(self.volume_slider)
        return box

    def _icon_button(self, icon_name: str, tooltip: str) -> QPushButton:
        """造一个只放图标的按钮。

        Args:
            icon_name: 图标名(``resources/icons`` 下的文件名)。
            tooltip: 中文提示,播放控件不写文字,全靠它表达含义。

        Returns:
            固定尺寸的按钮。
        """
        button = QPushButton()
        button.setFixedSize(32, 32)
        button.setIconSize(QSize(BUTTON_ICON_SIZE, BUTTON_ICON_SIZE))
        button.setToolTip(tooltip)
        button.setIcon(self._themed_icon(icon_name, self._palette.text))
        return button

    def _themed_icon(self, icon_name: str, color: str) -> QIcon:
        """按图标名与颜色取一个带禁用态的图标。"""
        return get_icon(
            icon_name,
            color,
            BUTTON_ICON_SIZE,
            disabled_color=self._palette.disabled,
        )

    # ------------------------------------------------------------ 上层推状态

    def set_playing(self, playing: bool) -> None:
        """更新播放/暂停按钮的外观。

        Args:
            playing: 是否正在播放。
        """
        self._playing = bool(playing)
        name = "pause" if self._playing else "play"
        tooltip = "暂停" if self._playing else "播放"
        self.play_button.setIcon(self._themed_icon(name, self._palette.text))
        self.play_button.setToolTip(tooltip)

    def set_now_playing(self, title: str, subtitle: str) -> None:
        """更新曲目标题与副标题。

        Args:
            title: 主标题(多P时是分P标题)。
            subtitle: 副标题(UP主、音质等)。
        """
        self.title_label.setText(title or "未播放")
        self.title_label.setToolTip(title or "")
        self.subtitle_label.setText(subtitle or "")

    def set_position(self, position_ms: int, duration_ms: int) -> None:
        """更新进度条与时间标签。

        Args:
            position_ms: 当前播放位置(毫秒)。
            duration_ms: 总时长(毫秒);为 0 表示还不知道,此时只更新时间标签。
        """
        if duration_ms > 0:
            self._duration_ms = duration_ms
            self.position_slider.setRange(0, duration_ms)
            self.duration_label.setText(format_duration(duration_ms // 1000))
        if not self._seeking:
            self.position_slider.setValue(max(0, position_ms))
            self.position_label.setText(format_duration(max(0, position_ms) // 1000))

    def set_mode(self, mode: PlayMode) -> None:
        """更新模式按钮的图标、颜色与提示。

        Args:
            mode: 目标模式。
        """
        self._mode = PlayMode(mode)
        icon_name, tooltip = MODE_ICONS[self._mode]
        # 顺序播放用弱化色:图标一样,颜色就是"循环开没开"的唯一区别
        color = (
            self._palette.muted
            if self._mode is PlayMode.SEQUENCE
            else self._palette.accent
        )
        self.mode_button.setIcon(self._themed_icon(icon_name, color))
        self.mode_button.setToolTip(tooltip)

    def set_quality(self, quality_id: int | None) -> None:
        """把音质下拉框同步到指定档位(不触发 ``quality_changed``)。

        Args:
            quality_id: 档位 id;``None`` 表示"自动"。
        """
        index = self.quality_combo.findData(quality_id)
        if index < 0:
            return
        self.quality_combo.blockSignals(True)
        self.quality_combo.setCurrentIndex(index)
        self.quality_combo.blockSignals(False)

    def set_cover(self, pixmap: QPixmap | None) -> None:
        """设置封面缩略图;``None`` 时用音乐图标占位。

        Args:
            pixmap: 已下载好的封面;传 ``None`` 表示没有封面(或下载失败)。
        """
        self._cover = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self._render_cover()

    @property
    def has_cover(self) -> bool:
        """当前显示的是真封面还是占位图标。

        封面槽位**永远**有图(没有就画占位图标),所以光看 ``cover.pixmap()`` 非空
        判断不出"到底有没有封面";界面自检与测试都需要这个明确的答案。
        """
        return self._cover is not None

    def _render_cover(self) -> None:
        """把当前封面(或占位图标)画进封面槽位。

        单独抽出来是因为换主题时也要按新配色重画一遍占位图标。
        """
        if self._cover is None:
            self.cover.setPixmap(
                self._themed_icon("music", self._palette.muted).pixmap(
                    COVER_SIZE // 2, COVER_SIZE // 2
                )
            )
            return
        self.cover.setPixmap(
            self._cover.scaled(
                COVER_SIZE,
                COVER_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def apply_palette(self, colors: Palette) -> None:
        """套用一套主题配色:按新颜色重绘所有图标与文字。

        Args:
            colors: 来自 :func:`bilibili_music.ui.theme.apply_theme` 的调色板。
        """
        self._palette = colors
        self.subtitle_label.setStyleSheet(f"color: {self._palette.muted};")
        self.volume_label.setStyleSheet(f"color: {self._palette.muted};")
        self.previous_button.setIcon(self._themed_icon("prev", self._palette.text))
        self.next_button.setIcon(self._themed_icon("next", self._palette.text))
        # 播放按钮与模式按钮的颜色取决于状态,交给各自的方法重画,避免在这里
        # 复制一遍"哪种状态配哪个图标"的判断
        self.set_playing(self._playing)
        self.set_mode(self._mode)
        self._render_cover()

    def set_volume(self, volume: float) -> None:
        """把音量滑块同步到指定值(不触发 ``volume_changed``)。

        Args:
            volume: 0.0 ~ 1.0。
        """
        value = int(round(min(1.0, max(0.0, volume)) * 100))
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(value)
        self.volume_slider.blockSignals(False)

    @property
    def mode(self) -> PlayMode:
        """当前显示的播放模式。"""
        return self._mode

    # ------------------------------------------------------------ 内部槽

    def _on_mode_clicked(self) -> None:
        """按固定顺序切到下一个模式并上报。"""
        index = MODE_CYCLE.index(self._mode)
        self.set_mode(MODE_CYCLE[(index + 1) % len(MODE_CYCLE)])
        self.mode_changed.emit(self._mode)

    def _on_quality_changed(self, index: int) -> None:
        """上报下拉框选中的档位(``None`` 表示自动)。"""
        self.quality_changed.emit(self.quality_combo.itemData(index))

    def _on_slider_pressed(self) -> None:
        """开始拖动:暂停被位置回写覆盖。"""
        self._seeking = True

    def _on_slider_released(self) -> None:
        """拖动结束:把目标位置上报给编排层。"""
        self._seeking = False
        self.seek_requested.emit(self.position_slider.value())

    def _on_volume_changed(self, value: int) -> None:
        """把滑块百分比换算成 0.0 ~ 1.0 再上报。"""
        self.volume_changed.emit(value / 100.0)
