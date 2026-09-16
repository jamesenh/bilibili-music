"""搜索框的历史下拉框:点搜索框给出最近搜过的词,点一条就填回去搜。

为什么是**主窗口内容区里的子控件**,而不是 ``Qt.Popup`` 顶层窗口
----------------------------------------------------------------

播放条的分P菜单用 ``Qt.Popup`` 是合适的(选完就关,不需要在里面打字),但这里的下拉框
必须与输入框**同时**活着:``Qt.Popup`` 会抓走键盘,输入框随即失焦,用户就没法"边打字边看
历史被过滤"。所以下拉框做成覆盖在内容区**之上**的普通子控件(见
:meth:`SearchSuggest.popup_under` 里的 ``raise_()``)。

父对象取**中间内容控件**(``MainWindow`` 里那个 ``AppRoot``)而不是主窗口本身:
``QMainWindow`` 会接管直接挂在它下面的子控件(``QMainWindowLayout`` 处理 ChildAdded),
把浮层交给它管理,位置就不再由我们说了算 —— 而这里的浮层必须"贴着搜索框",不能被任何
布局重排。挂在中间控件下还有个好处:它与内容区同坐标系,定位不用换算。

代价是"点控件外面自动关闭"得自己实现 —— ``Qt.Popup`` 的那套不生效了。做法是由一个全进程
唯一的鼠标按下监听器 :class:`_PressWatcher` 把每一次按下转告各下拉框(见
:meth:`SearchSuggest._on_app_press`):落点不在搜索框 / 下拉框里就收起下拉框,并
``clearFocus()`` 清掉输入框焦点 —— 用户明确要求"光标也别再闪"。清焦点不能只靠
``FocusOut``:点在没有焦点能力的空白处(标题栏空白、面板留白)根本不会触发 ``FocusOut``,
那种情况下输入光标会一直闪。

为什么不是"下拉框自己装应用级事件过滤器"
-----------------------------------------

最直觉的做法是 ``app.installEventFilter(self)``,但**不能**这么做:应用会把过滤器对象长期
引用着,而下拉框是窗口树的一部分 —— 两边互相引用之后,窗口销毁时的垃圾回收会让 PySide6
崩在 GC 里(实测:全量单测 SIGSEGV,栈顶就是 Garbage-collecting)。所以改成一个**全进程
唯一**的监听器 :class:`_PressWatcher`:它活在模块级、只持有各下拉框的**弱引用**,既不会
把窗口拖住,也不会在回收时形成环。

三个"看起来多余、其实必须"的防御
--------------------------------

1. **只认用户主动给的焦点**(:data:`_OPENING_FOCUS_REASONS`)。窗口重新变成活动窗口时,
   Qt 会把焦点"补"回原来的焦点控件,那个 ``FocusIn`` 不是用户想看历史;而且它会和
   "失焦就收起"凑成"展开 → 收起 → 展开"的闪烁。
2. **尺寸用 ``resize()``,不用 ``setFixedWidth/setFixedHeight``**。后者会
   ``updateGeometry()``,对父控件意味着一次布局请求;布局请求又会让搜索框重新排版并发回
   ``Resize``,于是"贴一次 → 布局 → 再贴一次"可能自己转起来(实测就是 CPU 占满、
   界面狂闪、下拉框反复重建的那条链路)。:meth:`SearchSuggest._reposition` 因此三处都
   不做多余动作:关着时直接返回、尺寸只在变了才写、位置没变就不 ``move``。
3. **内容没变就不重建行**(:meth:`SearchSuggest.refresh`)。行是"清空重建"的,而
   ``refresh`` 的触发源里有成串到来的(同一次点击先 ``MouseButtonPress`` 再 ``FocusIn``):
   重建一次就是一次可见的闪烁。

本模块**不查库、也不做业务判断**:历史词由 :meth:`SearchSuggest.set_source` 注入的回调
提供(实际是 :class:`~bilibili_music.core.search_history.SearchHistory` 的 ``suggest``),
"包含匹配、最多 10 条"这些规则都在 ``core`` 那一侧。
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
# shiboken6 是 PySide6 的组成部分(不是新依赖):用它判断某个包装对象背后的 C++ 对象还在不在
from shiboken6 import isValid

from .elided_label import ElidedLabel

__all__ = [
    "SUGGEST_BORDER",
    "SUGGEST_GAP",
    "SUGGEST_MARGIN",
    "SUGGEST_MIN_WIDTH",
    "SUGGEST_ROW_HEIGHT",
    "SUGGEST_SCREEN_MARGIN",
    "SUGGEST_SPACING",
    "SearchSuggest",
    "SearchSuggestRow",
]

#: 下拉框里一行的固定高度(像素)。
SUGGEST_ROW_HEIGHT = 32

#: 下拉框四周的内边距(像素)。
SUGGEST_MARGIN = 6

#: 下拉框行与行之间的间距(像素)。
SUGGEST_SPACING = 2

#: 下拉框自己的描边宽度(像素),**必须**与 QSS 里 ``QFrame#SearchSuggest`` 的 border 一致。
#:
#: 要算进高度:描边占的是内部尺寸,漏掉它内容就比框高 2px,行是定高的,多出来的部分不会
#: 自己收缩,而是从框底溢出去(与 ``page_selector.POPUP_BORDER`` 同一个坑)。
SUGGEST_BORDER = 1

#: 下拉框与搜索框之间的空隙(像素)。留一点缝,视觉上才是"从搜索框下面长出来"。
SUGGEST_GAP = 4

#: 下拉框的最小宽度(像素)。搜索框被窗口挤窄时,历史词仍要放得下。
SUGGEST_MIN_WIDTH = 240

#: 下拉框与宿主控件边缘至少留出的空隙(像素)。
SUGGEST_SCREEN_MARGIN = 8

#: 哪些焦点来源算"用户主动要看历史词"。
#:
#: 只列白名单、不放黑名单:焦点事件的来源五花八门,漏掉一个的后果是"下拉框自己冒出来"
#: 甚至"展开/收起"互相触发;而白名单漏掉的最坏结果只是"这一次没展开",用户再点一下就有。
_OPENING_FOCUS_REASONS: frozenset[Qt.FocusReason] = frozenset(
    {
        Qt.FocusReason.MouseFocusReason,  # 点搜索框
        Qt.FocusReason.TabFocusReason,  # Tab 进搜索框
        Qt.FocusReason.BacktabFocusReason,  # Shift+Tab 进搜索框
        Qt.FocusReason.ShortcutFocusReason,  # 快捷键跳进搜索框
    }
)


class _PressWatcher(QObject):
    """应用级"鼠标按下"监听器(全进程一个)。

    只干一件事:把每一次鼠标按下转给还在的下拉框(见
    :meth:`SearchSuggest._on_app_press`),由下拉框自己判断"这一下算不算点到外面"。
    事件一律放行(返回 ``False``)。

    Args:
        parent: Qt 父对象(实际传 ``QApplication``,让它跟着应用一起析构)。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        """建一个还没有任何下拉框在盯着的监听器。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._targets: list[weakref.ReferenceType[SearchSuggest]] = []

    def watch(self, suggest: SearchSuggest) -> None:
        """让某个下拉框参与监听(重复登记同一实例时只记一次)。

        Args:
            suggest: 要监听的下拉框。**只存弱引用**:强引用会把窗口拖住。
        """
        if any(reference() is suggest for reference in self._targets):
            return
        self._targets.append(weakref.ref(suggest))

    def unwatch(self, suggest: SearchSuggest) -> None:
        """把某个下拉框从监听列表里摘掉(窗口关闭时调用)。

        Args:
            suggest: 要摘掉的下拉框。
        """
        self._targets = [
            reference for reference in self._targets if reference() is not suggest
        ]

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001, N802 - QObject / Qt 命名
        """鼠标按下时挨个通知还活着的下拉框。

        Returns:
            永远 ``False``:本监听器只**看**事件,不吃掉任何东西。
        """
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        alive: list[weakref.ReferenceType[SearchSuggest]] = []
        for reference in self._targets:
            suggest = reference()
            if suggest is None or not isValid(suggest):
                # 窗口已经没了(C++ 对象已销毁,Python 包装对象还在):顺手清掉,
                # 不然这个表只增不减;而且往已销毁的控件上碰一下是会崩的
                continue
            alive.append(reference)
            suggest._on_app_press(event)  # noqa: SLF001 - 同一个模块里的私有协作
        self._targets = alive
        return False


#: 全进程唯一的"鼠标按下"监听器;第一次要用时创建(见 :func:`_press_watcher`)。
_WATCHER: _PressWatcher | None = None


def _press_watcher() -> _PressWatcher | None:
    """取(必要时创建)全进程唯一的鼠标按下监听器。

    Returns:
        监听器;进程里还没有 ``QApplication`` 时返回 ``None``(离屏构造控件的中间态)。
    """
    global _WATCHER
    if _WATCHER is None:
        app = QApplication.instance()
        if app is None:
            return None
        # 挂在应用上:应用析构时连带删掉它,不必自己管生命周期
        _WATCHER = _PressWatcher(app)
        app.installEventFilter(_WATCHER)
    return _WATCHER


class SearchSuggestRow(QPushButton):
    """下拉框里的一行:一条历史搜索词。

    整行做成**按钮**(而不是裸 ``QWidget`` 里放文字):悬停底色直接用 QSS 的 ``:hover``
    表达,与分P菜单的 ``PageRow`` 是同一手法。

    信号:
        chosen(str): 这一行被点,携带历史词原文。

    Args:
        term: 这一行的历史搜索词。
        parent: Qt 父对象。
    """

    chosen = Signal(str)

    def __init__(self, term: str, parent: QWidget | None = None) -> None:
        """摆好一行的文字,并**关掉键盘焦点**。

        关焦点是这里最要紧的一点:``QPushButton`` 默认会吃掉焦点,而点在行上让输入框失焦
        会触发"失焦就收起下拉框",于是下拉框在 ``mouseRelease`` 之前就藏了起来,这一行
        永远收不到点击(表现为"点了历史词没反应")。

        Args:
            term: 这一行的历史搜索词。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("SearchSuggestRow")
        self.setFixedHeight(SUGGEST_ROW_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(term)

        #: 这一行的历史词(点击后要把它发出去)。另存一份而不是读标签文本:标签显示的是
        #: 省略后的版本,长标题会带上省略号
        self.term = str(term)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(0)
        self.term_label = ElidedLabel(self.term)
        self.term_label.setObjectName("SearchSuggestTerm")
        # 长历史词**不许**撑宽下拉框:QLabel 的 minimumSizeHint 是这段文字不换行时的宽度,
        # 而布局会把子控件的 minimumSizeHint 当成自己的最小宽 —— 一条长关键词就能让下拉框
        # 宽过搜索框、甚至顶出窗口。Ignored 让布局忽略它的尺寸提示,宽度完全由下拉框决定,
        # 放不下时由 ElidedLabel 自己省略(悬停有 tooltip 看全文)
        self.term_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.term_label.setMinimumWidth(0)
        # 文字必须让鼠标事件穿透,否则点在文字上不会触发整行的 clicked
        self.term_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(self.term_label, 1)

        self.clicked.connect(self._on_clicked)

    def _on_clicked(self) -> None:
        """把这一行的历史词上报;收起下拉框与发起搜索由上层决定。"""
        self.chosen.emit(self.term)


class SearchSuggest(QFrame):
    """搜索框下方的历史词下拉框。

    信号:
        term_activated(str): 某条历史词被点中(发出时下拉框已经收起)。

    Args:
        parent: Qt 父对象。它同时是"下拉框要盖住哪块区域"的坐标系 —— 实际传主窗口的中间
            内容控件(见模块 docstring)。
    """

    term_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建一个空的下拉框(此时是隐藏的)。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("SearchSuggest")
        self.setVisible(False)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(
            SUGGEST_MARGIN, SUGGEST_MARGIN, SUGGEST_MARGIN, SUGGEST_MARGIN
        )
        self._layout.setSpacing(SUGGEST_SPACING)

        #: 当前列出的行。留成公开属性是为了让"下拉框里列了什么"能被自动验证
        #: (离屏测试不需要真的用鼠标去点)
        self.rows: list[SearchSuggestRow] = []
        #: 下拉框是不是开着。**自己记一份**而不是读 ``isVisible()``:父控件还没 ``show()``
        #: 时,子控件即使调过 ``show()``,``isVisible()`` 也仍然是假,状态就没法自洽
        self._open = False
        #: 最近一次请求过的尺寸(只在真的变化时才 ``resize``,理由见模块 docstring 第 2 条)
        self._size = (0, 0)
        self._line_edit: QLineEdit | None = None
        #: 参与监听的鼠标按下监听器(全进程一个);没 attach 时为 ``None``
        self._watcher: _PressWatcher | None = None
        self._source: Callable[[str], Sequence[str]] | None = None

    # ------------------------------------------------------------ 只读视图

    @property
    def is_open(self) -> bool:
        """下拉框当前是否展开。"""
        return self._open

    @property
    def line_edit(self) -> QLineEdit | None:
        """当前接管的搜索框;还没 :meth:`attach` 过时为 ``None``。"""
        return self._line_edit

    def terms(self) -> tuple[str, ...]:
        """当前列出的历史词(按显示顺序);下拉框是空的时返回空元组。"""
        return tuple(row.term for row in self.rows)

    # ------------------------------------------------------------ 接线

    def set_source(self, source: Callable[[str], Sequence[str]]) -> None:
        """注入"按输入内容取历史词"的回调。

        Args:
            source: 收输入框当前文本、返回要显示的历史词序列的回调。实际传入
                :meth:`bilibili_music.core.search_history.SearchHistory.suggest` ——
                过滤规则在 ``core`` 里,这里只负责显示。
        """
        self._source = source

    def attach(self, line_edit: QLineEdit) -> None:
        """接管搜索框:点它展开,输入时按内容过滤,点别处收起。

        搜索框自己挂事件过滤器(看它的焦点进出与尺寸变化);"鼠标到底点在哪"则交给全进程
        唯一的 :class:`_PressWatcher` —— 应用级过滤器不能挂在下拉框自己身上(见模块
        docstring)。

        Args:
            line_edit: 要接管的搜索框。
        """
        self._line_edit = line_edit
        line_edit.installEventFilter(self)
        # 只用 textEdited(用户敲的),不用 textChanged:点历史词时要 setText 填回输入框,
        # 若监听 textChanged,那一刻会立刻按新文本重算一遍下拉框,刚点完的浮层又冒出来
        line_edit.textEdited.connect(self.refresh)
        watcher = _press_watcher()
        if watcher is not None:
            watcher.watch(self)
            self._watcher = watcher

    def detach(self) -> None:
        """解除接管:摘下事件过滤器、从监听器里注销并断开信号。

        **宿主窗口关闭时必须调用**(见 ``MainWindow.closeEvent``):窗口被销毁后,这些注册
        关系还指着它的话,轻则报一屏 ``RuntimeError``/``AttributeError``,重则直接崩掉。
        """
        if self._watcher is not None:
            self._watcher.unwatch(self)
            self._watcher = None
        line_edit = self._line_edit
        if line_edit is not None:
            line_edit.removeEventFilter(self)
            try:
                line_edit.textEdited.disconnect(self.refresh)
            except RuntimeError:
                # 没连过就没有可断的;PySide 用 RuntimeError 表达"这个连接不存在"
                pass
        self._line_edit = None
        self.close_suggest()

    # ------------------------------------------------------------ 展开与收起

    def refresh(self, _text: str = "") -> None:
        """按输入框当前内容重新过滤并展示历史词。

        没有可显示的内容时**收起**下拉框:用户还没搜过任何东西、或当前输入匹配不上任何
        历史词时,一个空浮层只会挡住下面的内容。

        Args:
            _text: ``textEdited`` 会把它送进来(新输入的文本)。**刻意不用**它:文本的真源
                是输入框自己(``line_edit.text()``),少一个来源就少一处"两者不一致"的可能。
        """
        line_edit = self._line_edit
        source = self._source
        if line_edit is None or source is None:
            return
        terms = tuple(source(line_edit.text()))
        if not terms:
            self.close_suggest()
            return
        # 内容没变就别重建:行是"清空重建"的(见 set_terms),而触发源里有成串到来的
        # (同一次点击先 MouseButtonPress 再 FocusIn),每重建一次就是一次可见的闪烁
        if self._open and terms == self.terms():
            return
        self.set_terms(terms)
        self.popup_under()

    def set_terms(self, terms: Sequence[str]) -> None:
        """按给的历史词重建所有行。

        换一批词走"整块重建":最多 10 行,重建比逐行改文本再 ``unpolish/polish`` 更不容易
        出错(与分P菜单同一取舍)。

        Args:
            terms: 要显示的历史词,保持给进来的顺序(最近的在最前)。
        """
        self._clear_rows()
        for term in terms:
            row = SearchSuggestRow(term, self)
            row.chosen.connect(self._on_term_chosen)
            self._layout.addWidget(row)
            self.rows.append(row)

    def popup_under(self) -> None:
        """把下拉框摆到搜索框正下方(宽度与它对齐),抬到最上层并显示。"""
        was_open = self._open
        self._open = True
        self._reposition()
        if not was_open:
            self.show()
            # 浮在内容区(侧栏、结果列表、队列面板)之上:子控件的层叠顺序按创建先后,
            # 不抬一下会被后建的兄弟控件盖住
            self.raise_()

    def close_suggest(self, *, drop_focus: bool = False) -> None:
        """收起下拉框。

        Args:
            drop_focus: 是否同时清掉搜索框的焦点。点在搜索框与下拉框之外时要 ``True`` ——
                用户的要求是"光标也别再闪",只藏浮层不够。
        """
        if self._open:
            self._open = False
            self.hide()
        if drop_focus and self._line_edit is not None:
            self._line_edit.clearFocus()

    # ------------------------------------------------------------ 事件

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001, N802 - QObject / Qt 命名
        """观察搜索框的焦点进出与尺寸变化(过滤器挂在搜索框上,不看别处的事件)。

        Returns:
            一律 ``False`` —— 本控件只**看**这些事件,一个都不吃掉:吃掉尺寸变化会破坏
            窗口自己的布局。
        """
        line_edit = self._line_edit
        if line_edit is None or watched is not line_edit:
            return False
        kind = event.type()
        if kind == QEvent.Type.FocusIn:
            if event.reason() in _OPENING_FOCUS_REASONS:
                self.refresh()
        elif kind == QEvent.Type.FocusOut:
            # 焦点自己走了(Tab / Alt+Tab / 点进别的输入框):收起即可,不必再清一次焦点
            self.close_suggest()
        elif kind == QEvent.Type.Resize:
            # 搜索框自己变了尺寸/位置就重新贴一次。**只需看它**:标题栏是定高的一行,
            # 它的宽度跟着窗口变,而它一变化就发 Resize —— 这一条已经盖住窗口缩放与
            # 布局重排两种情形,不必再监听宿主控件(少一处注册就少一处回收时的麻烦)
            self._reposition()
        return False

    def _on_app_press(self, event) -> None:  # noqa: ANN001 - QMouseEvent
        """监听器转来的鼠标按下:判断落点在不在搜索框 / 下拉框里。

        判定用**几何位置**(两个矩形)而不是"事件的目标控件":监听器收到的是整个应用里
        每一次按下,而"这一下算不算点到外面"本来就是个位置问题。

        Args:
            event: 鼠标按下事件(``QMouseEvent``)。
        """
        if self._line_edit is None:
            return
        position = event.globalPosition().toPoint()
        if self._inside_line_edit(position):
            # 点搜索框本体:展开(已经展开且内容没变时 refresh 里会直接返回)。这里**不能**
            # 漏 —— 点已经在焦点的输入框不会再发 FocusIn,只靠焦点事件会"关掉之后打不开"
            self.refresh()
        elif not self._inside_suggest(position):
            self.close_suggest(drop_focus=True)

    def _inside_line_edit(self, position: QPoint) -> bool:
        """全局坐标是否落在搜索框内。"""
        line_edit = self._line_edit
        if line_edit is None or not line_edit.isVisible():
            return False
        return line_edit.rect().contains(line_edit.mapFromGlobal(position))

    def _inside_suggest(self, position: QPoint) -> bool:
        """全局坐标是否落在下拉框内(点在行上时不算"点到外面")。"""
        if not self._open:
            return False
        return self.rect().contains(self.mapFromGlobal(position))

    def _on_term_chosen(self, term: str) -> None:
        """点中一条历史词:先收起下拉框,再把词交给上层。

        顺序不能反:上层收到信号会立刻发起搜索,那一刻浮层不该还挂在界面上。
        """
        self.close_suggest()
        self.term_activated.emit(term)

    # ------------------------------------------------------------ 内部

    def _height_for(self, count: int) -> int:
        """按行数算下拉框高度。

        Args:
            count: 行数。

        Returns:
            高度(像素)。不含"最多显示几条"的裁决 —— 那是 ``core`` 里定的,这里给几行
            就显示几行。
        """
        content = count * SUGGEST_ROW_HEIGHT + max(0, count - 1) * SUGGEST_SPACING
        chrome = 2 * (SUGGEST_MARGIN + SUGGEST_BORDER)
        return content + chrome

    def _resize_to(self, width: int, height: int) -> None:
        """把下拉框改成指定尺寸,**值没变就什么都不做**。

        用 ``resize`` 而不是 ``setFixedWidth`` / ``setFixedHeight``:后者每次都
        ``updateGeometry()``,对父控件意味着一次布局请求,布局又会把 ``Resize`` 发回来 ——
        那条链路能自己转起来(见模块 docstring 第 2 条)。

        Args:
            width: 目标宽度(像素)。
            height: 目标高度(像素)。
        """
        if self._size == (width, height):
            return
        self._size = (width, height)
        self.resize(width, height)

    def _reposition(self) -> None:
        """按锚点(搜索框)当前的几何定尺寸并贴到它正下方。

        宽度跟随搜索框,但不小于 :data:`SUGGEST_MIN_WIDTH` —— 搜索框被挤窄时,一行历史词
        会被省略得只剩几个字;横向再收一次,不越出宿主控件的左右边界。宿主被拖得很矮时
        浮层会顶到底边之外 —— 子控件出不了宿主,那部分会被裁掉;这里不为此改成"往上弹":
        搜索框在标题栏里,往上没有空间。
        """
        if not self._open:
            # 关着的时候宿主怎么缩放都与它无关,不该为此动任何控件(见模块 docstring 第 2 条)
            return
        line_edit = self._line_edit
        parent = self.parentWidget()
        if line_edit is None or parent is None:
            return
        self._resize_to(
            max(SUGGEST_MIN_WIDTH, line_edit.width()), self._height_for(len(self.rows))
        )
        top_left = line_edit.mapTo(parent, QPoint(0, line_edit.height() + SUGGEST_GAP))
        max_x = parent.width() - SUGGEST_SCREEN_MARGIN - self.width()
        # 宿主比下拉框还窄时上下限会交叉:此时以"不越出左边界"为准,右边交给宿主裁剪
        x = max(SUGGEST_SCREEN_MARGIN, min(top_left.x(), max_x))
        target = QPoint(x, top_left.y())
        if self.pos() != target:
            self.move(target)

    def _clear_rows(self) -> None:
        """清空旧行。

        先 ``setParent(None)`` 再 ``deleteLater()``:``deleteLater`` 要等事件循环才生效,
        在它生效之前旧行仍是可见子控件,会和刚建的新行叠在一起(与分P菜单同一个坑)。
        """
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            widget.setParent(None)
            widget.deleteLater()
        self.rows.clear()
