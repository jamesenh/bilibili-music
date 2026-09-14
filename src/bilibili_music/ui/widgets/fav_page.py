"""收藏夹页:把 B站 收藏夹当歌单来浏览与播放。

这一页回答的是"我收藏的那些视频里,有哪些能当歌来听"。它**不自己发请求**:收藏夹内容由
``MainWindow`` 从 ``api`` 取回来推下去(:meth:`FavPage.set_items` /
:meth:`FavPage.append_items`),用户操作则变成信号发上去 —— 与其它控件同一条纪律
(``AGENTS.md`` 第 4 节:UI 只做展示与事件转发)。

设计取舍
--------

* **失效条目要看得见、但点不动**:实测一个收藏夹一页 20 条里就有 6 条已失效
  (``attr≠0``)。它们必须留在列表里(用户得知道自己收藏过什么),但要画成弱化行
  (``TrackRow.dimmed``),并且双击 / "+" / 右键**都不发播放类信号**,而是发
  :attr:`FavPage.unplayable_requested` 说明原因 —— 否则用户点一下就是一个必然失败的
  请求,还得自己猜为什么。
* **多P条目只显示分P数量,不展开分P列表**:条目里没有 ``cid``(实测),真要播必须再补
  一次详情。选哪一P交给播放条上已有的分P选择器(详情补全后它就能用),这一页不重复
  造一个选择器。
* **翻页用显式按钮**而不是"滚到底自动加载":收藏夹是**有限**列表(``ps`` 实测上限 20),
  "还有没有下一页"由 ``has_more`` 说了算;给一个看得见、点得动的出口,比隐式触发更好
  排查,也不会让滚动事件变成一个隐藏的请求源。
* **封面与播放高亮沿用既有接法**:封面走 :meth:`set_cover` 由上层转发(与 ``CachePage``
  一致),"正在播哪一行"由上层告知。收藏夹条目是**视频级**的,所以高亮按 ``bvid`` 找行,
  不像缓存页那样还要比 ``cid``。
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

from ...api.bilibili import FAV_ITEM_TYPE_VIDEO, FavItem
from ...core.models import format_duration
from ..cover_loader import CoverLoader
from .placeholder import PlaceholderPage
from .track_list import TrackList, TrackRow, split_title_prefix

__all__ = [
    "FAV_FILTER_WIDTH",
    "FavPage",
]

#: 过滤框的固定宽度(像素)。与本地缓存页同一个口径:它不该跟着窗口一起变宽。
FAV_FILTER_WIDTH = 220

#: 列表的列。第 0 / 1 列由 ``TrackList`` 自己填(序号、封面 + 标题)。
_COLUMNS = ("#", "视频信息", "UP主", "时长", "分P", "操作")

#: "时长""分P"两列居中。
_CENTERED = (3, 4)

#: 操作列下标(行内 "+" 与 "⋮")。
_ACTION_COLUMN = 5

#: 非视频条目(type=12 音频 / 21 合集)的说明文字。
_NOT_VIDEO_REASON = "这条不是视频稿件(音频或合集),当前版本还不支持播放"


class FavPage(QWidget):
    """收藏夹内容页:曲目列表 + 过滤框 + 分页。

    信号:
        row_activated(int): 双击某一行(以**当前可见的**列表为准,过滤后行号会变)
        row_menu_requested(int, QPoint): 请求某行的右键菜单(操作列的"⋮"也会发它)
        add_requested(int): 点了某行的"+"
        load_more_requested(): 点了"加载更多"
        unplayable_requested(str): 点了不能播的行,携带给用户看的中文原因

    Args:
        covers: 封面加载器;``None`` 表示不取封面(测试可以省略)。
        parent: Qt 父对象。
    """

    row_activated = Signal(int)
    row_menu_requested = Signal(int, QPoint)
    add_requested = Signal(int)
    load_more_requested = Signal()
    unplayable_requested = Signal(str)

    def __init__(
        self,
        covers: CoverLoader | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """建好标题行、列表、空态页与"加载更多"页脚。

        Args:
            covers: 封面加载器。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("CenterPanel")

        #: 当前收藏夹的全部条目(未过滤)
        self._all: list[FavItem] = []
        #: 当前真正显示出来的条目(过滤结果);行号信号里的行号是它的下标
        self._visible: list[FavItem] = []
        #: 收藏夹标题(空串表示还没选夹子)
        self._title = ""
        #: 当前收藏夹是否私密;私密记号显示在标题行 —— 用户得知道这一页不该被外人看到
        self._private = False
        #: 接口给的收藏夹总条数(可能大于已加载的条数)
        self._media_count = 0
        #: 还有没有下一页
        self._has_more = False
        #: 是否有请求在飞(挡住重复点"加载更多")
        self._loading = False
        #: 正在播放的 ``bvid``;空串表示没有
        self._playing_bvid = ""
        #: 上一次加载失败的中文提示;成功加载新内容时清掉(见 set_items / append_items)
        self._failed_message = ""

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
        self.list.row_activated.connect(self._on_activated)
        self.list.row_menu_requested.connect(self._on_menu_requested)
        self.list.add_requested.connect(self._on_add_requested)

        self.empty_page = PlaceholderPage()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.empty_page)
        layout.addWidget(self.stack, 1)
        layout.addLayout(self._build_footer())

        self._render()

    # ------------------------------------------------------------ 构建界面

    def _build_header(self) -> QHBoxLayout:
        """建"收藏夹名 + 私密标记 + 过滤框 + 统计"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.title_label = QLabel("收藏夹")
        self.title_label.setObjectName("PageTitle")

        self.privacy_label = QLabel("")
        self.privacy_label.setObjectName("MutedLabel")
        self.privacy_label.setToolTip("私密收藏夹:内容不该被外人看到")

        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("FilterInput")
        self.filter_input.setFixedWidth(FAV_FILTER_WIDTH)
        self.filter_input.setPlaceholderText("按标题或UP主过滤")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._on_filter_changed)

        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")

        row.addWidget(self.title_label)
        row.addWidget(self.privacy_label)
        row.addStretch(1)
        row.addWidget(self.filter_input)
        row.addWidget(self.count_label)
        return row

    def _build_footer(self) -> QHBoxLayout:
        """建"加载更多 + 说明"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.status_label = QLabel("")
        self.status_label.setObjectName("StatusLabel")

        self.load_more_button = QPushButton("加载更多")
        self.load_more_button.setObjectName("GhostTextButton")
        self.load_more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more_button.setToolTip("继续从B站取这个收藏夹的下一页")
        self.load_more_button.clicked.connect(self._on_load_more_clicked)

        row.addWidget(self.status_label, 1)
        row.addWidget(self.load_more_button)
        return row

    # ------------------------------------------------------------ 上层推状态

    def set_folder(self, title: str, *, is_private: bool = False, media_count: int = 0) -> None:
        """设置当前收藏夹的标题行信息(不碰列表内容)。

        Args:
            title: 收藏夹名;空串时显示占位文案。
            is_private: 是否私密夹。
            media_count: 接口给的总条数(用于"已加载 N / 共 M")。
        """
        self._title = title.strip()
        self._private = bool(is_private)
        self._media_count = max(0, int(media_count))
        self.title_label.setText(self._title or "收藏夹")
        self.privacy_label.setText("· 私密" if self._private else "")

    def set_items(self, items: Sequence[FavItem]) -> None:
        """整体替换列表内容(切收藏夹或刷新第一页时用)。

        过滤条件**保留**:用户在列表里挑歌时不该因为翻了一页就把关键字清掉。

        Args:
            items: 这一页的条目,顺序由上层决定(接口按最近收藏倒序)。
        """
        self._all = list(items)
        self._failed_message = ""
        self._apply_filter()

    def append_items(self, items: Sequence[FavItem]) -> None:
        """把新一页追加到列表末尾(点"加载更多"时用)。

        Args:
            items: 新一页的条目。
        """
        self._all.extend(items)
        self._failed_message = ""
        self._apply_filter()

    def set_has_more(self, has_more: bool) -> None:
        """设置"还有没有下一页",并同步按钮状态。

        Args:
            has_more: 接口 ``has_more`` 的取值。
        """
        self._has_more = bool(has_more)
        self._sync_footer()

    def set_loading(self, loading: bool) -> None:
        """设置"是否有请求在飞"(挡住重复点"加载更多")。

        Args:
            loading: 是否正在加载。
        """
        self._loading = bool(loading)
        self._sync_footer()

    def set_failed(self, message: str) -> None:
        """把上层的一次失败显示在页脚(不影响已有列表)。

        Args:
            message: 中文提示;空串表示清除。
        """
        self._loading = False
        self._failed_message = message
        self._sync_footer()

    def set_playing(self, bvid: str) -> None:
        """标记"正在播放的是哪个视频"(按 ``bvid`` 找行)。

        Args:
            bvid: 正在播放的视频 BV 号;空串表示清掉标记。
        """
        self._playing_bvid = bvid or ""
        self._apply_playing()

    def entry_at(self, row: int) -> FavItem | None:
        """取当前可见列表里某一行的条目;越界返回 ``None``。

        行号是**过滤之后**的下标 —— 信号里给的也是它,两者必须同一套口径。

        Args:
            row: 行号,0 起。

        Returns:
            对应的收藏条目;越界时返回 ``None``。
        """
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def visible_entries(self) -> tuple[FavItem, ...]:
        """当前显示出来的条目(过滤后的结果)。"""
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
        """按过滤框当前的内容重算可见条目并重绘。"""
        needle = self.filter_input.text().strip().lower()
        if not needle:
            self._visible = list(self._all)
        else:
            self._visible = [
                item
                for item in self._all
                if needle in item.title.lower() or needle in item.author.lower()
            ]
        self._render()

    def _render(self) -> None:
        """重绘列表、空态与统计。"""
        self.list.set_tracks([self._row_of(item) for item in self._visible])
        self._apply_playing()
        self._update_summary()
        # 页脚也要跟着重绘:"还有 N 个没加载"与失败提示都依赖已加载条数,
        # 内容变了却不刷新会留下过时的数字与已经消失的错误提示
        self._sync_footer()
        self.stack.setCurrentWidget(self.list if self._visible else self.empty_page)

    def _update_summary(self) -> None:
        """更新"已加载 / 共多少条"与空态文案。"""
        loaded = len(self._all)
        if self._media_count and loaded != self._media_count:
            self.count_label.setText(f"{loaded} / {self._media_count} 个内容")
        else:
            self.count_label.setText(f"{loaded} 个内容" if loaded else "")

        if self._all:
            self.empty_page.set_content("没有匹配的内容", "换个关键字,或者清空过滤条件")
        elif self._title:
            self.empty_page.set_content("这个收藏夹是空的", "在B站里收藏一些视频,再回来刷新")
        else:
            self.empty_page.set_content("还没有选择收藏夹", "从左边的「我的歌单」里点一个收藏夹")

    def _sync_footer(self) -> None:
        """按"有没有下一页 / 是否在加载 / 有没有失败"刷新页脚。"""
        self.load_more_button.setVisible(self._has_more)
        self.load_more_button.setEnabled(self._has_more and not self._loading)
        self.load_more_button.setText("加载中…" if self._loading else "加载更多")
        if self._loading:
            self.status_label.setText("正在加载…")
            return
        if self._failed_message:
            self.status_label.setText(self._failed_message)
            return
        remaining = self._media_count - len(self._all)
        self.status_label.setText(f"还有 {remaining} 个内容没加载" if self._has_more and remaining > 0 else "")

    def _apply_playing(self) -> None:
        """把"正在播放"标记刷到列表上(过滤后行号会变,所以每次重绘都要重算)。"""
        row = -1
        if self._playing_bvid:
            for index, item in enumerate(self._visible):
                if item.bvid == self._playing_bvid:
                    row = index
                    break
        self.list.set_highlight(row)

    @staticmethod
    def _row_of(item: FavItem) -> TrackRow:
        """把一条收藏条目摊成列表行。

        * 曲名走 :func:`~bilibili_music.ui.widgets.track_list.split_title_prefix`
          (与搜索结果、本地缓存同一套处理);
        * 副标题承担"这条为什么不能播"的说明(已失效 / 非视频),正常条目留空;
        * 时长在拿不到(失效条目常常没有)时显示空串,而不是 ``0:00`` —— 那个数字看起来
          像"零秒的歌",比空着更容易误导。

        Args:
            item: 收藏条目。

        Returns:
            可直接交给 ``TrackList`` 的展示数据。
        """
        prefix, title = split_title_prefix(item.title)
        if item.is_dead:
            reason = "已失效"
        elif item.item_type != FAV_ITEM_TYPE_VIDEO:
            reason = "音频/合集(暂不支持)"
        else:
            reason = ""
        return TrackRow(
            title=title,
            prefix=prefix,
            subtitle=reason,
            cover_url=item.cover_url,
            columns=(
                item.author,
                format_duration(item.duration) if item.duration > 0 else "",
                f"P{item.page_count}" if item.is_multipart else "",
            ),
            dimmed=not item.is_playable,
        )

    # ------------------------------------------------------------ 内部槽

    def _on_filter_changed(self, _text: str) -> None:
        """过滤框内容变了:重算可见条目(状态在控件自己身上,参数用不到)。"""
        self._apply_filter()

    def _on_load_more_clicked(self) -> None:
        """点了"加载更多":只在真能加载时上报。"""
        if not self._has_more or self._loading:
            return
        self.load_more_requested.emit()

    def _emit_or_reject(self, row: int, signal: Signal, *args: object) -> None:
        """行操作统一出口:能播就发信号,不能播就发原因。

        Args:
            row: 行号(以过滤后的列表为准)。
            signal: 能播时要发的信号。
            *args: 追加给信号的参数(行号由本方法补在最前面)。
        """
        item = self.entry_at(row)
        if item is None:
            return
        if not item.is_playable:
            self.unplayable_requested.emit(self._reason_of(item))
            return
        signal.emit(row, *args)

    @staticmethod
    def _reason_of(item: FavItem) -> str:
        """给"点不动的条目"拼一句中文原因。

        Args:
            item: 收藏条目。

        Returns:
            面向用户的原因文本。
        """
        if item.is_dead:
            return f"「{item.title}」已经失效了(稿件被删除或下架),播不了"
        if item.item_type != FAV_ITEM_TYPE_VIDEO:
            return f"「{item.title}」{_NOT_VIDEO_REASON}"
        return f"「{item.title}」暂不可用"

    def _on_activated(self, row: int) -> None:
        """双击某一行。"""
        self._emit_or_reject(row, self.row_activated)

    def _on_add_requested(self, row: int) -> None:
        """点某行的"+";不能播的行不发信号。"""
        self._emit_or_reject(row, self.add_requested)

    def _on_menu_requested(self, row: int, position: QPoint) -> None:
        """右键 / 点"⋮";菜单只对能播的行弹。

        不能播的行**不弹菜单**:菜单里全是"播放""加入队列"这类动作,弹出来却全都点不动,
        还不如直接说一句为什么。

        Args:
            row: 行号(以过滤后的列表为准)。
            position: 全局坐标,交给 ``QMenu.exec``。
        """
        item = self.entry_at(row)
        if item is None:
            return
        if not item.is_playable:
            self.unplayable_requested.emit(self._reason_of(item))
            return
        self.row_menu_requested.emit(row, position)
