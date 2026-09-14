"""自绘标题栏:应用图标 + 搜索框 + 搜索按钮 + 窗口按钮。

窗口是无边框的(见 :mod:`.window_frame`),所以"标题栏"不再是系统画的那一条,而是
这个普通控件。它只负责三件事:把搜索动作变成信号、把窗口按钮变成信号、在空白处拖动
窗口 —— 搜索**怎么做**由 ``MainWindow`` 决定。

拖动与双击最大化用 Qt 的 ``QWindow.startSystemMove()``:让**平台**去做移动与吸附
(Windows 的 Aero Snap、拖到屏幕边缘自动铺满),比自己在 ``mouseMoveEvent`` 里算
坐标靠谱得多,也不会在缩放/多屏下错位。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ..icons import DARK, get_icon
from ..pixmaps import logo_pixmap
from .sidebar import SIDEBAR_WIDTH

__all__ = ["TITLE_BAR_HEIGHT", "TitleBar"]

#: 标题栏高度(像素)。和 :data:`~bilibili_music.ui.widgets.sidebar.SIDEBAR_WIDTH` 一起
#: 决定"左上角图标正对着侧栏"的观感,所以两处都写成常量。
TITLE_BAR_HEIGHT = 64

#: 标题栏这一行的左边距(像素)。
_TITLE_BAR_LEFT_MARGIN = 14

#: 应用图标尺寸(像素)。
_LOGO_SIZE = 28

#: 图标所在方块的宽度(像素)。
#:
#: 由"侧栏宽度 - 本行的左边距"**算出来**,目的是让搜索框的左边缘与内容区的左边缘对齐
#: —— 设计稿里这两条竖线是齐的。以前这里写死 162(对应 176 宽的侧栏),侧栏一加宽就
#: 会悄悄错位,所以改成从 :data:`~bilibili_music.ui.widgets.sidebar.SIDEBAR_WIDTH` 推导。
_LOGO_BOX_WIDTH = SIDEBAR_WIDTH - _TITLE_BAR_LEFT_MARGIN

#: 窗口按钮尺寸(像素)。
_WINDOW_BUTTON = 30


class TitleBar(QWidget):
    """窗口顶部那一条。

    信号:
        search_requested(): 回车或点了"搜索"
        minimize_requested(): 点了最小化
        maximize_requested(): 点了最大化/还原(双击标题栏空白处也一样)
        close_requested(): 点了关闭
    """

    search_requested = Signal()
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好图标、搜索框、搜索按钮与三个窗口按钮。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_TITLE_BAR_LEFT_MARGIN, 0, 8, 0)
        layout.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
        self.logo_label.setPixmap(logo_pixmap(_LOGO_SIZE))
        self.logo_label.setToolTip("BiliMusic")

        logo_box = QWidget()
        logo_box.setFixedWidth(_LOGO_BOX_WIDTH)
        logo_layout = QHBoxLayout(logo_box)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        logo_layout.addWidget(self.logo_label)
        logo_layout.addStretch(1)
        layout.addWidget(logo_box)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchInput")
        self.search_input.setPlaceholderText("搜索 B站音乐视频,例如:周杰伦 MV")
        self.search_input.setClearButtonEnabled(True)
        # 搜索图标做成 QLineEdit 的前置 action:这样它随输入框一起圆角裁切,不用手工
        # 算位置;样式表里的左内边距(34px)就是给它留的位置
        self.search_input.addAction(
            get_icon("search", DARK.muted, 16), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search_input.returnPressed.connect(self.search_requested.emit)
        layout.addWidget(self.search_input, 1)

        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("PrimaryButton")
        self.search_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_button.clicked.connect(self.search_requested.emit)
        layout.addWidget(self.search_button)

        # 搜索框右侧留一段空白,让"搜索"按钮不贴着窗口按钮(设计稿里两者之间有空档)
        layout.addStretch(1)

        self.minimize_button = self._window_button(
            "minimize", "最小化", self.minimize_requested
        )
        layout.addWidget(self.minimize_button)
        self.maximize_button = self._window_button(
            "maximize", "最大化", self.maximize_requested
        )
        layout.addWidget(self.maximize_button)
        self.close_button = self._window_button("close", "关闭", self.close_requested)
        # 关闭键悬停变红:这是窗口按钮的通用约定,不给的话最容易点错的那颗最不显眼
        self.close_button.setProperty("closing", True)
        layout.addWidget(self.close_button)

    # ------------------------------------------------------------ 构建界面

    def _window_button(self, icon: str, tooltip: str, signal) -> QPushButton:  # noqa: ANN001
        """造一个窗口按钮(图标 + 提示,点击发指定信号)。

        Args:
            icon: 图标名。
            tooltip: 中文提示。
            signal: 点击时要发的信号(``bound signal``)。

        Returns:
            造好的按钮。
        """
        button = QPushButton()
        button.setObjectName("WindowButton")
        button.setFixedSize(_WINDOW_BUTTON, _WINDOW_BUTTON)
        button.setIconSize(QSize(16, 16))
        button.setIcon(get_icon(icon, DARK.muted, 16))
        button.setToolTip(tooltip)
        button.clicked.connect(signal.emit)
        return button

    # ------------------------------------------------------------ 上层推状态

    def set_maximized(self, maximized: bool) -> None:
        """按窗口是否已最大化切换按钮图标与提示。

        Args:
            maximized: 窗口当前是否最大化。
        """
        name = "restore" if maximized else "maximize"
        tooltip = "向下还原" if maximized else "最大化"
        self.maximize_button.setIcon(get_icon(name, DARK.muted, 16))
        self.maximize_button.setToolTip(tooltip)

    # ------------------------------------------------------------ 拖动窗口

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        """在标题栏空白处按下左键时,把移动交给平台去处理。

        子控件(搜索框、按钮)会先吃掉落在它们身上的事件,所以这里只会收到"点到了空白处"
        的情况 —— 点搜索框时不会莫名其妙把窗口拖着走。
        """
        if event.button() == Qt.MouseButton.LeftButton and self._start_system_move():
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        """双击标题栏空白处 = 最大化 / 还原(Windows 上的通用习惯)。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _start_system_move(self) -> bool:
        """请求平台开始移动窗口。

        Returns:
            是否成功交给平台;无边框窗口在部分平台(或离屏测试)上拿不到窗口句柄,此时
            返回 ``False``,调用方回落到默认处理。
        """
        handle = self.window().windowHandle()
        return bool(handle is not None and handle.startSystemMove())
