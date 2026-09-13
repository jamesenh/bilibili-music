"""右侧播放队列面板。

面板本身**不知道**"下一首放什么",它只做两件事:把上层给的队列渲染成列表、把用户操作
(跳转 / 移除 / 清空)转成信号。队列的真源在
:class:`~bilibili_music.audio.playback.PlaybackController`。

与旧版的差别:不再自带"折叠"按钮。折叠原本是为了把面板收窄腾地方,而设计稿把"显示/
隐藏队列"这件事放在了播放条与侧栏的队列开关上(见 ``player_bar.PlayerBar.queue_toggled``)
—— 同一个动作有两个入口已经够了,再多一个折叠按钮只会让人猜"折叠和隐藏有什么不一样"。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.queue import QueueItem
from ..cover_loader import CoverLoader
from ..icons import DARK, get_icon
from .track_list import TrackList, TrackRow, split_title_prefix

__all__ = [
    "PANEL_MIN_WIDTH",
    "QueueDrawer",
]

#: 面板最小宽度(像素);再窄标题与时长就挤在一起了。
PANEL_MIN_WIDTH = 268

#: 面板里列表的列标题。第 0 / 1 列由 ``TrackList`` 自己填(序号、封面 + 曲名)。
_COLUMNS = ("#", "曲目", "时长")


class QueueDrawer(QWidget):
    """播放队列面板:标题栏(数量 / 清空) + 曲目列表。

    信号:
        row_activated(int): 双击队列第几行(跳到那一首)
        remove_requested(int): 请求移除第几行
        clear_requested(): 请求清空队列

    Args:
        covers: 封面加载器;``None`` 表示队列行不取封面(测试可以省略)。
        parent: Qt 父对象。
    """

    row_activated = Signal(int)
    remove_requested = Signal(int)
    clear_requested = Signal()

    def __init__(
        self,
        covers: CoverLoader | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """建好标题栏与列表。

        Args:
            covers: 封面加载器。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("QueuePanel")
        self.setMinimumWidth(PANEL_MIN_WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        root.addLayout(self._build_header())

        self.list = TrackList(_COLUMNS, covers=covers, centered=(2,))
        self.list.row_activated.connect(self.row_activated.emit)
        self.list.row_menu_requested.connect(self._on_row_menu)
        root.addWidget(self.list, 1)

    # ------------------------------------------------------------ 构建界面

    def _build_header(self) -> QHBoxLayout:
        """建"标题 + 数量 + 清空"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(6)

        self.title_label = QLabel("播放队列")
        self.title_label.setObjectName("PanelTitle")

        self.count_label = QLabel("0 首")
        self.count_label.setObjectName("MutedLabel")

        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("GhostTextButton")
        self.clear_button.setIcon(get_icon("trash", DARK.muted, 14))
        self.clear_button.setToolTip("清空播放队列")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear_requested.emit)

        row.addWidget(self.title_label)
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
        self.list.set_tracks([self._row_of(item) for item in items])
        self.list.set_highlight(current_index)
        self.count_label.setText(f"{len(items)} 首")

    def set_current(self, current_index: int) -> None:
        """只更新高亮,不重建列表。

        切歌时用这个而不是 :meth:`set_items`:队列内容没变,重建整表会把用户的滚动位置
        和封面一起冲掉,还要为同一批封面再走一遍取图流程。

        Args:
            current_index: 当前项下标;``-1`` 表示没有当前项。
        """
        self.list.set_highlight(current_index)

    def set_cover(self, url: str, pixmap: QPixmap) -> None:
        """把取到的封面转给列表(转接给 ``TrackList`` 的同名方法)。

        Args:
            url: 封面地址。
            pixmap: 已下载好的原图。
        """
        self.list.set_cover(url, pixmap)

    @staticmethod
    def _row_of(item: QueueItem) -> TrackRow:
        """把队列项摊成列表行。

        曲名用 ``QueueItem.title``:多P合集里它已经是分P标题(领域铁律),不该在这里
        再判断一次"是不是合集"。

        Args:
            item: 队列项。

        Returns:
            可以直接交给 ``TrackList`` 的展示数据。
        """
        duration = item.page.duration_text if item.page is not None else ""
        prefix, title = split_title_prefix(item.title)
        return TrackRow(
            title=title,
            prefix=prefix,
            subtitle=item.subtitle,
            cover_url=item.video.cover_https,
            columns=(duration,),
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
