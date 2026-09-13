"""搜索结果与播放队列共用的曲目列表。

两种列表(搜索 / 队列)共用同一个控件是有意的:它们的差别只是列与数据来源,做成两个
表格会立刻出现"搜索列表能双击播放、队列列表忘了接"这类不一致。

控件只认**已经摊平的展示数据**(:class:`TrackRow`)与行号:它不 import ``Video`` /
``QueueItem`` —— 那些模型的展示规则(多P用分P标题、副标题带 ``P3``)属于上层,
不该让一个通用列表去认识它们。

设计取舍
--------

**富行(封面 + 标题 + 副标题)用"单元格控件"实现,而不是自定义 delegate。**
设计稿里每行右侧有真按钮("+"与"⋮"),delegate 画出来的按钮要自己处理命中测试与悬停,
而 Qt 的 ``QPushButton`` 天生就有这些。代价是单元格控件会盖住表格自己的绘制,所以那一格
设了 ``WA_TransparentForMouseEvents`` —— Qt 对这个属性的语义是"该控件**及其子控件**都
不接收鼠标事件",于是双击标题区域仍然穿透到表格、照常触发 ``row_activated``。

**"当前播放行"用选中该行来表达,而不是给每个单元格设背景色。** 应用贴了全局 QSS,
``::item:hover`` / ``::item:selected`` 的底色由样式绘制并盖住 item 自己的
``BackgroundRole``,两者同时用会互相打架;统一交给 QSS 之后,"当前行长什么样"只有
一处定义。行首序号加粗 + 标题染强调色则由本控件负责(样式表管不到单个 item 的字体)。

**单元格里的 ``QLabel`` 不会省略过长文字**(delegate 才会),所以富行里的标签用
:class:`~bilibili_music.ui.widgets.elided_label.ElidedLabel`;完整标题挂在 item 的
``ToolTipRole`` 上 —— 单元格控件对鼠标透明,挂在它自己身上的工具提示永远不会弹出来,
而 ``QAbstractItemView`` 内置支持"按 item 的工具提示"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..cover_loader import CoverLoader
from ..icons import DARK, get_icon
from ..pixmaps import cover_pixmap
from .elided_label import ElidedLabel

__all__ = [
    "COVER_SIZE",
    "INDEX_COLUMN",
    "RICH_COLUMN",
    "ROW_HEIGHT",
    "TrackList",
    "TrackRow",
    "split_title_prefix",
]

#: 序号列(第 0 列)。
INDEX_COLUMN = 0

#: 富信息列(封面 + 标题 + 副标题);它同时是占据多余宽度的那一列。
RICH_COLUMN = 1

#: 行高(像素)。要放得下 40px 的封面并留出上下呼吸空间。
ROW_HEIGHT = 56

#: 封面缩略图边长(像素)。
COVER_SIZE = 40

#: 距底部多少像素算"快到底了",提前触发下一页。取 3 行高:用户几乎感觉不到停顿,
#: 又不至于刚往下滚一屏就白花一次请求。
_LOAD_MORE_MARGIN = ROW_HEIGHT * 3

#: 序号列的**最小**宽度(像素)。实际宽度按字体算(见 :meth:`TrackList._index_width`),
#: 这个值只是下限,免得字体特别小时列窄得像条缝。
_INDEX_WIDTH = 44

#: 序号要能显示到几位:翻页上限是 5 页 × 每页 30 条 ≈ 150 行,所以三位数封顶。
_INDEX_SAMPLE = "100"

#: QSS 给 ``TrackTable::item`` 的 ``padding: 0 8px`` 在左右各吃掉的像素。
_INDEX_PADDING = 16

#: 操作列宽度(像素):"+"与"⋮"两个 26px 按钮加间距。
_ACTION_WIDTH = 76

#: 操作按钮的尺寸(像素)。
_ACTION_BUTTON = 26


def split_title_prefix(title: str) -> tuple[str, str]:
    """把"歌手 - 歌名"这一类标题拆成"弱化前缀 + 主标题"。

    音乐区的标题普遍写成 ``周杰伦 - 晴天【官方 MV】``;设计稿里歌手用弱化色、歌名用
    加粗亮色,所以这里按**第一个带空格的连字符**拆一次。

    只认 ``" - "``(两侧都有空格):标题里出现单个 ``-``(``【4K】``、``MV-01``)的概率
    不低,不设这个门槛会把歌名拆坏。拆不出来就返回空前缀,界面照样显示完整标题。

    Args:
        title: 原始标题。

    Returns:
        ``(前缀, 主标题)``;前缀自带 ``" - "`` 分隔符,拼回去与原文一字不差。
        ``("", title)`` 表示这条标题没有可拆的前缀。

    Examples:
        >>> split_title_prefix("周杰伦 - 晴天【官方 MV】")
        ('周杰伦 - ', '晴天【官方 MV】')
        >>> split_title_prefix("【4K】某首歌")
        ('', '【4K】某首歌')
    """
    head, separator, tail = title.partition(" - ")
    if not separator or not head.strip() or not tail.strip():
        return "", title
    return f"{head} - ", tail


@dataclass(slots=True)
class TrackRow:
    """列表里一行曲目的展示数据。

    内容是**已经摊平**的字符串,而不是 ``Video`` / ``QueueItem``:列表控件因此不认识
    业务模型,上层的展示规则(比如"多P合集用分P标题")改动时不需要动它。

    Attributes:
        title: 主标题(搜到的那首歌)。
        subtitle: 灰色小字副标题(UP主 / 分P序号等);空串表示这一行没有副标题。
        prefix: 标题前的弱化前缀(如 ``"周杰伦 - "``);空串表示没有。
        cover_url: 封面地址;空串表示这一行没有封面,显示占位图。
        columns: 富信息列**之后**各列的文本,按顺序填。长度不足的列留空。
    """

    title: str
    subtitle: str = ""
    prefix: str = ""
    cover_url: str = ""
    columns: tuple[str, ...] = ()


@dataclass(slots=True)
class _RowWidgets:
    """一行里后续还需要改动的控件引用(贴封面、给标题上色)。"""

    cover_url: str
    cover_label: QLabel
    title_label: ElidedLabel
    subtitle_label: ElidedLabel
    #: 是否已经贴上真封面(占位图不算),供 :meth:`TrackList.has_cover_at` 回答
    has_cover: bool = False


class TrackList(QTableWidget):
    """一列曲目,支持双击激活、行内操作按钮与右键菜单。

    信号:
        row_activated(int): 双击某一行,参数是行号(0 起)
        row_menu_requested(int, QPoint): 请求某行的右键菜单(操作列的"⋮"也会发它),
            参数是行号与全局坐标
        add_requested(int): 点了某行的"+"(加入播放队列)
        load_more_requested(): 用户快滑到底了,想再要一批。
            **控件只报"快到底了"这一件事**:有没有下一页、是不是正在加载、要不要去重,
            全是调用方(``MainWindow``)的状态 —— 列表不该认识分页协议。

    信号名带 ``row_`` / ``*_requested`` 前缀是为了**避开基类已有的信号**:
    ``QAbstractItemView`` 本身就有 ``activated(QModelIndex)``、``doubleClicked`` 等,
    同名覆盖会打断 Qt 内部转发。

    Args:
        columns: 列标题。第 0 列固定是序号、第 1 列固定是富信息列,由本控件填充,
            调用方只负责给它们起名字。
        covers: 封面加载器;``None`` 表示不取封面(全部显示占位图),测试可以省略。
        action_column: 操作列的下标;``None`` 表示这张表没有操作列。
        centered: 需要居中对齐的普通列下标集合(如"时长""分P");其余普通列左对齐。
        parent: Qt 父对象。
    """

    row_activated = Signal(int)
    row_menu_requested = Signal(int, QPoint)
    add_requested = Signal(int)
    load_more_requested = Signal()

    def __init__(
        self,
        columns: Sequence[str],
        *,
        covers: CoverLoader | None = None,
        action_column: int | None = None,
        centered: Sequence[int] = (),
        parent: QWidget | None = None,
    ) -> None:
        """建表并固定交互行为。

        Args:
            columns: 列标题;至少要两列(序号 + 富信息)。
            covers: 封面加载器。
            action_column: 操作列下标。
            centered: 居中对齐的列下标。
            parent: Qt 父对象。

        Raises:
            ValueError: 列数少于 2 —— 那样第 1 列的富信息没有地方放。
        """
        super().__init__(0, len(columns), parent)
        if len(columns) < 2:
            raise ValueError("曲目列表至少需要两列:序号与曲目信息")

        self.setObjectName("TrackTable")
        self._covers = covers
        self._action_column = action_column
        self._centered = set(centered)
        self._rows: list[_RowWidgets] = []

        self.setHorizontalHeaderLabels(list(columns))
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        # 逐像素滚动:整行滚动在 56px 行高下会一跳一跳的
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 表格不该抢焦点:焦点框会在行上画一圈虚线,而"当前行"另有表达方式
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        for column in range(len(columns)):
            if column == INDEX_COLUMN:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                self.setColumnWidth(column, self._index_width())
            elif column == RICH_COLUMN:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
            elif column == action_column:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
                self.setColumnWidth(column, _ACTION_WIDTH)
            else:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        self.doubleClicked.connect(self._on_double_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu_requested)
        # "快到底了"只能从滚动条读:QTableWidget 没有"滚到边界"的信号
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # ------------------------------------------------------------ 填数据

    def set_tracks(self, rows: Sequence[TrackRow]) -> None:
        """整体替换所有行,并按需请求封面。

        列表整体换内容时旧的封面请求已经没人要了,所以先清掉加载队列 —— 否则用户连着
        搜两次,第一次那几十张封面还要占着限速窗口慢慢取完。

        翻页追加请用 :meth:`append_tracks` —— 那条路径**不清**封面队列,也不动已有行。

        Args:
            rows: 每行的展示数据;行数可以变化。
        """
        if self._covers is not None:
            self._covers.clear()
        self.clearSelection()
        self.setRowCount(0)
        self._rows.clear()

        self._append_rows(rows)

    def append_tracks(self, rows: Sequence[TrackRow]) -> None:
        """在末尾追加若干行(翻页加载用)。

        与 :meth:`set_tracks` 的关键差别是**不动已有行**:不重建、不丢掉已经贴上的封面、
        也不清封面加载队列 —— 翻页时把前 30 行的封面请求全部作废再重下一遍,既慢又是
        白挨一次限速。序号由 :meth:`_set_index` 按全局行号生成,所以接着往下排。

        Args:
            rows: 要追加的展示数据;空序列时什么都不做。
        """
        if not rows:
            return
        self._append_rows(rows)

    def _append_rows(self, rows: Sequence[TrackRow]) -> None:
        """把一批行接到表格末尾,并请求它们缺的封面。

        Args:
            rows: 要追加的展示数据;空序列是安全的空操作。
        """
        start = self.rowCount()
        for offset, track in enumerate(rows):
            index = start + offset
            self.insertRow(index)
            self.setRowHeight(index, ROW_HEIGHT)
            self._set_index(index)
            self._rows.append(self._set_rich(index, track))
            self._set_plain_columns(index, track)
            if self._action_column is not None:
                self._set_actions(index)

        if self._covers is not None:
            for track in rows:
                if track.cover_url:
                    self._covers.load(track.cover_url)

    def set_cell(self, row: int, column: int, text: str) -> None:
        """改写单个普通单元格(用于"分P数"这种稍后才补全的信息)。

        序号列、富信息列与操作列由本控件自己维护,传它们的下标会被忽略 —— 否则调用方
        一不小心就把封面或按钮覆盖掉了。

        Args:
            row: 行号,越界时什么都不做。
            column: 列号,越界或属于特殊列时什么都不做。
            text: 新的单元格文本。
        """
        if not 0 <= row < self.rowCount() or not 0 <= column < self.columnCount():
            return
        if column in (INDEX_COLUMN, RICH_COLUMN) or column == self._action_column:
            return
        item = self.item(row, column)
        if item is None:
            self.setItem(row, column, self._plain_item(text, column))
        else:
            item.setText(text)

    def set_cover(self, url: str, pixmap: QPixmap) -> None:
        """把某张封面贴到所有引用它的行上。

        Args:
            url: 封面地址(与 :attr:`TrackRow.cover_url` 对应)。
            pixmap: 已下载好的原图;会被裁成圆角缩略图。
        """
        thumb = cover_pixmap(pixmap, COVER_SIZE)
        for record in self._rows:
            if record.cover_url == url:
                record.cover_label.setPixmap(thumb)
                record.has_cover = True

    def set_highlight(self, row: int) -> None:
        """标记"当前播放的是哪一行",并把视图滚到它上面。

        Args:
            row: 要突出的行号;传负数表示取消标记。
        """
        for index, record in enumerate(self._rows):
            active = index == row
            self._style_index(index, active)
            record.title_label.setStyleSheet(f"color: {DARK.accent};" if active else "")
        if 0 <= row < self.rowCount():
            self.selectRow(row)
            item = self.item(row, INDEX_COLUMN)
            if item is not None:
                self.scrollToItem(item)
        else:
            self.clearSelection()

    def highlighted_row(self) -> int:
        """当前被加粗标记的行号;没有则为 ``-1``。

        用于测试与界面自检 —— 否则"到底哪一行是当前播放"这个状态就只存在于字体里,
        外面无从断言。
        """
        for index in range(self.rowCount()):
            item = self.item(index, INDEX_COLUMN)
            if item is not None and item.font().bold():
                return index
        return -1

    def title_at(self, row: int) -> str:
        """某一行显示的主标题(**完整**文本,不是省略后的);越界返回空串。

        富信息列是单元格控件而不是 ``QTableWidgetItem``,``item(row, 1).text()`` 拿不到
        曲名;测试与界面自检需要这个明确的入口。
        """
        if not 0 <= row < len(self._rows):
            return ""
        return self._rows[row].title_label.full_text()

    def subtitle_at(self, row: int) -> str:
        """某一行的副标题(完整文本);越界返回空串。"""
        if not 0 <= row < len(self._rows):
            return ""
        return self._rows[row].subtitle_label.full_text()

    def cover_url_at(self, row: int) -> str:
        """某一行期望的封面地址;越界返回空串。"""
        if not 0 <= row < len(self._rows):
            return ""
        return self._rows[row].cover_url

    def has_cover_at(self, row: int) -> bool:
        """某一行贴的是真封面还是占位图。

        封面槽位**永远**有图(没有就画占位图),所以只看 ``pixmap()`` 非空判断不出
        "到底有没有封面";这与 :attr:`PlayerBar.has_cover` 是同一个理由。
        """
        if not 0 <= row < len(self._rows):
            return False
        return self._rows[row].has_cover

    # ------------------------------------------------------------ 行内构造

    def _index_width(self) -> int:
        """按当前字体算出序号列要留多宽。

        **不能写死像素**:翻页加载后行号会到三位数(:data:`_INDEX_SAMPLE`),而三位数要
        多宽完全取决于字体 —— 同一个 44,在 Microsoft YaHei UI 9pt 下够(``"100"`` 21px),
        在找不到系统字体时的 fallback 字体下就不够(实测 36px)。字体是运行期才知道的。

        按**加粗**字体算:当前播放行的序号是加粗的(见 :meth:`_style_index`)。再补上 QSS
        给 ``::item`` 预留的左右 padding —— 列宽里有一段是被样式表吃掉的,那部分放不下字。

        Returns:
            列宽(像素),不小于 :data:`_INDEX_WIDTH`。
        """
        bold = self.font()
        bold.setBold(True)
        needed = max(
            QFontMetrics(self.font()).horizontalAdvance(_INDEX_SAMPLE),
            QFontMetrics(bold).horizontalAdvance(_INDEX_SAMPLE),
        )
        return max(_INDEX_WIDTH, needed + _INDEX_PADDING)

    def _set_index(self, row: int) -> None:
        """填行首序号列(设计稿里是从 1 开始的裸数字)。"""
        item = QTableWidgetItem(str(row + 1))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(QBrush(QColor(DARK.muted)))
        self.setItem(row, INDEX_COLUMN, item)

    def _set_rich(self, row: int, track: TrackRow) -> _RowWidgets:
        """填富信息列:圆角封面 + 前缀 + 标题 + 副标题。

        Returns:
            这一行需要后续改动的控件引用。
        """
        box = QWidget()
        # 见模块 docstring:让整块(含子控件)对鼠标透明,双击才能穿透到表格
        box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(4, 0, 8, 0)
        layout.setSpacing(10)

        cover = QLabel()
        cover.setObjectName("CoverThumb")
        cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        cover.setPixmap(cover_pixmap(None, COVER_SIZE))
        layout.addWidget(cover)

        text = QVBoxLayout()
        text.setSpacing(2)

        title_line = QHBoxLayout()
        title_line.setSpacing(6)
        prefix = ElidedLabel(track.prefix)
        prefix.setObjectName("TrackPrefix")
        prefix.setVisible(bool(track.prefix))
        title = ElidedLabel(track.title)
        title.setObjectName("TrackTitle")
        title_line.addWidget(prefix, 0)
        title_line.addWidget(title, 1)

        subtitle = ElidedLabel(track.subtitle)
        subtitle.setObjectName("TrackSubtitle")
        subtitle.setVisible(bool(track.subtitle))

        text.addLayout(title_line)
        text.addWidget(subtitle)
        layout.addLayout(text, 1)

        self.setCellWidget(row, RICH_COLUMN, box)
        # 单元格控件对鼠标透明,工具提示挂在它身上永远弹不出来;而 QAbstractItemView
        # 内置支持"鼠标停在哪一格就显示那一格 item 的工具提示",所以这里留一个空 item
        holder = QTableWidgetItem()
        holder.setToolTip(track.title)
        self.setItem(row, RICH_COLUMN, holder)

        return _RowWidgets(
            cover_url=track.cover_url,
            cover_label=cover,
            title_label=title,
            subtitle_label=subtitle,
        )

    def _plain_item(self, text: str, column: int) -> QTableWidgetItem:
        """造一个普通文本单元格(按列决定居中还是左对齐)。"""
        item = QTableWidgetItem(str(text))
        align = (
            Qt.AlignmentFlag.AlignCenter
            if column in self._centered
            else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        item.setTextAlignment(align)
        return item

    def _set_plain_columns(self, row: int, track: TrackRow) -> None:
        """把 ``TrackRow.columns`` 摊到富信息列之后的各普通列上。"""
        for offset, text in enumerate(track.columns):
            column = RICH_COLUMN + 1 + offset
            if column >= self.columnCount() or column == self._action_column:
                break
            self.setItem(row, column, self._plain_item(text, column))

    def _set_actions(self, row: int) -> None:
        """在操作列放"加入队列"与"更多操作"两个按钮。"""
        assert self._action_column is not None  # 调用方已判过,这里只是给类型检查看
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(2)

        add_button = QPushButton()
        add_button.setObjectName("RowActionButton")
        add_button.setFixedSize(_ACTION_BUTTON, _ACTION_BUTTON)
        add_button.setIcon(get_icon("plus", DARK.accent, 16))
        add_button.setToolTip("加入播放队列")
        add_button.clicked.connect(lambda _=False, target=row: self.add_requested.emit(target))

        more_button = QPushButton()
        more_button.setObjectName("RowActionButton")
        more_button.setFixedSize(_ACTION_BUTTON, _ACTION_BUTTON)
        more_button.setIcon(get_icon("more-vertical", DARK.muted, 16))
        more_button.setToolTip("更多操作")
        more_button.clicked.connect(
            lambda _=False, target=row, button=more_button: self.row_menu_requested.emit(
                target, button.mapToGlobal(QPoint(0, button.height()))
            )
        )

        layout.addStretch(1)
        layout.addWidget(add_button)
        layout.addWidget(more_button)
        self.setCellWidget(row, self._action_column, box)

    def _style_index(self, row: int, active: bool) -> None:
        """给序号加粗/上色,作为"当前播放行"的标记之一。"""
        item = self.item(row, INDEX_COLUMN)
        if item is None:
            return
        font = item.font()
        font.setBold(active)
        item.setFont(font)
        item.setForeground(QBrush(QColor(DARK.accent if active else DARK.muted)))

    # ------------------------------------------------------------ 内部槽

    def _on_double_clicked(self, index) -> None:  # noqa: ANN001 - QModelIndex
        """把 Qt 的 ``QModelIndex`` 折算成行号再发信号。"""
        self.row_activated.emit(index.row())

    def _on_menu_requested(self, position: QPoint) -> None:
        """把右键位置折算成"哪一行 + 全局坐标"。"""
        item = self.itemAt(position)
        if item is None:
            return
        self.row_menu_requested.emit(item.row(), self.viewport().mapToGlobal(position))

    def _on_scrolled(self, value: int) -> None:
        """滚到接近底部时发一次 :attr:`load_more_requested`。

        这里**不做去重、也不判断有没有下一页**:到底了还在滚、上一次请求还没回来、
        已经加载完最后一页 —— 这些重复触发由调用方的状态机挡掉,列表只负责如实汇报
        "用户快到底了"。

        Args:
            value: 滚动条当前值(像素,``ScrollPerPixel`` 模式下就是滚过的距离)。
        """
        bar = self.verticalScrollBar()
        if bar.maximum() <= 0:
            return  # 整批内容一屏就放得下,无所谓"到底"
        if value >= bar.maximum() - _LOAD_MORE_MARGIN:
            self.load_more_requested.emit()
