"""左侧导航栏:页面入口 + "我的歌单"分组。

侧栏是**纯导航控件**:它不知道点下去会发生什么,只把"用户点了哪个入口"变成信号。
"哪个入口对应哪一页、没实现的入口怎么处理"由 ``MainWindow`` 决定(``AGENTS.md``
第 4 节:UI 控件只做展示与事件转发)。

设计取舍
--------

* **导航项用互斥的 ``QPushButton`` 而不是 ``QListWidget``**:设计稿里的选中态是圆角
  胶囊 + 强调色文字,样式表能直接表达 ``:checked``,而列表项的选中态要跟
  ``::item:selected`` 与 ``QPalette`` 的选中色一起对付。
* **侧栏不放入口给"播放队列"**:队列面板的开关只有播放条上那一个(见
  ``PlayerBar.queue_button``)。侧栏每一个入口都是"切一页"的语义,而队列面板是常驻
  侧边的开关,混在一起会让"点侧栏"到底是换页还是收起面板变得说不清。
* **图标要手动按选中态重新染色**:SVG 是栅格化成位图之后再染色的,样式表的 ``:checked``
  只管文字颜色,管不到图标 —— 与 ``PlayerBar`` 的播放模式按钮同源。
* **两套互斥组(页面 / 歌单)**:歌单也是"选中一个"的语义,但选歌单要清掉页面选中态、
  选页面要清掉歌单选中态,所以分成两个组再手工互斥,比塞进一个大组清楚。
* **歌单是动态的**(:meth:`Sidebar.set_playlists`):内容来自登录后读到的 B站 收藏夹。
  这里曾经摆过五个写死的假歌单("周杰伦""华语经典"…),凭空的歌单名比空着更糟 ——
  点进去只会看到"功能没做",还会让人以为自己的歌单丢了。信号里传 ``media_id`` 而不是
  名字:名字可以重复、实测还有空标题的夹子,只有 id 是唯一键。
* **私密记号不放在侧栏按钮上**:侧栏只有 200px,加个后缀就截断,记号改由收藏夹页的
  标题行显示 —— 那才是用户浏览内容、需要被提醒的地方。
* **收藏夹列表要有自己的操作行**(:attr:`Sidebar.refresh_button` /
  :attr:`Sidebar.visibility_button`):列表来自 B站,在网页上新建 / 删除收藏夹之后本机
  不会自己知道,必须给一个"重新取一遍"的出口;而"我不想在侧栏看到这个夹子"只能靠一层
  本地备忘(见 :meth:`Sidebar.set_playlist_actions_enabled` 与 ``core/config.py`` 的
  ``fav_hidden_ids``)。两个按钮**另起一行**而不是挤进"我的歌单 + 账号 + 新建"那一行:
  实测那一行在 176px 下就已经要被挤掉,再塞两个按钮只能靠截断文字,不如分成两行。
  侧栏因此从 176 加宽到 200 —— 顺带让长收藏夹名与长昵称不再一进来就被切掉。
* **未登录时这两个按钮是禁用的**,而不是点了弹一句"请先登录":没有登录就没有收藏夹
  列表,刷不了也藏不了(禁用色与正常色的区别见 ``ui/theme.py`` 的 ``QPalette`` 禁用组)。
"""

from __future__ import annotations

from collections.abc import Sequence
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
    "SIDEBAR_WIDTH",
    "NavItem",
    "PlaylistEntry",
    "Sidebar",
]

#: 侧栏宽度(像素)。设计稿里是 172,这里给 200:176 时"我的歌单 + 账号 + 新建"那一行
#: 已经把长昵称挤掉(实测截图),再加"刷新 / 显示隐藏"两个按钮只能另起一行,于是把宽度
#: 放宽一档,让长收藏夹名与长昵称都有地方。
#:
#: 改这个值要一起看 ``title_bar.py`` 的 ``_LOGO_BOX_WIDTH``:那里的搜索框左缘按
#: "侧栏宽度 - 标题栏左边距"对齐内容区。
SIDEBAR_WIDTH = 200

#: 导航按钮里的图标尺寸(像素)。
_NAV_ICON = 18

#: 收藏夹操作行(「刷新」「显示/隐藏」)里的小图标尺寸(像素)。
_ACTION_ICON = 14


@dataclass(frozen=True, slots=True)
class NavItem:
    """一个侧栏入口。

    Attributes:
        key: 内部标识,信号里传的就是它(界面文案可以改,key 不该跟着变)。
        label: 显示文字。
        icon: 图标名(``resources/icons`` 下的文件名)。
    """

    key: str
    label: str
    icon: str


#: 侧栏入口,每一个都对应中间内容区的一页。顺序与设计稿一致;"发现"对应的功能
#: 属路线图 M3,当前点开是占位页(见 ``widgets/placeholder.py``);
#: "搜索结果""最近播放""本地缓存"三页都是真的。
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("discover", "发现", "home"),
    NavItem("results", "搜索结果", "search"),
    NavItem("history", "最近播放", "history"),
    NavItem("cache", "本地缓存", "download"),
)

#: "我的歌单"分组的说明文字(一个歌单都没有时显示)。
#:
#: 这里**曾经**是一份写死的假歌单("周杰伦""华语经典"…)。凭空的歌单名比空着更糟:
#: 用户点进去只会看到"功能没做",还会以为自己的歌单丢了。现在歌单来自真实收藏夹,
#: 没登录时就说清楚"登录后显示"。
EMPTY_PLAYLIST_HINT = "登录后显示收藏夹"


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    """侧栏里的一个歌单条目(数据来自 B站 收藏夹)。

    侧栏不认识 ``FavFolder``(那是 ``api`` 层的类型),由 ``MainWindow`` 把它翻成这个
    纯展示结构 —— 与 :class:`NavItem` 同一条纪律:控件只认"要显示什么"。

    Attributes:
        media_id: 收藏夹 id。**信号里传的是它而不是名字**:名字可能重复、也可能为空,
            只有 id 是唯一键。
        title: 显示用的名字。
        media_count: 里面的内容条数;``0`` 表示空夹(界面上不显示 "0")。
        is_private: 是否私密夹。记号**不挤在侧栏按钮里**(176px 宽,加了后缀就截断),
            而是显示在收藏夹页的标题行上 —— 那里正是用户要看内容的地方。
    """

    media_id: int
    title: str
    media_count: int = 0
    is_private: bool = False


class Sidebar(QWidget):
    """左侧导航栏。

    信号:
        nav_selected(str): 点了某个入口,携带 :attr:`NavItem.key`
        playlist_selected(str): 点了某个收藏夹,携带十进制 ``media_id`` 文本
        refresh_playlists_requested(): 点了「刷新」(重新从B站取一遍收藏夹列表)
        manage_playlists_requested(): 点了「显示/隐藏」(打开自定义显示与否的弹窗)
        create_playlist_requested(): 点了"我的歌单"旁边的 "+"(新建歌单,尚未实现)
        account_requested(): 点了账号按钮(登录 / 查看当前账号 / 登出)
        open_log_dir_requested(): 点了底部的「日志目录」(用文件管理器打开日志所在目录)
        export_logs_requested(): 点了底部的「导出日志」(打包成一个压缩包)
    """

    nav_selected = Signal(str)
    #:
    #: ``media_id`` 已可超过 Qt ``int`` 的有符号 32 位上限(例如 ``4140917469``)。用文本
    #: 穿过 Qt 信号边界,主窗口再转回 Python ``int``,避免溢出后连槽调用也被中断。
    playlist_selected = Signal(str)
    refresh_playlists_requested = Signal()
    manage_playlists_requested = Signal()
    create_playlist_requested = Signal()
    account_requested = Signal()
    open_log_dir_requested = Signal()
    export_logs_requested = Signal()

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
        #: ``str(media_id)`` -> 按钮。**键是 id 的字符串形式**:收藏夹名可以重复、
        #: 也可以为空(实测就有一个空标题的夹子),拿名字当键会互相覆盖
        self.playlist_buttons: dict[str, QPushButton] = {}
        #: ``media_id`` -> 条目,供上层按 id 回查标题
        self.playlists: dict[int, PlaylistEntry] = {}
        #: 按钮 -> 图标名,用于按选中态重新染色
        self._nav_icons: dict[str, str] = {}
        #: 是否处于已登录态(决定收藏夹操作按钮能不能点);只由
        #: :meth:`set_playlist_actions_enabled` 写
        self._actions_enabled = False
        #: 收藏夹列表是否正在重新拉取;只由 :meth:`set_refresh_busy` 写
        self._refresh_busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(2)

        for item in NAV_ITEMS:
            layout.addWidget(self._nav_button(item))

        layout.addSpacing(10)
        layout.addLayout(self._build_playlist_header())
        layout.addLayout(self._build_playlist_actions())
        #: 歌单按钮的容器。它是**可重建的** —— 登录 / 登出 / 刷新收藏夹都要整组换掉,
        #: 直接往侧栏主布局里塞按钮就没法干净地撤下来
        self._playlist_box = QVBoxLayout()
        self._playlist_box.setContentsMargins(0, 0, 0, 0)
        self._playlist_box.setSpacing(2)
        layout.addLayout(self._playlist_box)
        layout.addStretch(1)

        #: 一个歌单都没有时显示的说明标签(登录前 / 收藏夹为空时)
        self.playlist_hint = QLabel(EMPTY_PLAYLIST_HINT)
        self.playlist_hint.setObjectName("MutedLabel")
        self.playlist_hint.setWordWrap(True)
        self.playlist_hint.setContentsMargins(12, 2, 4, 0)
        self._playlist_box.addWidget(self.playlist_hint)

        layout.addSpacing(6)
        layout.addLayout(self._build_log_row())

        self._sync_playlist_actions()

    def _build_log_row(self) -> QHBoxLayout:
        """建底部那行低存在感的日志入口:打开目录 + 导出压缩包。

        为什么不做成一个页面:**日志是排障工具,不是功能**。为它占一个与"发现 / 本地缓存"
        并列的导航项,会把日常使用的界面变吵;而做成两个小号文字按钮,既找得到、又不会
        诱导用户去点。

        Returns:
            已装好两个按钮的水平布局。
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        caption = QLabel("日志")
        caption.setObjectName("MutedLabel")
        row.addWidget(caption)

        #: 打开日志所在目录(给开发者自己排障用;因为日志目录与凭据目录物理隔离,
        #: 打开它不存在泄露风险)
        self.log_dir_button = QPushButton("打开目录")
        self.log_dir_button.setObjectName("GhostTextButton")
        self.log_dir_button.setToolTip("用文件管理器打开日志所在目录")
        self.log_dir_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_dir_button.clicked.connect(self.open_log_dir_requested.emit)
        row.addWidget(self.log_dir_button)

        #: 导出日志包(给用户"点了出问题,把日志发过来"用)
        self.export_logs_button = QPushButton("导出")
        self.export_logs_button.setObjectName("GhostTextButton")
        self.export_logs_button.setToolTip("把日志打包成一个压缩包,方便发给开发者")
        self.export_logs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_logs_button.clicked.connect(self.export_logs_requested.emit)
        row.addWidget(self.export_logs_button)

        row.addStretch(1)
        return row

    # ------------------------------------------------------------ 构建界面

    def _nav_button(self, item: NavItem) -> QPushButton:
        """造一个导航按钮,并挂到页面互斥组上。"""
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
        self._page_group.addButton(button)
        return button

    def _build_playlist_header(self) -> QHBoxLayout:
        """建"我的歌单"标签 + 账号按钮 + 新建按钮这一行。"""
        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(12, 0, 4, 0)

        label = QLabel("我的歌单")
        label.setObjectName("SectionLabel")
        row.addWidget(label)
        row.addStretch(1)

        #: 账号入口。文案随登录态变("登录" / "账号"),具体状态由
        #: :meth:`set_account` 推下来 —— 侧栏不去问"现在登录了没有"。
        self.account_button = QPushButton("登录")
        self.account_button.setObjectName("GhostTextButton")
        self.account_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_button.setToolTip("登录 B站账号后可以把收藏夹当歌单用")
        self.account_button.clicked.connect(self.account_requested.emit)
        row.addWidget(self.account_button)

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

    def _build_playlist_actions(self) -> QHBoxLayout:
        """建收藏夹列表的操作行:「刷新」+「显示/隐藏」。

        两个按钮都带图标 + 文字:图标单独一个按钮在侧栏里看不出是干什么的(尤其"显示/
        隐藏"没有公认的图形),而纯文字又比图标慢半拍才认出来。
        """
        row = QHBoxLayout()
        # 左边距与「我的歌单」标签对齐(见 _build_playlist_header)
        row.setContentsMargins(12, 2, 4, 0)
        row.setSpacing(6)

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("GhostTextButton")
        self.refresh_button.setIcon(get_icon("refresh", DARK.muted, _ACTION_ICON))
        self.refresh_button.setIconSize(QSize(_ACTION_ICON, _ACTION_ICON))
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh_playlists_requested.emit)

        self.visibility_button = QPushButton("显示/隐藏")
        self.visibility_button.setObjectName("GhostTextButton")
        self.visibility_button.setIcon(get_icon("eye", DARK.muted, _ACTION_ICON))
        self.visibility_button.setIconSize(QSize(_ACTION_ICON, _ACTION_ICON))
        self.visibility_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.visibility_button.clicked.connect(self.manage_playlists_requested.emit)

        row.addWidget(self.refresh_button)
        row.addWidget(self.visibility_button)
        row.addStretch(1)
        return row

    def _playlist_button(self, entry: PlaylistEntry) -> QPushButton:
        """造一个收藏夹按钮。

        按钮文字带上内容条数(设计稿里就有这一列);``0`` 条时**不显示数字** ——
        "0" 只会让人觉得是坏了,不如什么都不写。
        """
        text = f"{entry.title}  {entry.media_count}" if entry.media_count else entry.title
        button = QPushButton(text)
        button.setObjectName("PlaylistRow")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(16, 16))
        button.setIcon(get_icon("music", DARK.muted, 16))
        tip = f"收藏夹「{entry.title}」· {entry.media_count} 个内容"
        if entry.is_private:
            tip += "(私密)"
        button.setToolTip(tip)
        button.clicked.connect(
            lambda _=False, target=entry.media_id: self._on_playlist_clicked(target)
        )
        self._playlist_group.addButton(button)
        self.playlist_buttons[str(entry.media_id)] = button
        return button

    # ------------------------------------------------------------ 上层推状态

    def set_playlists(
        self,
        entries: Sequence[PlaylistEntry],
        *,
        empty_hint: str = EMPTY_PLAYLIST_HINT,
    ) -> None:
        """整组替换"我的歌单"里的条目(登录 / 登出 / 刷新收藏夹都走这里)。

        先建后删的顺序是有意的:全部换掉的过程中任何时刻侧栏都是完整的,不会出现
        "旧按钮已经拆了、新按钮还没上"的空窗。条目为空时显示说明标签。

        **选中态会自己接回去**:重建会把旧按钮连同 ``:checked`` 一起丢掉,而"刷新"是个
        很常见的动作 —— 一刷新就把正在听的夹子从侧栏熄灭,用户会以为选择丢了。

        Args:
            entries: 要显示的收藏夹,顺序由上层决定(接口返回的顺序)。
            empty_hint: 一条都不显示时那句说明。三种空态(未登录 / 账号下没有收藏夹 /
                收藏夹全被隐藏)必须分得清,所以文案由上层给 —— 侧栏不认识登录态。
        """
        keep = self._checked_media_id()
        for button in list(self.playlist_buttons.values()):
            # 先从互斥组里摘掉再删:留在组里的已删除按钮会让"取消全部选中"碰到野指针
            self._playlist_group.removeButton(button)
            self._playlist_box.removeWidget(button)
            button.deleteLater()
        self.playlist_buttons.clear()
        self.playlists = {entry.media_id: entry for entry in entries}

        self.playlist_hint.setText(empty_hint)
        self.playlist_hint.setVisible(not entries)
        index = self._playlist_box.indexOf(self.playlist_hint)
        for offset, entry in enumerate(entries):
            self._playlist_box.insertWidget(index + offset, self._playlist_button(entry))

        if keep is not None:
            self.set_active_playlist(keep)

    def set_active_playlist(self, media_id: int) -> None:
        """把某个收藏夹标成选中(不改内容、不发信号)。

        用于"上层替用户选了一个夹子"(打开收藏夹页)或"重建之后把选中态接回来"。
        传一个不在列表里的 ``media_id``(比如它刚被隐藏 / 删除)时**什么都不做**:那正是
        "侧栏上没有任何夹子被选中"的合法状态,不该顺手把页面入口点亮。

        Args:
            media_id: 收藏夹 id;列表里没有它时直接返回。
        """
        button = self.playlist_buttons.get(str(media_id))
        if button is None:
            return
        _uncheck_all(self._page_group)
        was_blocked = button.blockSignals(True)
        button.setChecked(True)
        button.blockSignals(was_blocked)

    def set_playlist_actions_enabled(self, enabled: bool) -> None:
        """设置收藏夹操作按钮(「刷新」「显示/隐藏」)是否可点。

        未登录时禁掉:没有登录就没有收藏夹列表,点了也只能弹一句"请先登录"。禁用态
        与文案由 :meth:`_sync_playlist_actions` 统一算 —— 它还要考虑"正在刷新"。

        Args:
            enabled: 是否处于已登录态。
        """
        self._actions_enabled = bool(enabled)
        self._sync_playlist_actions()

    def set_refresh_busy(self, busy: bool) -> None:
        """设置"正在重新拉取收藏夹列表",期间禁掉「刷新」并改文案。

        Args:
            busy: 是否有列表请求在飞。
        """
        self._refresh_busy = bool(busy)
        self._sync_playlist_actions()

    def set_account(self, uname: str) -> None:
        """更新账号按钮的文案。

        Args:
            uname: 已登录账号的昵称;空串表示未登录。昵称可能很长,按钮上只作截断显示
                —— 完整信息在账号对话框里。
        """
        name = uname.strip()
        if name:
            short = name if len(name) <= 6 else name[:6] + "…"
            self.account_button.setText(short)
            self.account_button.setToolTip(f"当前账号:{name}(点击查看详情或登出)")
        else:
            self.account_button.setText("登录")
            self.account_button.setToolTip("登录 B站账号后可以把收藏夹当歌单用")

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

    # ------------------------------------------------------------ 内部槽

    def _on_nav_clicked(self, key: str) -> None:
        """点了导航入口:清掉歌单的选中态,再把 key 上报给 ``MainWindow``。

        页面入口之间的互斥由 ``_page_group`` 自己保证,这里只管跨组的互斥。

        Args:
            key: :data:`NAV_ITEMS` 里的键。
        """
        _uncheck_all(self._playlist_group)
        self.nav_selected.emit(key)

    def _on_playlist_clicked(self, media_id: int) -> None:
        """点了收藏夹:清掉页面入口,并将不受 Qt ``int`` 限制的 id 文本上报。

        Args:
            media_id: B站 返回的收藏夹 id;可能大于有符号 32 位整数上限。
        """
        _uncheck_all(self._page_group)
        self.playlist_selected.emit(str(media_id))

    def _checked_media_id(self) -> int | None:
        """当前被选中的收藏夹 id;一个都没选中时返回 ``None``。

        重建前用它记住选中态(见 :meth:`set_playlists`)。按钮的键是 id 的字符串形式,
        这里再转回整数,免得把字符串 id 流传到上层。
        """
        for key, button in self.playlist_buttons.items():
            if button.isChecked():
                return int(key)
        return None

    def _sync_playlist_actions(self) -> None:
        """按"是否登录 + 是否正在刷新"刷新两个操作按钮的可点状态与说明。

        两个条件是不同的东西:未登录是**长期**不可点(没东西可操作),正在刷新是**临时**
        不可点(挡住连点,那等于白挨两次风控)。所以分开记、合在一处算。
        """
        if not self._actions_enabled:
            tips = "登录后才能刷新或隐藏收藏夹"
            self.refresh_button.setToolTip(tips)
            self.visibility_button.setToolTip(tips)
        else:
            self.refresh_button.setToolTip(
                "重新从B站取一遍收藏夹列表(在网页上新建的夹子要点它才会出现)"
            )
            self.visibility_button.setToolTip(
                "自定义哪些收藏夹显示在侧栏里(只影响本机显示,B站上的收藏夹不会被改动)"
            )
        self.refresh_button.setText("刷新中…" if self._refresh_busy else "刷新")
        self.refresh_button.setEnabled(self._actions_enabled and not self._refresh_busy)
        self.visibility_button.setEnabled(self._actions_enabled)


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
