"""底部播放条:封面、曲目信息、传输控件、进度、分P、音质与音量。

控件**不自己保存播放状态**。``set_*`` 方法由上层把状态推下来,信号把用户操作送上去,
这样"当前在播什么、在播第几秒"只有一个真源(``audio.playback``),不会出现
"界面显示在播、实际已经停了"这种两边各存一份状态导致的错位。

版式(对着设计稿):左边封面 + 曲名/UP主,中间上面一行传输控件、下面一行进度,
右边上面是"分P选择器 + 音质下拉"、下面是音量。分P选择器挨着音质下拉:两者都属于
"这一首怎么播",同处一行既贴着播放控制按钮的右侧,又不会挤到中间的进度条
(多P合集在队列里只占一行,换分P的入口见 :mod:`.page_selector`)。

两个空按钮的处理见 :meth:`_build_like_button` 与 :meth:`_build_expand_button` ——
**能点的东西必须真的有用**,装饰性的假按钮一律禁用并说明原因。
"""

from __future__ import annotations

from collections.abc import Sequence

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

from ...core.models import Page, format_duration
from ...core.queue import PlayMode
from ..icons import DARK, get_icon, render_pixmap
from ..pixmaps import cover_pixmap
from .elided_label import ElidedLabel
from .page_selector import PageSelector

__all__ = [
    "BUTTON_ICON_SIZE",
    "COVER_SIZE",
    "MODE_CYCLE",
    "QUALITY_CHOICES",
    "PlayerBar",
]

#: 播放控件图标的逻辑尺寸。
BUTTON_ICON_SIZE = 22

#: 封面缩略图的边长(正方形)。
COVER_SIZE = 48

#: 曲目信息区的固定宽度(像素)。固定住是为了让长标题**省略**而不是把中间的进度条挤扁。
INFO_WIDTH = 176

#: 圆形播放按钮的直径(像素)。
PLAY_BUTTON_SIZE = 44

#: 其余传输按钮的尺寸(像素)。
TRANSPORT_BUTTON_SIZE = 32

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
        page_selected(int): 分P已选,携带目标分P序号(从 1 开始)
        seek_requested(int): 用户拖完进度条,携带目标毫秒
        volume_changed(float): 音量滑块变化,携带 0.0 ~ 1.0
        queue_toggled(bool): 队列开关被点,携带期望的可见性
    """

    play_toggled = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    mode_changed = Signal(object)
    quality_changed = Signal(object)
    page_selected = Signal(int)
    seek_requested = Signal(int)
    volume_changed = Signal(float)
    queue_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好所有控件并接上内部信号。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("PlayerBar")
        self._mode = PlayMode.SEQUENCE
        #: 拖动进度条期间不要被播放位置回写打断
        self._seeking = False
        self._duration_ms = 0
        #: 记住播放态与封面,**只为了换状态时能重绘**。它们不是播放状态的真源
        #: (真源在 audio.playback),这里存的是"上次画成什么样"
        self._playing = False
        self._cover: QPixmap | None = None

        self._build_ui()
        self.set_playing(False)
        self.set_mode(self._mode)

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        """组装封面、曲目信息、传输控件、进度、音质与音量。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(14)

        self.cover = QLabel()
        self.cover.setObjectName("CoverThumb")
        self.cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        root.addWidget(self.cover)
        self.set_cover(None)

        root.addWidget(self._build_info())
        root.addWidget(self._build_like_button())

        center = QVBoxLayout()
        center.setSpacing(10)
        center.addStretch(1)
        center.addLayout(self._build_transport_row())
        center.addLayout(self._build_progress_row())
        center.addStretch(1)
        root.addLayout(center, 1)

        root.addLayout(self._build_right_column())

    def _build_info(self) -> QWidget:
        """建"曲名 + UP主"这一块。"""
        box = QWidget()
        box.setFixedWidth(INFO_WIDTH)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addStretch(1)

        self.title_label = ElidedLabel("未播放")
        self.title_label.setObjectName("TrackTitle")
        self.subtitle_label = ElidedLabel("")
        self.subtitle_label.setObjectName("TrackSubtitle")

        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        layout.addStretch(1)
        return box

    def _build_like_button(self) -> QPushButton:
        """建"喜欢"按钮:设计稿里有它,但"喜欢"要落到歌单上,而歌单功能还没做。

        做成**禁用**而不是"点了没反应":禁用的按钮一眼就能看出"现在用不了",
        比一个看起来能点、点完什么都不发生的按钮诚实。
        """
        self.like_button = QPushButton()
        self.like_button.setObjectName("TransportButton")
        self.like_button.setFixedSize(TRANSPORT_BUTTON_SIZE, TRANSPORT_BUTTON_SIZE)
        self.like_button.setIconSize(QSize(18, 18))
        self.like_button.setIcon(get_icon("heart", DARK.disabled, 18))
        self.like_button.setToolTip("喜欢(歌单功能开发中)")
        self.like_button.setEnabled(False)
        return self.like_button

    def _build_transport_row(self) -> QHBoxLayout:
        """建"模式 / 上一首 / 播放 / 下一首 / 队列"这一行(整行居中)。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)

        self.mode_button = self._icon_button("repeat", "顺序播放(点击切换)")
        self.mode_button.clicked.connect(self._on_mode_clicked)

        self.previous_button = self._icon_button("prev", "上一首")
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.play_button = QPushButton()
        self.play_button.setObjectName("PlayButton")
        self.play_button.setFixedSize(PLAY_BUTTON_SIZE, PLAY_BUTTON_SIZE)
        self.play_button.setIconSize(QSize(22, 22))
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.clicked.connect(self.play_toggled.emit)

        self.next_button = self._icon_button("next", "下一首")
        self.next_button.clicked.connect(self.next_requested.emit)

        self.queue_button = self._icon_button("list", "显示/隐藏播放队列")
        self.queue_button.setCheckable(True)
        # 选中态要换图标颜色:SVG 是栅格化成位图后染色的,样式表管不到它
        self.queue_button.toggled.connect(self._on_queue_toggled)

        for button in (
            self.mode_button,
            self.previous_button,
            self.play_button,
            self.next_button,
            self.queue_button,
        ):
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _build_progress_row(self) -> QHBoxLayout:
        """建"当前时间 / 进度条 / 总时长"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.position_label = QLabel("0:00")
        self.position_label.setObjectName("MutedLabel")
        self.duration_label = QLabel("0:00")
        self.duration_label.setObjectName("MutedLabel")
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderPressed.connect(self._on_slider_pressed)
        self.position_slider.sliderReleased.connect(self._on_slider_released)

        row.addWidget(self.position_label)
        row.addWidget(self.position_slider, 1)
        row.addWidget(self.duration_label)
        return row

    def _build_right_column(self) -> QVBoxLayout:
        """建右侧一列:上面"分P选择器 + 音质下拉",下面音量。

        分P选择器与音质下拉同处一行、排在音质**之前**:两者都是"这一首怎么播"的控制,
        放在播放控制按钮右侧既不打断中间的进度条,也和设计稿一致。两个控件高度不同
        (36 / 30),所以显式写 ``AlignVCenter`` —— 布局默认会把矮的那个顶到行顶。
        """
        column = QVBoxLayout()
        column.setSpacing(8)
        column.addStretch(1)

        self.page_selector = PageSelector()
        self.page_selector.page_selected.connect(self.page_selected.emit)

        self.quality_combo = QComboBox()
        self.quality_combo.setObjectName("QualityCombo")
        self.quality_combo.setFixedWidth(118)
        for label, quality_id in QUALITY_CHOICES:
            self.quality_combo.addItem(label, quality_id)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(8)
        quality_row.addStretch(1)
        quality_row.addWidget(self.page_selector, 0, Qt.AlignmentFlag.AlignVCenter)
        quality_row.addWidget(self.quality_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(quality_row)
        column.addLayout(self._build_volume_row())
        column.addStretch(1)
        return column

    def _build_volume_row(self) -> QHBoxLayout:
        """建"音量图标 + 滑块 + 展开"这一行。

        音量图标是**标签**而不是按钮:B站播放器里那颗喇叭点了要静音,静音属于新的
        播放行为,不在这一轮(只改版式)的范围里;做成标签就只表达"当前音量状态"。
        """
        row = QHBoxLayout()
        row.setSpacing(6)

        self.volume_icon = QLabel()
        self.volume_icon.setFixedSize(18, 18)
        self.volume_icon.setPixmap(render_pixmap("volume", DARK.muted, 18))

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setObjectName("VolumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(110)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)

        self.expand_button = QPushButton()
        self.expand_button.setObjectName("TransportButton")
        self.expand_button.setFixedSize(TRANSPORT_BUTTON_SIZE, TRANSPORT_BUTTON_SIZE)
        self.expand_button.setIconSize(QSize(16, 16))
        self.expand_button.setIcon(get_icon("expand", DARK.disabled, 16))
        self.expand_button.setToolTip("全屏播放(尚未实现)")
        self.expand_button.setEnabled(False)

        row.addStretch(1)
        row.addWidget(self.volume_icon)
        row.addWidget(self.volume_slider)
        row.addWidget(self.expand_button)
        return row

    def _icon_button(self, icon_name: str, tooltip: str) -> QPushButton:
        """造一个只放图标的传输按钮。

        Args:
            icon_name: 图标名(``resources/icons`` 下的文件名)。
            tooltip: 中文提示,播放控件不写文字,全靠它表达含义。

        Returns:
            固定尺寸的按钮。
        """
        button = QPushButton()
        button.setObjectName("TransportButton")
        button.setFixedSize(TRANSPORT_BUTTON_SIZE, TRANSPORT_BUTTON_SIZE)
        button.setIconSize(QSize(BUTTON_ICON_SIZE, BUTTON_ICON_SIZE))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setIcon(self._themed_icon(icon_name, DARK.text))
        return button

    def _themed_icon(self, icon_name: str, color: str) -> QIcon:
        """按图标名与颜色取一个带禁用态的图标。"""
        return get_icon(
            icon_name,
            color,
            BUTTON_ICON_SIZE,
            disabled_color=DARK.disabled,
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
        # 圆形按钮底色是强调色,所以图标要用叠在强调色上的白,而不是普通文字色
        self.play_button.setIcon(get_icon(name, DARK.on_accent, 22))
        self.play_button.setToolTip(tooltip)

    def set_now_playing(self, title: str, subtitle: str) -> None:
        """更新曲目标题与副标题。

        Args:
            title: 主标题(多P时是分P标题)。
            subtitle: 副标题(UP主、音质等)。
        """
        self.title_label.setText(title or "未播放")
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
        color = DARK.muted if self._mode is PlayMode.SEQUENCE else DARK.accent
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

    def set_pages(self, pages: Sequence[Page], current_index: int = 0) -> None:
        """把"当前视频的分P列表 + 正在播的那一P"推给分P选择器。

        Args:
            pages: 当前视频的分P列表;空序列表示没有当前视频(或详情还没补全)。
            current_index: 正在播的分P序号,从 1 开始;``0`` 表示没有当前视频。
        """
        self.page_selector.set_pages(pages, current_index)

    def set_page_menu_avoid_widget(self, widget: QWidget | None) -> None:
        """指定分P菜单**不该压住**的控件(实际用法是右侧的播放队列面板)。

        播放条看不到队列面板(那是主窗口组装的),所以引用由上层递进来。

        Args:
            widget: 要避开的控件;``None`` 表示不需要避让。
        """
        self.page_selector.set_avoid_widget(widget)

    def set_queue_visible(self, visible: bool) -> None:
        """同步队列开关的选中态(不触发 ``queue_toggled``)。

        播放条与侧栏各有一个队列开关,必须显示同一个状态。

        Args:
            visible: 队列面板当前是否可见。
        """
        if self.queue_button.isChecked() == bool(visible):
            return
        self.queue_button.blockSignals(True)
        self.queue_button.setChecked(bool(visible))
        self.queue_button.blockSignals(False)

    def set_cover(self, pixmap: QPixmap | None) -> None:
        """设置封面缩略图;``None`` 时用占位图。

        Args:
            pixmap: 已下载好的封面;传 ``None`` 表示没有封面(或下载失败)。
        """
        self._cover = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self.cover.setPixmap(cover_pixmap(self._cover, COVER_SIZE))

    @property
    def has_cover(self) -> bool:
        """当前显示的是真封面还是占位图。

        封面槽位**永远**有图(没有就画占位图),所以光看 ``cover.pixmap()`` 非空判断不出
        "到底有没有封面";界面自检与测试都需要这个明确的答案。
        """
        return self._cover is not None

    def set_volume(self, volume: float) -> None:
        """把音量滑块同步到指定值(不触发 ``volume_changed``)。

        Args:
            volume: 0.0 ~ 1.0。
        """
        value = int(round(min(1.0, max(0.0, volume)) * 100))
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(value)
        self.volume_slider.blockSignals(False)
        self._render_volume_icon(value / 100.0)

    @property
    def mode(self) -> PlayMode:
        """当前显示的播放模式。"""
        return self._mode

    def _render_volume_icon(self, volume: float) -> None:
        """按音量大小换喇叭图标(0 的时候是静音图标)。"""
        name = "volume-mute" if volume <= 0.001 else "volume"
        self.volume_icon.setPixmap(render_pixmap(name, DARK.muted, 18))

    # ------------------------------------------------------------ 内部槽

    def _on_mode_clicked(self) -> None:
        """按固定顺序切到下一个模式并上报。"""
        index = MODE_CYCLE.index(self._mode)
        self.set_mode(MODE_CYCLE[(index + 1) % len(MODE_CYCLE)])
        self.mode_changed.emit(self._mode)

    def _on_queue_toggled(self, checked: bool) -> None:
        """队列开关:先按新状态重染图标,再把期望的可见性上报。"""
        self.queue_button.setIcon(
            self._themed_icon("list", DARK.accent if checked else DARK.text)
        )
        self.queue_toggled.emit(bool(checked))

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
        """把滑块百分比换算成 0.0 ~ 1.0 再上报,并同步喇叭图标。"""
        self._render_volume_icon(value / 100.0)
        self.volume_changed.emit(value / 100.0)
