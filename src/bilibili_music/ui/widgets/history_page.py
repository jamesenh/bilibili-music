"""最近播放页:列出放过哪些歌,支持过滤、点播与从历史中删除。

这一页回答的是"我刚才听的是什么"。它**不自己读库**:数据由上层从
``core/history.PlayHistory`` 取出来推下去(``set_entries``),用户操作则变成信号发上去 ——
与本地缓存页同一条纪律(``AGENTS.md`` 第 4 节:UI 只做展示与事件转发)。

设计取舍
--------

* **与 :class:`~bilibili_music.ui.widgets.cache_page.CachePage` 同构**:两页都是"一列歌 +
  过滤框 + 统计 + 清空",复用同一个 :class:`~bilibili_music.ui.widgets.track_list.TrackList`
  与空态页。用户因此不需要为"最近播放"再学一套操作,代码也只剩"列不一样、删除语义不一样"
  这两处差异。
* **播放时间在行尾**:副标题留给 UP主(与缓存页同口径),时间单独占一列。放进副标题会
  把"谁唱的"和"什么时候听的"挤成一行,反而都看不清。
* **"从历史中删除"与"清空历史"都不动缓存文件**:这一页只是"听过什么"的流水,
  删一条记录不该顺手把用户下载好的音频删掉 —— 那是缓存页的事,且删除缓存不可逆。
  删除按钮的文案与确认框都把这一点写清楚。
* **过滤在本控件内做**:与缓存页同一个理由,它只是"按字符串挑行",不涉及业务判断。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.history import HistoryEntry
from ...core.models import format_duration, format_relative_time
from ..cover_loader import CoverLoader
from ..icons import DARK, get_icon
from .placeholder import PlaceholderPage
from .track_list import TrackList, TrackRow, split_title_prefix

__all__ = [
    "HistoryPage",
    "FILTER_WIDTH",
]

#: 过滤框的固定宽度(像素)。与缓存页同宽,两页切换时过滤框不会跳位置。
FILTER_WIDTH = 220

#: 列表的列。第 0 / 1 列由 ``TrackList`` 自己填(序号、封面 + 曲名)。
_COLUMNS = ("#", "曲目", "时长", "播放时间", "操作")

#: "时长""播放时间"两列居中(设计稿里短列不贴左边线)。
_CENTERED = (2, 3)

#: 操作列下标(行内 "+" 与 "⋮")。
_ACTION_COLUMN = 4


class HistoryPage(QWidget):
    """最近播放页:曲目列表 + 过滤框 + 首数 + 清空。

    信号:
        row_activated(int): 双击第几行(以**当前可见的**列表为准,过滤后行号会变)
        row_menu_requested(int, QPoint): 请求某行的右键菜单(操作列的"⋮"也会发它)
        add_requested(int): 点了某行的"+"(加入播放队列)
        remove_requested(int): 请求从历史中删掉某一行
        clear_requested(): 请求清空全部历史

    Args:
        covers: 封面加载器;``None`` 表示不取封面(测试可以省略)。
        parent: Qt 父对象。
    """

    row_activated = Signal(int)
    row_menu_requested = Signal(int, QPoint)
    add_requested = Signal(int)
    remove_requested = Signal(int)
    clear_requested = Signal()

    def __init__(
        self,
        covers: CoverLoader | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """建好标题行、列表与空态页。

        Args:
            covers: 封面加载器。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("CenterPanel")

        #: 全部记录(未过滤)。过滤只影响 :attr:`_visible`,不动这一份 ——
        #: 否则清空过滤框时就没有"原来的列表"可以恢复了。
        self._all: list[HistoryEntry] = []
        #: 当前真正显示出来的记录(过滤结果);行号信号里的行号是它的下标
        self._visible: list[HistoryEntry] = []
        #: 正在播放的 ``(bvid, cid)``;``None`` 表示没有
        self._playing: tuple[str, int] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())

        self.list = TrackList(
            _COLUMNS,
            covers=covers,
            action_column=_ACTION_COLUMN,
            centered=_CENTERED,
        )
        self.list.row_activated.connect(self.row_activated.emit)
        self.list.row_menu_requested.connect(self.row_menu_requested.emit)
        self.list.add_requested.connect(self.add_requested.emit)

        self.empty_page = PlaceholderPage()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.empty_page)
        layout.addWidget(self.stack, 1)

        self._render()

    # ------------------------------------------------------------ 构建界面

    def _build_header(self) -> QHBoxLayout:
        """建"标题 + 过滤框 + 首数 + 清空"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.title_label = QLabel("最近播放")
        self.title_label.setObjectName("PageTitle")

        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("FilterInput")
        self.filter_input.setFixedWidth(FILTER_WIDTH)
        self.filter_input.setPlaceholderText("按曲名或UP主过滤")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._on_filter_changed)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")

        self.clear_button = QPushButton("清空历史")
        self.clear_button.setObjectName("GhostTextButton")
        self.clear_button.setIcon(get_icon("trash", DARK.muted, 14))
        self.clear_button.setToolTip("删除全部播放记录(不影响已缓存的音频文件)")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear_requested.emit)

        row.addWidget(self.title_label)
        row.addStretch(1)
        row.addWidget(self.filter_input)
        row.addWidget(self.count_label)
        row.addWidget(self.clear_button)
        return row

    # ------------------------------------------------------------ 上层推状态

    def set_entries(self, entries: Sequence[HistoryEntry]) -> None:
        """整体替换内容。

        过滤条件**保留**:用户在列表里找一首歌时删掉一条记录,不该把已经打好的关键字
        清掉(与缓存页同一套行为)。

        Args:
            entries: 全部记录(未过滤),顺序由上层决定(最近播放的在前)。
        """
        self._all = list(entries)
        self._apply_filter()

    def set_playing(self, bvid: str, cid: int) -> None:
        """标记"正在播放的是哪一首"(按 ``bvid`` + ``cid`` 找行)。

        Args:
            bvid: 正在播放的视频 BV 号;空串表示没有在播的(清掉标记)。
            cid: 正在播放的分P cid。
        """
        self._playing = (bvid, cid) if bvid else None
        self._apply_playing()

    def entry_at(self, row: int) -> HistoryEntry | None:
        """取当前可见列表里某一行的记录;越界返回 ``None``。

        行号是**过滤之后**的下标 —— 信号里给的也是它,两者必须同一套口径。

        Args:
            row: 行号,0 起。

        Returns:
            对应的播放记录;行号越界时返回 ``None``。
        """
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def visible_entries(self) -> tuple[HistoryEntry, ...]:
        """当前显示出来的记录(过滤后的结果),供上层"整列变成队列"。"""
        return tuple(self._visible)

    def set_cover(self, url: str, pixmap: QPixmap) -> None:
        """把取到的封面转给列表(转接给 ``TrackList`` 的同名方法)。

        Args:
            url: 封面地址。
            pixmap: 已下载好的原图。
        """
        self.list.set_cover(url, pixmap)

    # ------------------------------------------------------------ 内部:渲染

    def _apply_filter(self) -> None:
        """按过滤框当前的内容重算可见记录并重绘。"""
        needle = self.filter_input.text().strip().lower()
        if not needle:
            self._visible = list(self._all)
        else:
            self._visible = [
                entry
                for entry in self._all
                if needle in entry.title.lower() or needle in entry.author.lower()
            ]
        self._render()

    def _render(self) -> None:
        """重绘列表、空态与统计数字。"""
        self.list.set_tracks([self._row_of(entry) for entry in self._visible])
        self._apply_playing()
        self._update_summary()
        self.stack.setCurrentWidget(self.list if self._visible else self.empty_page)

    def _update_summary(self) -> None:
        """更新"首数"与空态文案、清空按钮的可用性。"""
        if self._all and len(self._visible) != len(self._all):
            self.count_label.setText(f"{len(self._visible)} / {len(self._all)} 首")
        else:
            self.count_label.setText(f"{len(self._all)} 首" if self._all else "")
        self.clear_button.setEnabled(bool(self._all))

        if self._all:
            # 有记录但一行都没显示出来,那一定是过滤条件的锅 —— 文案要指向这一点,
            # 否则用户会以为历史丢了
            self.empty_page.set_content("没有匹配的播放记录", "换个关键字,或者清空过滤条件")
        else:
            self.empty_page.set_content(
                "还没有播放记录", "双击一首歌开始播放,这里就会留下记录"
            )

    def _apply_playing(self) -> None:
        """把"正在播放"标记刷到列表上(过滤后行号会变,所以每次重绘都要重算)。"""
        row = -1
        if self._playing is not None:
            bvid, cid = self._playing
            for index, entry in enumerate(self._visible):
                if entry.bvid == bvid and entry.cid == cid:
                    row = index
                    break
        self.list.set_highlight(row)

    @staticmethod
    def _row_of(entry: HistoryEntry) -> TrackRow:
        """把一条播放记录摊成列表行。

        曲名走 :func:`~bilibili_music.ui.widgets.track_list.split_title_prefix`(与搜索
        结果、缓存页同一套处理);副标题是 UP主,多P合集再补一个分P序号 —— 单P视频不显示
        ``P1``,与 :func:`~bilibili_music.core.models.track_subtitle` 同一口径;
        行尾的"播放时间"由 :func:`~bilibili_music.core.models.format_relative_time` 现算。

        Args:
            entry: 播放记录。

        Returns:
            可直接交给 ``TrackList`` 的展示数据。
        """
        prefix, title = split_title_prefix(entry.title)
        parts = [entry.author] if entry.author else []
        if entry.multipart:
            parts.append(f"P{entry.page_index}")
        return TrackRow(
            title=title,
            prefix=prefix,
            subtitle=" · ".join(parts),
            cover_url=entry.cover_url,
            columns=(
                format_duration(entry.duration),
                format_relative_time(entry.played_at),
            ),
        )

    # ------------------------------------------------------------ 内部槽

    def _on_filter_changed(self, _text: str) -> None:
        """过滤框内容变了:重算可见记录。

        参数没用到(状态在控件自己身上),但信号必须收下 —— ``textChanged`` 带的是文本。
        """
        self._apply_filter()
