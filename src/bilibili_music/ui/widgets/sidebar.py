"""左侧导航栏:页面入口 + "我的歌单"分组。

侧栏是**纯导航控件**:它不知道点下去会发生什么,只把"用户点了哪个入口"变成信号。
"哪个入口对应哪一页、没实现的入口怎么处理"由 ``MainWindow`` 决定(``AGENTS.md``
第 4 节:UI 控件只做展示与事件转发)。

设计取舍
--------

* **导航项用互斥的 ``QPushButton`` 而不是 ``QListWidget``**:设计稿里的选中态是圆角
  胶囊 + 强调色文字,样式表能直接表达 ``:checked``,而列表项的选中态要跟
  ``::item:selected`` 与 ``QPalette`` 的选中色一起对付。
* **"播放队列"不在页面组里**:它不是一页,而是右侧队列面板的开关(设计稿里它既有
  侧栏入口,也有播放条上的按钮)。放进互斥组会导致"打开队列"顺手取消掉当前页面选中。
* **图标要手动按选中态重新染色**:SVG 是栅格化成位图之后再染色的,样式表的 ``:checked``
  只管文字颜色,管不到图标 —— 与 ``PlayerBar`` 的播放模式按钮同源。
* **两套互斥组(页面 / 歌单)**:歌单也是"选中一个"的语义,但选歌单要清掉页面选中态、
  选页面要清掉歌单选中态,所以分成两个组再手工互斥,比塞进一个大组清楚。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..icons import DARK, get_icon

__all__ = [
    "NAV_ITEMS",
    "PLAYLISTS",
    "SIDEBAR_WIDTH",
    "NavItem",
    "Sidebar",
]

#: 侧栏宽度(像素)。设计稿里是 172,这里给 176 让"我喜欢的音乐"这类长名字不至于挤到。
SIDEBAR_WIDTH = 176

#: 导航按钮里的图标尺寸(像素)。
_NAV_ICON = 18


@dataclass(frozen=True, slots=True)
class NavItem:
    """一个侧栏入口。

    Attributes:
        key: 内部标识,信号里传的就是它(界面文案可以改,key 不该跟着变)。
        label: 显示文字。
        icon: 图标名(``resources/icons`` 下的文件名)。
        is_page: ``True`` 表示它切换中间的内容页;``False`` 表示它是个面板开关
            (只有"播放队列"是这种)。
    """

    key: str
    label: str
    icon: str
    is_page: bool = True


#: 侧栏入口。顺序与设计稿一致;"发现""本地缓存"对应的功能分属路线图 M3 / M2,
#: 当前点开是占位页(见 ``widgets/placeholder.py``)。
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("discover", "发现", "home"),
    NavItem("results", "搜索结果", "search"),
    NavItem("cache", "本地缓存", "download"),
    NavItem("queue", "播放队列", "list", is_page=False),
)

#: "我的歌单"分组的条目 ``(名称, 图标名)``。
#:
#: **只有名字,没有数量**。设计稿里每个歌单后面跟着 "32 / 24 / 58" 这样的数字,那是
#: 示意数据;歌单功能(路线图 M5,当前冻结)还没实现,界面显示一个凭空编出来的数字比
#: 空着更糟。等真能读到歌单时再把数量填上。
PLAYLISTS: tuple[tuple[str, str], ...] = (
    ("我喜欢的音乐", "heart"),
    ("周杰伦", "music"),
    ("华语经典", "music"),
    ("深夜听歌", "music"),
    ("工作专注", "music"),
)


class Sidebar(QWidget):
    """左侧导航栏。

    信号:
        nav_selected(str): 点了某个入口,携带 :attr:`NavItem.key`
            (播放队列开关也走它,key 是 ``"queue"``,按钮的 check 状态即期望的可见性)
        playlist_selected(str): 点了某个歌单,携带歌单名
        create_playlist_requested(): 点了"我的歌单"旁边的 "+"
    """

    nav_selected = Signal(str)
    playlist_selected = Signal(str)
    create_playlist_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好导航区与歌单区;初始没有选中任何入口。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)

        self._page_group = QButtonGroup(self)
        self._page_group.setExclusive(True)
        self._playlist_group = QButtonGroup(self)
        self._playlist_group.setExclusive(True)
        #: key -> 按钮;测试与 ``MainWindow`` 都要按 key 找按钮
        self.nav_buttons: dict[str, QPushButton] = {}
        #: 歌单名 -> 按钮
        self.playlist_buttons: dict[str, QPushButton] = {}
        #: 按钮 -> 图标名,用于按选中态重新染色
        self._nav_icons: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(2)

        for item in NAV_ITEMS:
            layout.addWidget(self._nav_button(item))

        layout.addSpacing(10)
        layout.addLayout(self._build_playlist_header())
        for name, icon in PLAYLISTS:
            layout.addWidget(self._playlist_button(name, icon))
        layout.addStretch(1)

    # ------------------------------------------------------------ 构建界面

    def _nav_button(self, item: NavItem) -> QPushButton:
        """造一个导航按钮,并按它是不是页面挂到对应的按钮组上。"""
        button = QPushButton(item.label)
        button.setObjectName("NavButton")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(_NAV_ICON, _NAV_ICON))
        self.nav_buttons[item.key] = button
        self._nav_icons[item.key] = item.icon
        _paint_nav_icon(button, item.icon, False)
        # 图标颜色跟着选中态走,见模块 docstring
        button.toggled.connect(
            lambda checked, target=button, name=item.icon: _paint_nav_icon(
                target, name, checked
            )
        )
        button.clicked.connect(lambda _=False, key=item.key: self._on_nav_clicked(key))
        if item.is_page:
            self._page_group.addButton(button)
        return button

    def _build_playlist_header(self) -> QHBoxLayout:
        """建"我的歌单"标签 + 新建按钮这一行。"""
        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(12, 0, 4, 0)

        label = QLabel("我的歌单")
        label.setObjectName("SectionLabel")
        row.addWidget(label)
        row.addStretch(1)

        self.create_button = QPushButton()
        self.create_button.setObjectName("IconGhostButton")
        self.create_button.setFixedSize(22, 22)
        self.create_button.setIconSize(QSize(14, 14))
        self.create_button.setIcon(get_icon("plus", DARK.muted, 14))
        self.create_button.setToolTip("新建歌单(功能开发中)")
        self.create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_button.clicked.connect(self.create_playlist_requested.emit)
        row.addWidget(self.create_button)
        return row

    def _playlist_button(self, name: str, icon: str) -> QPushButton:
        """造一个歌单按钮(点击进入占位页)。"""
        button = QPushButton(name)
        button.setObjectName("PlaylistRow")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(16, 16))
        button.setIcon(get_icon(icon, DARK.muted, 16))
        button.setToolTip(f"歌单「{name}」(功能开发中)")
        button.clicked.connect(
            lambda _=False, target=name: self._on_playlist_clicked(target)
        )
        self._playlist_group.addButton(button)
        self.playlist_buttons[name] = button
        return button

    # ------------------------------------------------------------ 上层推状态

    def set_active_page(self, key: str) -> None:
        """把某个页面入口标成选中(不改内容、不发信号)。

        用于"内容页被别的路径切走了,侧栏要跟上"的场景。

        Args:
            key: :data:`NAV_ITEMS` 里的键;未知键或不存在的按钮会被忽略。
        """
        button = self.nav_buttons.get(key)
        if button is None:
            return
        was_blocked = button.blockSignals(True)
        button.setChecked(True)
        button.blockSignals(was_blocked)
        # 阻塞信号顺手挡掉了 toggled,图标得自己补一次
        _paint_nav_icon(button, self._nav_icons[key], True)
        _uncheck_all(self._playlist_group)

    def set_queue_visible(self, visible: bool) -> None:
        """同步"播放队列"开关的选中态(不触发 ``nav_selected``)。

        播放条上也有队列开关,两处必须显示同一个状态 —— 这条路径用来对齐它们。

        Args:
            visible: 队列面板当前是否可见。
        """
        button = self.nav_buttons.get("queue")
        if button is None or button.isChecked() == bool(visible):
            return
        was_blocked = button.blockSignals(True)
        button.setChecked(bool(visible))
        button.blockSignals(was_blocked)
        _paint_nav_icon(button, self._nav_icons["queue"], bool(visible))

    # ------------------------------------------------------------ 内部槽

    def _on_nav_clicked(self, key: str) -> None:
        """点了导航入口:页面类入口要点亮,并清掉歌单的选中态。"""
        item = next((entry for entry in NAV_ITEMS if entry.key == key), None)
        if item is not None and item.is_page:
            _uncheck_all(self._playlist_group)
        self.nav_selected.emit(key)

    def _on_playlist_clicked(self, name: str) -> None:
        """点了歌单:清掉页面入口的选中态,再上报。"""
        _uncheck_all(self._page_group)
        queue_button = self.nav_buttons.get("queue")
        if queue_button is not None and queue_button.isChecked():
            queue_button.setChecked(False)
        self.playlist_selected.emit(name)


def _paint_nav_icon(button: QPushButton, icon: str, active: bool) -> None:
    """按选中态给导航按钮的图标换色。

    Args:
        button: 目标按钮。
        icon: 图标名。
        active: 是否处于选中态(选中用强调色,否则用弱化色)。
    """
    button.setIcon(get_icon(icon, DARK.accent if active else DARK.muted, _NAV_ICON))


def _uncheck_all(group: QButtonGroup) -> None:
    """清掉一个互斥按钮组里所有按钮的选中态。

    ``QButtonGroup`` 没有"取消全部选中"的接口,而互斥组里又**不允许**把已选中的按钮
    再点一遍取消;只能临时关掉互斥、逐个清、再打开。

    Args:
        group: 目标按钮组。
    """
    group.setExclusive(False)
    for button in group.buttons():
        button.setChecked(False)
    group.setExclusive(True)
