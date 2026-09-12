"""右侧可折叠的播放队列抽屉。

抽屉本身**不知道**"下一首放什么",它只做两件事:把上层给的队列渲染成列表、
把用户操作(跳转 / 移除 / 清空 / 折叠)转成信号。队列的真源在
:class:`~bilibili_music.audio.playback.PlaybackController`。

折叠的实现刻意不用 ``QSplitter``:折叠时把列表隐藏、把整个抽屉压到只够放标题栏的
宽度,比让 splitter 去记住两套尺寸简单得多,也不会在窗口缩放时把主列表挤变形。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.queue import QueueItem
from ..icons import Palette, get_icon, palette
from .track_list import TrackList

__all__ = [
    "COLLAPSED_WIDTH",
    "EXPANDED_MIN_WIDTH",
    "QueueDrawer",
]

#: 折叠后只保留标题栏时的抽屉宽度(像素)。
COLLAPSED_WIDTH = 112

#: 展开时的最小宽度;再窄列就挤成一团了。
EXPANDED_MIN_WIDTH = 280

#: 抽屉里表格的列标题。
_COLUMNS = ("标题", "UP主", "时长")


class QueueDrawer(QWidget):
    """播放队列面板:标题栏(数量 / 清空 / 折叠) + 曲目列表。

    信号:
        row_activated(int): 双击队列第几行(跳到那一首)
        remove_requested(int): 请求移除第几行
        clear_requested(): 请求清空队列
        collapsed_changed(bool): 折叠状态变化(``True`` 表示已折叠)
    """

    row_activated = Signal(int)
    remove_requested = Signal(int)
    clear_requested = Signal()
    collapsed_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好标题栏与列表;初始是展开状态。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._palette = palette("light")
        self._collapsed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        root.addLayout(self._build_header())

        self.list = TrackList(_COLUMNS, stretch_column=0)
        self.list.row_activated.connect(self.row_activated.emit)
        self.list.row_menu_requested.connect(self._on_row_menu)
        root.addWidget(self.list, 1)

        self.setMinimumWidth(EXPANDED_MIN_WIDTH)

    # ------------------------------------------------------------ 构建界面

    def _build_header(self) -> QHBoxLayout:
        """建"折叠按钮 + 数量 + 清空"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(4)

        self.toggle_button = QPushButton("队列")
        self.toggle_button.setToolTip("折叠/展开播放队列")
        self.toggle_button.setIcon(
            get_icon("list", self._palette.text, 16, disabled_color=self._palette.disabled)
        )
        self.toggle_button.clicked.connect(self.toggle_collapsed)

        self.count_label = QLabel("0 首")
        self.count_label.setStyleSheet(f"color: {self._palette.muted};")

        self.clear_button = QPushButton("清空")
        self.clear_button.setToolTip("清空播放队列")
        self.clear_button.clicked.connect(self.clear_requested.emit)

        row.addWidget(self.toggle_button)
        row.addWidget(self.count_label)
        row.addStretch(1)
        row.addWidget(self.clear_button)
        return row

    # ------------------------------------------------------------ 上层推状态

    def set_items(self, items: Sequence[QueueItem], current_index: int = -1) -> None:
        """整体替换列表内容并高亮当前项。

        Args:
            items: 队列内容(插入顺序)。
            current_index: 当前项在 ``items`` 里的下标;``-1`` 表示没有当前项。
        """
        self.list.set_rows(
            [
                (item.title, item.subtitle, item.page.duration_text if item.page else "")
                for item in items
            ]
        )
        self.list.set_highlight(current_index)
        self.count_label.setText(f"{len(items)} 首")

    def set_current(self, current_index: int) -> None:
        """只更新高亮,不重建列表。

        切歌时用这个而不是 :meth:`set_items`:队列内容没变,重建整表会把用户的滚动
        位置和选中行一起冲掉。

        Args:
            current_index: 当前项下标;``-1`` 表示没有当前项。
        """
        self.list.set_highlight(current_index)

    def set_collapsed(self, collapsed: bool) -> None:
        """切换折叠状态。

        Args:
            collapsed: ``True`` 折叠(只留标题栏),``False`` 展开。
        """
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.list.setVisible(not collapsed)
        self.clear_button.setVisible(not collapsed)
        self.count_label.setVisible(not collapsed)
        if collapsed:
            self.setMinimumWidth(0)
            self.setMaximumWidth(COLLAPSED_WIDTH)
        else:
            self.setMaximumWidth(16777215)  # Qt 的 QWIDGETSIZE_MAX
            self.setMinimumWidth(EXPANDED_MIN_WIDTH)
        self.toggle_button.setToolTip("展开播放队列" if collapsed else "折叠播放队列")
        self.collapsed_changed.emit(collapsed)

    def toggle_collapsed(self) -> None:
        """在折叠与展开之间切换。"""
        self.set_collapsed(not self._collapsed)

    @property
    def collapsed(self) -> bool:
        """当前是否处于折叠状态。"""
        return self._collapsed

    def apply_palette(self, colors: Palette) -> None:
        """套用一套主题配色(数量文字的灰色与折叠按钮的图标色)。

        Args:
            colors: 来自 :func:`bilibili_music.ui.theme.apply_theme` 的调色板。
        """
        self._palette = colors
        self.count_label.setStyleSheet(f"color: {self._palette.muted};")
        self.toggle_button.setIcon(
            get_icon(
                "list",
                self._palette.text,
                16,
                disabled_color=self._palette.disabled,
            )
        )

    # ------------------------------------------------------------ 内部槽

    def _on_row_menu(self, row: int, position: QPoint) -> None:
        """右键菜单:只提供"从队列移除"。

        移除走右键而不是常驻按钮:队列里每行都挂个删除按钮会让列表非常吵。
        """
        menu = QMenu(self)
        action = menu.addAction("从队列移除")
        if menu.exec(position) is action:
            self.remove_requested.emit(row)
