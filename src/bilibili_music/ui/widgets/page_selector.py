"""播放条上的分P选择器:切换当前视频正在播的是哪一P。

音乐区的多P合集在队列里只占**一行**,但内部有 N 首独立的歌(``AGENTS.md`` 1.3 的领域
铁律),所以"换这一行内部的哪一首"既不属于队列层的概念,也不该塞回搜索结果列表 ——
它跟着播放条走,永远显示**当前正在播的那个视频**的分P,队列切歌时自动跟着换。

实现取舍
--------

* **选择器本体是 ``QPushButton``**:悬停、禁用、圆角描边都能复用 ``theme.py`` 里
  ``#objectName`` 那套约定,不必自绘;标签放不下时按控件宽度省略(与
  :class:`~bilibili_music.ui.widgets.elided_label.ElidedLabel` 同一手法,末尾的下拉
  箭头永远保留),完整文本走 tooltip。
* **弹出菜单是 ``Qt.Popup`` 的 ``QFrame``,不是 ``QMenu``**:一行要放"单选圆点 +
  分P编号 + 标题 + 右对齐时长"四段内容,而 ``QMenu`` 的一条 action 只有一段文本,
  硬塞进去就得自己接管绘制与命中测试。"锚在控件上方""点菜单外或按 Esc 关闭"
  这些行为 ``Qt.Popup`` 原生就有,不必手写事件过滤器。
* **菜单锚点算在选择器上方**:菜单从播放条里"长出来",不会被窗口底部裁掉,也不会
  盖住选择器自己(见 :meth:`PageSelector._popup_position`)。
* **控件不自己取数据**:分P列表与"当前播的是哪一P"由上层(主窗口从
  :mod:`bilibili_music.audio.playback` 读取)推下来,选择结果用信号送回去 ——
  与 ``PlayerBar`` 其余部分一样,"当前在播什么"只有一个真源。

本模块**不含业务判断**:不查缓存、不发请求、不改队列,只做展示与事件转发。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Page
from .elided_label import ElidedLabel

__all__ = [
    "ARROW_SUFFIX",
    "DOT_CHECKED",
    "DOT_UNCHECKED",
    "EMPTY_LABEL",
    "LABEL_PREFIX",
    "POPUP_BORDER",
    "POPUP_MARGIN",
    "POPUP_MAX_HEIGHT",
    "POPUP_ROW_HEIGHT",
    "POPUP_SCREEN_MARGIN",
    "POPUP_SPACING",
    "POPUP_WIDTH",
    "SELECTOR_HEIGHT",
    "SELECTOR_PADDING",
    "SELECTOR_WIDTH",
    "PagePopup",
    "PageRow",
    "PageSelector",
]

#: 选择器本体的高度(像素)。与音质下拉框同处一行,36 不会把播放条撑高。
SELECTOR_HEIGHT = 36

#: 选择器本体的固定宽度(像素)。
#:
#: **固定**而不是随标题伸缩:分P标题长短不一,让按钮按内容缩放会让整行布局跟着左右
#: 跳(切一首歌就抖一下)。取值高于需求给的最小宽度 150:多出来的那点用来放下
#: ``分P:P12 某个副标题`` 这类常见标签,再宽就要挤掉中间的进度条了。
SELECTOR_WIDTH = 168

#: 标签两侧的内边距(像素),**必须**与 QSS 里 ``QPushButton#PageSelector`` 的 padding 一致
#: —— 省略文字时要按它扣掉可用宽度,否则文字会顶到边框上。
#:
#: 之所以两边各写一份、而不是由 ``theme.py`` 导入本模块的常量:``ui.pixmaps`` 反过来
#: 依赖 ``ui.theme``,``theme`` 再回头导入 ``ui.widgets`` 就会撞上循环导入。
SELECTOR_PADDING = 10

#: 标签末尾的下拉箭头。**永远保留**:它是"这里能点开分P列表"的唯一提示。
ARROW_SUFFIX = " ▾"

#: 标签前缀,与音质下拉框的 ``音质:`` 同一套写法。
LABEL_PREFIX = "分P:"

#: 没有当前视频时显示的占位文本。
EMPTY_LABEL = f"{LABEL_PREFIX}--"

#: 弹出菜单的宽度(像素)。比选择器宽一倍左右,标题与时长才放得下。
POPUP_WIDTH = 348

#: 弹出菜单的最大高度(像素);分P更多时在菜单**内部**滚动,而不是撑到屏幕外。
POPUP_MAX_HEIGHT = 240

#: 弹出菜单里一行的固定高度(像素)。
POPUP_ROW_HEIGHT = 34

#: 弹出菜单四周的内边距(像素)。
POPUP_MARGIN = 6

#: 弹出菜单自己的描边宽度(像素),与 QSS 里 ``QFrame#PagePopup`` 的 border 一致。
#:
#: 必须算进高度:描边占的是**内部**尺寸,漏掉它内容就比视口高 2px,Qt 会因此冒出一条
#: 毫无意义的滚动条(实测正好差 2px,行数不满时也照样出现)。
POPUP_BORDER = 1

#: 弹出菜单行与行之间的间距(像素)。
POPUP_SPACING = 2

#: 弹出菜单与窗口边缘至少留出的空隙(像素)。
POPUP_SCREEN_MARGIN = 8

#: 未选中的单选圆点符号。
DOT_UNCHECKED = "○"

#: 选中的单选圆点符号(当前分P)。
DOT_CHECKED = "●"


class PageRow(QPushButton):
    """分P菜单里的一行:单选圆点 + 分P编号 + 标题 + 右对齐时长。

    整行做成**按钮**而不是裸 ``QWidget``:悬停与选中底色直接用 QSS 伪状态/属性选择器
    表达,不必为了一块底色去写 ``WA_Hover`` 或自绘。

    信号:
        chosen(int): 这一行被点,携带分P序号。

    Args:
        page: 这一行对应的分P。
        current: 是否就是当前正在播的那一P(选中态)。
        parent: Qt 父对象。
    """

    chosen = Signal(int)

    def __init__(
        self, page: Page, *, current: bool, parent: QWidget | None = None
    ) -> None:
        """按分P数据摆好四段内容,并记下序号与选中态。

        Args:
            page: 这一行对应的分P。
            current: 是否标记成当前分P(圆点填实 + 强调色底)。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("PageRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(POPUP_ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(page.title or f"P{page.index}")
        # 让 QSS 按属性选到"当前行":不用 :checked 是因为菜单里只有单选语义,
        # 换选中项走的是"重建行",把按钮做成 checkable 反而要额外维护互斥
        self.setProperty("current", bool(current))

        #: 这一行对应的分P序号(菜单选中后要把它发出去)
        self.page_index = int(page.index)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(8)

        self.dot_label = QLabel(DOT_CHECKED if current else DOT_UNCHECKED)
        self.dot_label.setObjectName("PageDot")
        self.dot_label.setProperty("current", bool(current))
        self.dot_label.setFixedWidth(14)
        self.dot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.index_label = QLabel(f"P{page.index}")
        self.index_label.setObjectName("PageIndex")
        self.index_label.setFixedWidth(30)

        # 标题用 ElidedLabel:音乐区的分P标题可以很长,放不下要省略而不是撑破菜单
        self.title_label = ElidedLabel(page.title or f"P{page.index}")
        self.title_label.setObjectName("PageTitle")

        # 时长取**分P自己的** duration:视频级 duration 是所有分P之和,不能当单曲时长
        self.duration_label = QLabel(page.duration_text)
        self.duration_label.setObjectName("PageDuration")
        self.duration_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.duration_label.setFixedWidth(48)

        for widget in (
            self.dot_label,
            self.index_label,
            self.title_label,
            self.duration_label,
        ):
            # 行内文字必须让鼠标事件穿透:否则点在标题上不会触发整行的 clicked
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(self.dot_label)
        row.addWidget(self.index_label)
        row.addWidget(self.title_label, 1)
        row.addWidget(self.duration_label)

        self.clicked.connect(self._on_clicked)

    @property
    def is_current(self) -> bool:
        """这一行是不是当前正在播的那一P。"""
        return bool(self.property("current"))

    def _on_clicked(self) -> None:
        """把这一行的分P序号上报;关菜单与切歌分别由选择器和主窗口负责。"""
        self.chosen.emit(self.page_index)


class PagePopup(QFrame):
    """分P列表弹出菜单。

    用 ``Qt.Popup`` 顶层窗口:点菜单外或按 Esc 会**自动关闭且不改变当前选择**,
    也不会被父窗口的边界裁掉 —— 菜单要浮在搜索结果内容之上。

    信号:
        page_chosen(int): 某一P被点选,携带分P序号。

    Args:
        parent: Qt 父对象(选择器本体);``Qt.Popup`` 只借用它做归属与销毁。
    """

    page_chosen = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好滚动容器;行内容由 :meth:`set_pages` 填。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("PagePopup")
        self.setFixedWidth(POPUP_WIDTH)
        # 圆角要真的透出底下的内容,否则四个角会是四块突兀的方角
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(POPUP_MARGIN, POPUP_MARGIN, POPUP_MARGIN, POPUP_MARGIN)
        root.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("PagePopupScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 滚动区视口默认按 QPalette 的 Base 角色铺底(那是面板色),会和菜单底色对不上
        self.scroll.viewport().setObjectName("PagePopupViewport")

        self.body = QWidget()
        self.body.setObjectName("PagePopupBody")
        self._rows = QVBoxLayout(self.body)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(POPUP_SPACING)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll)

        #: 当前列出的行,按分P顺序。留成公开属性是为了让"菜单里列了什么"能被自动验证
        #: (离屏测试没法真的用鼠标去点)
        self.rows: list[PageRow] = []

    def set_pages(self, pages: Sequence[Page], current_index: int) -> None:
        """按给的分P重建所有行,并把菜单高度夹在上限内。

        Args:
            pages: 要列出的分P,保持接口给它们的顺序。
            current_index: 当前正在播的分P序号;没有匹配行时不标记任何一行为选中。
        """
        self._clear_rows()
        for page in pages:
            row = PageRow(page, current=page.index == current_index, parent=self.body)
            row.chosen.connect(self.page_chosen.emit)
            self._rows.addWidget(row)
            self.rows.append(row)
        self._rows.addStretch(1)
        self.setFixedHeight(self._height_for(len(self.rows)))

    def _height_for(self, count: int) -> int:
        """按行数算菜单高度:行多时封顶在 :data:`POPUP_MAX_HEIGHT`,超出部分内部滚动。"""
        content = count * POPUP_ROW_HEIGHT + max(0, count - 1) * POPUP_SPACING
        chrome = 2 * (POPUP_MARGIN + POPUP_BORDER)
        return min(POPUP_MAX_HEIGHT, content + chrome)

    def _clear_rows(self) -> None:
        """清空旧行。

        换选中项与换视频都走"整菜单重建":分P数量本来就不多,重建比逐行改属性再
        ``unpolish/polish`` 更不容易出错。
        """
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue  # 末尾的 stretch
            # 先 setParent(None) 再 deleteLater:deleteLater 要等事件循环才生效,
            # 在它生效之前旧行仍是可见子控件,会和刚建的新行叠在一起
            widget.setParent(None)
            widget.deleteLater()
        self.rows.clear()


class PageSelector(QPushButton):
    """播放条上的分P选择器。

    **不自己保存播放状态**:分P列表与"当前播的是哪一P"由上层通过 :meth:`set_pages`
    推下来,用户的选择用 :attr:`page_selected` 送上去,这样"到底在播哪一P"只有一个真源
    (与 ``PlayerBar`` 的其余部分同一套约定)。

    信号:
        page_selected(int): 用户选了另一个分P,携带目标分P序号(从 1 开始)。

    Args:
        parent: Qt 父对象。
    """

    page_selected = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好选择器;此时没有当前视频,因此是禁用状态。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("PageSelector")
        self.setFixedSize(SELECTOR_WIDTH, SELECTOR_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._pages: tuple[Page, ...] = ()
        self._current_index = 0
        self._popup: PagePopup | None = None
        #: 菜单不该压住的控件(实际用法是右侧播放队列面板);由上层注入
        self._avoid: QWidget | None = None

        self.clicked.connect(self._on_clicked)
        self._refresh()

    # ------------------------------------------------------------ 只读视图

    @property
    def pages(self) -> tuple[Page, ...]:
        """当前视频的分P列表(上层推下来的那一份);没有当前视频时为空。"""
        return self._pages

    @property
    def current_page_index(self) -> int:
        """当前显示的分P序号;``0`` 表示还没有当前视频。"""
        return self._current_index

    @property
    def is_popup_open(self) -> bool:
        """分P菜单当前是否打开。"""
        return self._popup is not None and self._popup.isVisible()

    @property
    def avoid_widget(self) -> QWidget | None:
        """菜单当前要避开的控件;没有设置过时为 ``None``。"""
        return self._avoid

    def full_label_text(self) -> str:
        """当前分P对应的**完整**标签文本(不含省略,也不含末尾箭头)。

        界面上显示的可能是省略后的版本,完整文本在工具提示里 —— 与
        :class:`~bilibili_music.ui.widgets.elided_label.ElidedLabel` 的 ``full_text()``
        同一个用意:截断显示不等于信息丢失。
        """
        return self._label_text()

    def popup_rows(self) -> tuple[PageRow, ...]:
        """菜单里的行;菜单还没建过时返回空元组。"""
        if self._popup is None:
            return ()
        return tuple(self._popup.rows)

    # ------------------------------------------------------------ 上层推状态

    def set_pages(self, pages: Sequence[Page], current_index: int = 0) -> None:
        """更新"当前视频的分P列表 + 正在播的那一P"。

        Args:
            pages: 当前视频的分P列表;空序列表示没有当前视频(或详情还没补全)。
            current_index: 正在播的分P序号,从 1 开始;``0`` 表示没有当前视频。
        """
        self._pages = tuple(pages)
        self._current_index = int(current_index)
        # 菜单里的行已经属于上一首/上一P了,留着只会让人误点
        self.close_popup()
        self._refresh()

    def set_avoid_widget(self, widget: QWidget | None) -> None:
        """指定菜单**不该压住**的控件(实际用法是右侧的播放队列面板)。

        选择器自己看不到队列面板 —— 那是主窗口组装的,所以引用由上层递进来。

        Args:
            widget: 菜单要避开的控件;``None`` 表示不需要避让。
        """
        self._avoid = widget

    # ------------------------------------------------------------ 菜单

    def show_popup(self) -> None:
        """弹出分P菜单;没有任何分P可列时什么都不做。"""
        if not self._pages:
            return
        popup = self._popup
        if popup is None:
            popup = PagePopup(self)
            popup.page_chosen.connect(self._on_page_chosen)
            self._popup = popup
        popup.set_pages(self._pages, self._current_index)
        self._place_popup(popup)
        popup.show()

    def close_popup(self) -> None:
        """关掉分P菜单(没打开时是空操作);**不改变**当前选择。"""
        if self._popup is not None:
            self._popup.hide()

    def _place_popup(self, popup: QWidget) -> None:
        """把菜单挪到锚点上(位置算法见 :meth:`_popup_position`)。

        这里有一处平台差异必须处理:**先创建原生窗口再移动**。窗口首次创建时,平台会
        按边框宽度把客户区往右下推一点(离屏平台实测 2px,Windows 上通常是 0),
        于是"隐藏状态下 move 到 A、show 之后落在 A+边框" —— 菜单就不再贴着选择器了。
        先把窗口建出来(Qt 的 ``QMenu`` 也是这么做的),``show()`` 便不会再有这一跳。

        Args:
            popup: 菜单控件。
        """
        popup.winId()  # 强制创建原生窗口,消掉 show() 时的那一次偏移
        popup.move(self._popup_position(popup.size()))

    def _popup_position(self, size: QSize) -> QPoint:
        """算出菜单左上角的全局坐标。

        基本规则两条:水平与选择器**居中对齐**、底边**紧贴**选择器上边缘(菜单从播放条里
        长出来,既不会被窗口底部裁掉,也不会盖住选择器自己)。在此之上再往回收两处 ——
        不越出窗口左右边界,以及不压住右侧的播放队列面板。菜单浮在搜索结果之上是应该的,
        盖住用户自己排的队就不是了,所以"避让"这一条允许挤掉居中。

        Args:
            size: 菜单的尺寸(像素)。

        Returns:
            菜单左上角的全局坐标。
        """
        anchor = self.mapToGlobal(QPoint(0, 0))
        centered = anchor.x() + (self.width() - size.width()) // 2
        y = anchor.y() - size.height()

        window = self.window()
        if window is None:
            # 还没挂进任何窗口(离屏构造的中间态):只能按锚点原样算
            return QPoint(centered, y)
        top_left = window.mapToGlobal(QPoint(0, 0))
        min_x = top_left.x() + POPUP_SCREEN_MARGIN
        max_x = top_left.x() + window.width() - POPUP_SCREEN_MARGIN - size.width()

        avoid = self._avoid
        if avoid is not None and avoid.isVisible() and avoid.window() is window:
            avoid_left = avoid.mapToGlobal(QPoint(0, 0)).x()
            max_x = min(max_x, avoid_left - size.width())
        # 窗口比菜单还窄时上下限会交叉:此时以"不越出左边界"为准,右边交给窗口裁剪
        return QPoint(max(min_x, min(centered, max_x)), y)

    def _on_clicked(self) -> None:
        """点选择器本体:展开分P列表(已经展开时收起)。"""
        if self.is_popup_open:
            self.close_popup()
            return
        self.show_popup()

    def _on_page_chosen(self, page_index: int) -> None:
        """菜单里选定一P:先关菜单,再把选择上报给上层。

        顺序不能反:上报会立刻触发重新解析与下载,菜单不该在这期间悬在界面上。
        """
        self.close_popup()
        self.page_selected.emit(int(page_index))

    # ------------------------------------------------------------ 内部

    def _refresh(self) -> None:
        """按当前状态刷新标签、工具提示与可用性。"""
        text = self._label_text()
        self.setToolTip(text)
        self._apply_text(text)
        # 没有视频、或当前视频只有一个分P时禁用。单P也照样显示当前分P(而不是清空),
        # 控件宽度与位置因此保持不变 —— 切歌时不会看到按钮突然变宽变窄
        self.setEnabled(bool(self._pages) and len(self._pages) > 1)

    def _label_text(self) -> str:
        """拼出完整标签文本(不含省略;末尾箭头由 :meth:`_apply_text` 统一追加)。"""
        page = self._current_page()
        if page is not None:
            return f"{LABEL_PREFIX}P{page.index} {page.title or f'P{page.index}'}"
        if self._current_index > 0:
            # 详情还没补全:标题未知,先只报序号,补全后 set_pages 会再刷一次
            return f"{LABEL_PREFIX}P{self._current_index}"
        return EMPTY_LABEL

    def _current_page(self) -> Page | None:
        """按序号在推下来的列表里找当前分P;没有当前视频或序号越界时返回 ``None``。"""
        for page in self._pages:
            if page.index == self._current_index:
                return page
        return None

    def _apply_text(self, text: str) -> None:
        """把文本写进按钮:放不下时省略标题,但**保留末尾箭头**。

        箭头是"这里能点开分P列表"的唯一提示,跟着标题一起被省略掉等于把入口藏了。
        """
        arrow = ARROW_SUFFIX
        width = self.width()
        if width <= 0:
            # 还没被布局过(构造后立刻取文本):先按原文显示,resizeEvent 会再刷一次
            super().setText(text + arrow)
            return
        metrics = QFontMetrics(self.font())
        available = width - 2 * SELECTOR_PADDING - metrics.horizontalAdvance(arrow)
        super().setText(
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, available))
            + arrow
        )

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - QResizeEvent / Qt 命名
        """宽度变了就重新算一次省略位置(省略只能在这一刻做,宽度是布局算的)。"""
        super().resizeEvent(event)
        self._apply_text(self._label_text())
