"""主窗口:组装各块 widget,并把界面事件接到播放编排上。

**这里只做接线**。搜索、解析、下载、切歌的判断分别在 ``api`` / ``audio`` 里,界面负责把
用户操作转成方法调用、把编排层发来的信号渲染成界面状态(``AGENTS.md`` 第 4 节)。

版式(对着设计稿):

::

    ┌──────────────────────────────────────────────────────┐
    │ [图标] [搜索框 ..............] [搜索]      [-][□][×] │  ← TitleBar(自绘)
    ├────────┬──────────────────────────┬──────────────────┤
    │ Sidebar│ 内容页(QStackedWidget)   │ QueueDrawer      │
    │ 发现   │  搜索结果页 / 本地缓存页  │ 播放队列         │
    │ ...    │  / 占位页                │                  │
    ├────────┴──────────────────────────┴──────────────────┤
    │ [封面] 曲名/UP主   [传输控件]  [进度]  [分P/音质/音量] │  ← PlayerBar
    └──────────────────────────────────────────────────────┘

窗口是**无边框**的(``FramelessWindow``),所以最小化/最大化/关闭落在自绘标题栏里。

底部播放条上的**分P选择器**是"多P合集内部换歌"的入口:合集在队列里只占一行,但每个
分P都是一首独立的歌,所以换分P既不改队列,也不在搜索结果列表里 —— 它显示当前正在播
那个视频的分P,选中的目标交给 ``audio.playback`` 走既有的播放切换流程。

侧栏入口里"发现"与"我的歌单"对应的功能分别属于路线图 M3 / M5,当前还没有实现 ——
点开是一张把话说清楚的占位页,而不是点了没反应(见
:class:`~bilibili_music.ui.widgets.placeholder.PlaceholderPage`)。"播放队列"不是页面入口,
它是右侧队列面板的开关,只有播放条上那一个按钮(见 ``PlayerBar.queue_button``)。

**"本地缓存"页(路线图 M2.2)是真的**:它列出 ``AudioCache`` 索引里的已缓存音轨,
支持过滤、离线点播与删除;这一页的数据来自索引(见 ``core/cache_index.py``),而
"索引怎么维护"是解析层的事 —— 这里只负责在需要的时候读一遍推给界面
(见 :meth:`MainWindow._refresh_cache_page`)。

**"最近播放"页也是真的**:每次音频真正就绪时(``audio_ready``)往
:class:`~bilibili_music.core.history.PlayHistory` 记一条,切到这一页时读出来。
记录与缓存索引共用一个本地库(``core/library_db.py`` 下的 ``library.db``),但**分表**:
"清空缓存"不会影响历史。

**搜索结果是分页加载的**:接口一次只给一页(见 ``api.bilibili.py`` 的 ``search_video``),
列表滚到接近底部时自动取下一页并追加,失败时底部会出现一个"重试"按钮。页码、去重与
停止条件全在这里裁决(见 :meth:`MainWindow._decide_has_more`),``TrackList`` 只报
"用户快到底了"。

依赖以参数注入的只有"可替换的外部资源"(客户端 / 音频缓存 / 封面缓存 / 配置 / 编排器 /
本地库):默认全部走真实实现,测试可以塞替身进来,于是界面接线能被自动化验证,
而不是只能靠肉眼点。**本地库必须与缓存一起注入同一个实例** —— 两者写的是同一个库文件,
各建一个连接虽然也能跑,但测试里一个漏注入就会写到用户真实的配置目录。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..api.bilibili import BilibiliClient, SearchResult
from ..audio.downloader import Downloader
from ..audio.playback import PlaybackController
from ..audio.player import PlayerController
from ..audio.resolver import AudioResolver, ResolvedAudio
from ..core.cache import AudioCache
from ..core.cache_index import CachedTrack, video_from_entry
from ..core.config import ConfigStore
from ..core.cover_cache import CoverCache
from ..core.download_task import DownloadTaskStore
from ..core.history import HistoryEntry, PlayHistory, history_entry_for
from ..core.library_db import LibraryDb
from ..core.models import Page, PlayableEntry, Video, format_count, format_size
from ..core.queue import QueueItem
from ..net.base import FetchHandle
from .cover_loader import CoverLoader
from .icons import app_icon_path
from .theme import apply_theme
from .widgets import (
    CachePage,
    FramelessWindow,
    HistoryPage,
    PlaceholderPage,
    PlayerBar,
    QueueDrawer,
    Sidebar,
    TaskDialog,
    TitleBar,
    TrackList,
    TrackRow,
)
from .widgets.track_list import split_title_prefix

__all__ = ["MainWindow", "run"]

#: 搜索结果列表的列。第 0 / 1 列由 ``TrackList`` 自己填(序号、封面 + 标题)。
_SEARCH_COLUMNS = ("#", "视频信息", "UP主", "时长", "分P", "操作")

#: "分P"列的下标(详情补全后要回填它)。
_PAGES_COLUMN = 4

#: "操作"列的下标(行内 "+" 与 "⋮")。
_ACTION_COLUMN = 5

#: "分P"列在详情补全前显示的占位符。
_UNKNOWN_PAGES = "?"

#: 搜索最多自动加载几页。
#:
#: 接口自己允许几十页(实测 ``numPages`` ≈ 34,``numResults`` 封顶 1000),但**每翻一页
#: 就是一次搜索请求**,而搜索接口是风控重灾区(README「坑 3」:同一份请求连发第 3 次
#: 就可能 412)。5 页 ≈ 150 条,挑歌足够了,代价封顶在 5 次请求。
_SEARCH_MAX_PAGES = 5

#: 侧栏里那些"还没有对应功能"的入口 -> ``(占位页标题, 说明)``。
#:
#: 说明里写上路线图编号,是为了让"为什么这页是空的"有据可查,而不是一句"敬请期待"。
#: "本地缓存"原来也在这张表里,现在它是真的页面了(路线图 M2.2)。
_PLACEHOLDER_PAGES: dict[str, tuple[str, str]] = {
    "discover": ("发现", "排行榜与内容发现还没实现(路线图 M3)"),
}

#: 歌单相关入口的占位说明。
_PLAYLIST_HINT = "歌单功能还没实现(路线图 M5,当前冻结)"


class MainWindow(FramelessWindow):
    """主窗口。

    Args:
        client: 接口客户端;``None`` 时使用默认的 Qt 后端实现。
        cache: 音频缓存;``None`` 时使用平台默认缓存目录。
        cover_cache: 封面磁盘缓存;``None`` 时使用平台默认缓存目录。
        config_store: 配置读写;``None`` 时使用平台默认配置路径。
        playback: 播放编排器;``None`` 时用 ``client`` 与 ``cache`` 现搭一个。
            传入替身(duck typing)即可在测试里跑通整条界面接线。
        downloader: 批量缓存的调度器;``None`` 时用 ``client`` 与 ``cache`` 现搭一个。
            与 ``playback`` 相互独立(见 ``audio/downloader.py``:播放用的解析器是
            单任务状态机,复用会被对方顶掉)。
        library: 本地库(缓存索引与播放历史的落点);``None`` 时优先用注入的
            ``cache.db``,否则用平台默认库文件。测试必须让它落到沙箱目录,
            否则会写到用户真实的库里。
    """

    def __init__(
        self,
        client: BilibiliClient | None = None,
        *,
        cache: AudioCache | None = None,
        cover_cache: CoverCache | None = None,
        config_store: ConfigStore | None = None,
        playback: PlaybackController | None = None,
        downloader: Downloader | None = None,
        library: LibraryDb | None = None,
    ) -> None:
        """建依赖、建界面、接线,并套用配置。

        Args:
            client: 接口客户端。
            cache: 音频缓存。
            cover_cache: 封面磁盘缓存。
            config_store: 配置读写。
            playback: 播放编排器。
            downloader: 批量缓存的调度器。
            library: 本地库。
        """
        super().__init__()
        self.setWindowTitle("BiliMusic")
        self.resize(1180, 760)
        self._apply_window_icon()

        self.client = client if client is not None else BilibiliClient()
        # 库只解析一次、只建一个连接:缓存索引与播放历史写的是同一个库文件,
        # 各建一个连接虽然能跑,但两边看到的数据就不再是同一份了
        self.library = (
            library
            if library is not None
            else (cache.db if cache is not None else LibraryDb())
        )
        self.cache = cache if cache is not None else AudioCache(db=self.library)
        self.cover_cache = (
            cover_cache if cover_cache is not None else CoverCache()
        )
        self.config_store = config_store if config_store is not None else ConfigStore()
        self.playback = (
            playback
            if playback is not None
            else PlaybackController(
                AudioResolver(self.client, self.cache), PlayerController(self)
            )
        )
        # 批量缓存走**自己的**一条管线:AudioResolver 是单任务状态机,再调一次
        # resolve() 就会取消上一个 —— 共用的话,用户一点播放就把缓存任务掐死了
        self.downloader = (
            downloader
            if downloader is not None
            else Downloader(
                self.client,
                self.cache,
                store=DownloadTaskStore(self.library),
                now_playing=self._now_playing_page,
            )
        )

        self._videos: list[Video] = []
        #: 当前结果集里出现过的 bvid。**去重是必需的,不是保险**:实测同一次搜索的相邻
        #: 两页之间会重叠若干条(排序在两次请求之间会变),不查重就会在列表里出现重复行。
        self._seen_bvids: set[str] = set()
        #: 当前结果对应的关键字;空串表示还没搜过(翻页时要靠它)
        self._search_keyword = ""
        #: 已经拿到手的页码(第 1 页记 1);0 表示一条都还没有
        self._loaded_page = 0
        #: 接口在这一页回的总页数(``numPages``);它是按接口**实际**使用的 pagesize 算的
        self._total_pages = 1
        #: 还能不能接着翻页。由 :meth:`_decide_has_more` 统一裁决,别处只读不写
        self._has_more = False
        #: 是否有搜索请求在飞。挡住"滚动到底"的重复触发(滚一下会来好几个 valueChanged)
        self._searching = False
        #: 在飞的搜索句柄(用来取消);没有请求时为 ``None``
        self._search_handle: FetchHandle | None = None
        #: 请求序号:每次发起新请求就 +1。响应回来时对不上号就丢弃 —— 否则"搜索 A 的
        #: 第 2 页"会追加进"搜索 B 的结果里(与 resolver 里 ``_alive`` 是同一类防护)
        self._search_token = 0
        #: 最近一次位置信号里的毫秒数;落盘只在切歌与退出时做(见 _remember_position)
        self._position_ms = 0
        self._config = self.config_store.load()
        #: 最近播放的读写。条数上限来自配置(可手改 ``config.json``,没有界面入口)。
        #: 必须在读到配置**之后**建:它要把 ``history_limit`` 带进每一次裁剪。
        self.history = PlayHistory(self.library, limit=self._config.history_limit)
        #: 播放条封面用的加载器。它只关心"当前这一首",所以还要配合 is_current 过滤
        self.cover_loader = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )
        #: 搜索列表与队列面板各自一个加载器:两边都会整体换内容,共用一个的话
        #: "列表换了一批"会把另一边还在排队的封面一起清掉。
        #: 本地缓存页同理(它的内容会随过滤框每一次输入整体重绘)。
        #: 四个加载器**共用同一个磁盘缓存**:同一张图在播放条与列表里是同一个 URL,
        #: 谁的队列先走到就落盘,另一个直接命中,不会重复下载。
        self.list_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )
        self.queue_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )
        self.cache_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )
        self.history_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )

        self._build_ui()
        #: 下载任务对话框。**懒建、只建一个**:非模态窗口关掉只是隐藏,
        #: 再点"下载任务"应该看到同一条列表,而不是又开一个窗
        self.task_dialog: TaskDialog | None = None
        self._connect()
        self._apply_config()
        # 启动时先把任务角标刷对(上次没下完的任务还在库里等着)
        self.cache_page.set_task_summary(self.downloader.active_count())

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        """组装标题栏、侧栏、内容页、队列面板与播放条。"""
        root = QWidget()
        root.setObjectName("AppRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_bar = TitleBar()
        layout.addWidget(self.title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 12, 12)
        body.setSpacing(12)
        self.sidebar = Sidebar()
        body.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.results_page = self._build_results_page()
        self.cache_page = CachePage(self.cache_covers)
        self.history_page = HistoryPage(self.history_covers)
        self.placeholder_page = PlaceholderPage()
        self.pages.addWidget(self.results_page)
        self.pages.addWidget(self.cache_page)
        self.pages.addWidget(self.history_page)
        self.pages.addWidget(self.placeholder_page)
        body.addWidget(self.pages, 1)

        self.queue_drawer = QueueDrawer(self.queue_covers)
        body.addWidget(self.queue_drawer)
        layout.addLayout(body, 1)

        self.player_bar = PlayerBar()
        # 分P菜单浮在内容区之上,但不该压住右侧的队列面板 —— 面板是主窗口装的,
        # 播放条自己看不到它,所以把引用递进去
        self.player_bar.set_page_menu_avoid_widget(self.queue_drawer)
        layout.addWidget(self.player_bar)

        self.setCentralWidget(root)
        self.sidebar.set_active_page("results")
        self._set_queue_visible(True)

    def _build_results_page(self) -> QWidget:
        """建"搜索标题行 + 结果列表 + 状态与进度"这一页。"""
        page = QWidget()
        page.setObjectName("CenterPanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(10)

        layout.addLayout(self._build_results_header())

        self.result_list = TrackList(
            _SEARCH_COLUMNS,
            covers=self.list_covers,
            action_column=_ACTION_COLUMN,
            centered=(3, _PAGES_COLUMN),
        )
        self.result_list.row_activated.connect(self._on_result_activated)
        self.result_list.row_menu_requested.connect(self._on_result_menu)
        self.result_list.add_requested.connect(self._on_result_add)
        layout.addWidget(self.result_list, 1)

        layout.addLayout(self._build_footer())
        return page

    def _build_results_header(self) -> QHBoxLayout:
        """建"搜索结果 + 关键字 + 命中数量"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.page_title_label = QLabel("搜索结果")
        self.page_title_label.setObjectName("PageTitle")
        self.query_label = QLabel("")
        self.query_label.setObjectName("PageQuery")
        self.total_label = QLabel("")
        self.total_label.setObjectName("MutedLabel")

        row.addWidget(self.page_title_label)
        row.addWidget(self.query_label)
        row.addStretch(1)
        row.addWidget(self.total_label)
        return row

    def _build_footer(self) -> QHBoxLayout:
        """建列表下方的"状态文字 + 重试按钮 + 缓存进度条"这一行。

        "加载更多"按钮平时是**隐藏**的:正常翻页由"滚到底"自动触发,不需要用户点。
        它只在翻页失败时露出来 —— 滚动触发是隐式的,失败了必须给一个看得见、点得动的
        出口,否则用户只会看到列表不再增长。
        """
        row = QHBoxLayout()
        row.setSpacing(10)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusLabel")
        row.addWidget(self.status_label, 1)

        self.load_more_button = QPushButton("加载更多")
        self.load_more_button.setObjectName("GhostTextButton")
        self.load_more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more_button.setVisible(False)
        self.load_more_button.clicked.connect(self._load_more)
        row.addWidget(self.load_more_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(180)
        self.progress.setVisible(False)
        row.addWidget(self.progress)
        return row

    def _connect(self) -> None:
        """把界面信号接到编排层,把编排层信号接到界面。"""
        self.playback.track_changed.connect(self._on_track_changed)
        self.playback.audio_ready.connect(self._on_audio_ready)
        self.playback.queue_changed.connect(self._on_queue_changed)
        self.playback.mode_changed.connect(self._on_mode_changed)
        self.playback.state_changed.connect(self.player_bar.set_playing)
        self.playback.position_changed.connect(self._on_position_changed)
        self.playback.progress.connect(self._on_progress)
        self.playback.error_occurred.connect(self._on_error)
        self.playback.stopped.connect(self._on_stopped)

        self.player_bar.play_toggled.connect(self.playback.toggle)
        self.player_bar.next_requested.connect(self._on_next)
        self.player_bar.previous_requested.connect(self.playback.previous)
        self.player_bar.mode_changed.connect(self.playback.set_mode)
        self.player_bar.quality_changed.connect(self.playback.set_quality)
        self.player_bar.page_selected.connect(self.playback.play_page)
        self.player_bar.seek_requested.connect(self.playback.seek)
        self.player_bar.volume_changed.connect(self._on_volume_changed)
        self.player_bar.queue_toggled.connect(self._set_queue_visible)

        self.queue_drawer.row_activated.connect(self.playback.jump_to)
        self.queue_drawer.remove_requested.connect(self.playback.remove_at)
        self.queue_drawer.clear_requested.connect(self.playback.clear)

        self.title_bar.search_requested.connect(self.on_search)
        # 列表只报"快到底了",要不要真去翻页由 _load_more 一处决定
        self.result_list.load_more_requested.connect(self._load_more)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximized)
        self.title_bar.close_requested.connect(self.close)

        self.sidebar.nav_selected.connect(self._on_nav_selected)
        self.sidebar.playlist_selected.connect(self._on_playlist_selected)
        self.sidebar.create_playlist_requested.connect(self._on_create_playlist)

        self.cache_page.row_activated.connect(self._on_cache_activated)
        self.cache_page.row_menu_requested.connect(self._on_cache_menu)
        self.cache_page.add_requested.connect(self._on_cache_add)
        self.cache_page.remove_requested.connect(self._on_cache_remove)
        self.cache_page.clear_requested.connect(self._on_cache_clear)
        self.cache_page.tasks_requested.connect(self._show_task_dialog)
        self.cache_page.open_dir_requested.connect(self._open_cache_dir)

        self.downloader.tasks_changed.connect(self._on_tasks_changed)
        self.downloader.task_updated.connect(self._on_task_updated)
        self.downloader.task_failed.connect(self._on_task_failed)

        self.history_page.row_activated.connect(self._on_history_activated)
        self.history_page.row_menu_requested.connect(self._on_history_menu)
        self.history_page.add_requested.connect(self._on_history_add)
        self.history_page.remove_requested.connect(self._on_history_remove)
        self.history_page.clear_requested.connect(self._on_history_clear)

        self.cover_loader.loaded.connect(self._on_cover_loaded)
        self.cover_loader.failed.connect(self._on_cover_failed)
        self.list_covers.loaded.connect(self.result_list.set_cover)
        self.queue_covers.loaded.connect(self.queue_drawer.set_cover)
        self.cache_covers.loaded.connect(self.cache_page.set_cover)
        self.history_covers.loaded.connect(self.history_page.set_cover)

    def _apply_window_icon(self) -> None:
        """设置窗口/任务栏图标。

        应用图标是位图 ``.ico``(含 16~256 共 7 档尺寸),不能用 SVG 替代 ——
        Windows 的任务栏与标题栏要按 DPI 自行挑档渲染。
        """
        path = app_icon_path()
        if path is None:
            return  # 资源缺失不该让应用起不来
        icon = QIcon(str(path))
        if icon.isNull():
            return
        self.setWindowIcon(icon)

    def _apply_config(self) -> None:
        """把读到的配置推给播放器与播放条(音量、播放模式),并套用主题。"""
        volume = self._config.volume / 100.0
        self.player_bar.set_volume(volume)
        self.playback.set_volume(volume)
        self.player_bar.set_mode(self._config.play_mode)
        self.playback.set_mode(self._config.play_mode)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app)

    # ------------------------------------------------------------ 搜索

    def on_search(self) -> None:
        """发起**新**搜索(回车或点按钮都会走到这里)。

        新搜索一律从第 1 页重来:作废在飞的旧请求、清掉上一次的结果与页码,再取第一页。
        请求是异步的,所以过程中禁掉搜索按钮 —— 否则用户连点会叠出多个请求,在这个接口上
        等于自找风控。
        """
        keyword = self.title_bar.search_input.text().strip()
        if not keyword:
            return
        self._search_keyword = keyword
        self._videos = []
        self._seen_bvids.clear()
        self._loaded_page = 0
        self._total_pages = 1
        self._has_more = False
        self.query_label.setText(f"「{keyword}」")
        self.total_label.setText("")
        # 立刻清空旧结果:留着上一批会让人以为"这次搜索搜出了这些"
        self.result_list.set_tracks([])
        self.result_list.set_highlight(-1)
        self._request_search_page(1)

    def _request_search_page(self, page: int) -> None:
        """请求某一页搜索结果,并给这次请求编号。

        Args:
            page: 页码,从 1 开始。
        """
        self._cancel_search_request()  # 同一时刻只允许一个搜索请求在飞
        token = self._search_token
        self._searching = True
        self.load_more_button.setVisible(False)
        self.title_bar.search_button.setEnabled(False)
        self.status_label.setText(
            f"正在搜索「{self._search_keyword}」…"
            if page == 1
            else f"正在加载第 {page} 页…"
        )
        self._search_handle = self.client.search_video(
            self._search_keyword,
            page=page,
            on_success=lambda result: self._on_search_page(result, page, token),
            on_error=lambda exc: self._on_search_failed(exc, token),
        )

    def _cancel_search_request(self) -> None:
        """取消在飞的搜索请求,并让它的回调作废。

        只靠 ``cancel()`` 是不够的:两个后端都保证"取消后不再回调",但换关键字与翻页
        都是"发新的",先自增序号最稳妥 —— 回来得再快也认不出这是哪一次的了。
        """
        handle = self._search_handle
        self._search_handle = None
        if handle is not None:
            handle.cancel()
        self._search_token += 1

    def _on_search_page(self, result: SearchResult, page: int, token: int) -> None:
        """某一页搜索结果到了:第 1 页替换列表,后续页追加(并按 bvid 去重)。

        Args:
            result: 接口返回的这一页。
            page: 请求时用的页码。
            token: 请求序号;与当前序号对不上说明这次结果已经过期,直接丢弃。
        """
        if token != self._search_token:
            return  # 用户已经换了关键字(或这次请求已被取消),这份结果不能再用了
        self._searching = False
        self._search_handle = None
        self.title_bar.search_button.setEnabled(True)

        fresh = [video for video in result.videos if video.bvid not in self._seen_bvids]
        self._seen_bvids.update(video.bvid for video in fresh)
        self._videos.extend(fresh)

        rows = [self._track_row_of(video) for video in fresh]
        if page == 1:
            self.result_list.set_tracks(rows)
            self.result_list.set_highlight(-1)
        else:
            self.result_list.append_tracks(rows)

        self._loaded_page = max(self._loaded_page, page)
        self._total_pages = max(1, result.total_pages)
        self._has_more = self._decide_has_more(page, result.page, fresh)
        self.total_label.setText(f"共找到 {result.total} 个视频")
        self._show_search_status()

    def _on_search_failed(self, exc: Exception, token: int) -> None:
        """某一页失败:如实报错,并留一个能重试的出口。

        第 1 页失败走弹窗(用户刚点了搜索,必须让他看见);后续页是滚动自动触发的,
        为它弹一个模态框太打扰(用户可能只想接着看已经加载出来的),所以改用状态栏 +
        重试按钮。
        """
        if token != self._search_token:
            return
        self._searching = False
        self._search_handle = None
        self.title_bar.search_button.setEnabled(True)
        if self._loaded_page == 0:
            self._fail(f"搜索失败:{exc}")
            return
        self.status_label.setText(f"第 {self._loaded_page + 1} 页加载失败:{exc}")
        self.load_more_button.setText(f"重试第 {self._loaded_page + 1} 页")
        self.load_more_button.setVisible(True)

    def _load_more(self) -> None:
        """加载下一页(滚到底自动触发,失败后也可以点按钮重来)。

        这里是**唯一的**翻页入口,所以重复触发全在这一处挡掉。
        """
        if self._searching or not self._search_keyword or not self._has_more:
            return
        self._request_search_page(self._loaded_page + 1)

    def _decide_has_more(self, page: int, served_page: int, fresh: Sequence[Video]) -> bool:
        """裁决"还能不能接着翻页"。

        四个停止条件,每一个都有实测依据:

        * **本页没带来任何新条目**:搜索排序在两次请求之间会变,翻页本来就可能"全是旧的";
          而且实测**越界页码会被静默当成第 1 页返回**(``page=1000`` 回的是第 1 页的数据),
          只信 ``numPages`` 的话一旦对不上就会永远重复拉第 1 页 —— 这条是兜底。
        * **接口回填的页码与请求的不一致**:同上,是"这一页其实不存在"的更直接证据。
        * **到了接口说的末页**:``numPages`` 按接口**实际**用的 pagesize 算(实测传 30 却
          回了 20 条时它是 50,正常回 30 条时是 34),所以它只能用当页的值,不能缓存。
        * **到了自设上限**:见 :data:`_SEARCH_MAX_PAGES`。

        Args:
            page: 本次请求的页码。
            served_page: 接口在响应里回的页码(``data.page``)。
            fresh: 本页**去掉重复之后**真正新增的条目。

        Returns:
            还能继续翻页返回 ``True``。
        """
        if not fresh:
            return False
        if page > 1 and served_page != page:
            return False
        if page >= _SEARCH_MAX_PAGES:
            return False
        return page < self._total_pages

    def _show_search_status(self) -> None:
        """把"已加载多少 / 还能不能继续"写到底部状态栏。"""
        loaded = len(self._videos)
        if self._has_more:
            self.status_label.setText(f"已加载 {loaded} 条,继续下滑加载更多")
        elif self._loaded_page >= _SEARCH_MAX_PAGES:
            self.status_label.setText(
                f"已加载 {loaded} 条(最多加载 {_SEARCH_MAX_PAGES} 页)"
            )
        else:
            self.status_label.setText(f"已加载全部 {loaded} 条")

    @staticmethod
    def _track_row_of(video: Video) -> TrackRow:
        """把搜索结果摊成列表行。

        UP主单独占一列,所以标题行里不再重复它;副标题放播放量 —— 搜索接口给的可用信息
        就这些(简介要到详情接口才有),总比空着一条线强。标题里的"歌手 - "前缀会被拆出来
        弱化显示(见 :func:`~bilibili_music.ui.widgets.track_list.split_title_prefix`)。

        Args:
            video: 搜索结果里的视频。

        Returns:
            可直接交给 ``TrackList`` 的展示数据。
        """
        subtitle = f"{format_count(video.play_count)} 播放" if video.play_count else ""
        prefix, title = split_title_prefix(video.title)
        return TrackRow(
            title=title,
            prefix=prefix,
            subtitle=subtitle,
            cover_url=video.cover_https,
            columns=(video.author, video.duration_text, _UNKNOWN_PAGES),
        )

    def _video_at(self, row: int) -> Video | None:
        """取某一行对应的视频;越界返回 ``None``。"""
        if 0 <= row < len(self._videos):
            return self._videos[row]
        return None

    def _on_result_activated(self, row: int) -> None:
        """双击搜索结果:整个结果列表成为队列,从这一行开始播。"""
        if self._video_at(row) is None:
            return
        self.playback.play_queue(
            [QueueItem(video=video) for video in self._videos], start=row
        )

    def _on_result_add(self, row: int) -> None:
        """点行内"+":把这一行加到队列末尾。"""
        video = self._video_at(row)
        if video is None:
            return
        self.playback.enqueue(QueueItem(video=video))
        self.status_label.setText(f"已加入播放队列:{video.title}")

    def _on_result_menu(self, row: int, position) -> None:  # noqa: ANN001 - QPoint
        """搜索结果右键菜单:播放 / 下一首播放 / 加入队列 / 缓存整个合集 / 在B站打开。"""
        video = self._video_at(row)
        if video is None:
            return
        menu = QMenu(self)
        play_action = menu.addAction("播放")
        next_action = menu.addAction("下一首播放")
        queue_action = menu.addAction("加入队列")
        menu.addSeparator()
        cache_action = menu.addAction(self._cache_all_label(video))
        menu.addSeparator()
        open_action = menu.addAction("在B站打开")

        chosen = menu.exec(position)
        if chosen is play_action:
            self._on_result_activated(row)
        elif chosen is next_action:
            self.playback.enqueue_next(QueueItem(video=video))
        elif chosen is queue_action:
            self.playback.enqueue(QueueItem(video=video))
        elif chosen is cache_action:
            self._cache_all_pages(video)
        elif chosen is open_action:
            QDesktopServices.openUrl(QUrl(video.web_url))

    # ------------------------------------------------------------ 批量缓存

    @staticmethod
    def _cache_all_label(video: Video) -> str:
        """右键菜单里"缓存整个合集"那一项的文字。

        分P数已知(用户刚播过、详情已在手)且多于一个时直接写出来 —— 点之前就该知道
        这一下要下 200 首歌。分P数未知时**不瞎猜**(搜索结果只给表面信息):写成
        "缓存到本地",具体数量留给确认框说 —— 那时详情已经取回来了。

        Args:
            video: 右键那一行对应的视频。

        Returns:
            菜单项文字。
        """
        if video.part_count > 1:
            return f"缓存全部 {video.part_count} 个分P"
        return "缓存到本地"

    def _cache_all_pages(self, video: Video) -> None:
        """右键"缓存整个合集":先确认,再把整个合集交给下载队列。

        分P列表还没到手时先补一次详情:确认框要写清"共 N 个分P,已缓存 M 个",
        这两个数字都离不开分P列表。详情命中 client 缓存时回调是同步的。

        Args:
            video: 目标视频。
        """
        if self.downloader.blocks_new_task(video.bvid):
            QMessageBox.information(
                self,
                "缓存到本地",
                f"「{video.title}」已经在下载任务列表里了,"
                "可以在「本地缓存 → 下载任务」里看进度。",
            )
            return
        if video.pages:
            self._confirm_cache_all(video)
            return
        self.status_label.setText(f"正在获取「{video.title}」的分P列表…")
        self.client.fetch_video(
            video.bvid,
            on_success=self._confirm_cache_all,
            on_error=lambda exc: self._fail(str(exc)),
        )

    def _confirm_cache_all(self, video: Video) -> None:
        """确认并开始缓存:说清要下多少个分P,同意后入队。

        默认按钮是"是"(与删除缓存的确认框相反):点这个菜单项本身就是用户的明确意图,
        确认框只是把数量告诉他。

        Args:
            video: 目标视频(分P列表已就绪)。
        """
        pages = video.pages
        if not pages:
            QMessageBox.information(
                self, "缓存到本地", "没能取到这个视频的分P列表,稍后再试试。"
            )
            return
        if self.downloader.blocks_new_task(video.bvid):
            # 详情是异步取的,等它回来的这段时间里任务可能已经被建好了(连点两次右键)
            QMessageBox.information(self, "缓存到本地", "这个视频已经在下载任务列表里了。")
            return
        cached = self.downloader.cached_page_count(video)
        remaining = len(pages) - cached
        if remaining <= 0:
            QMessageBox.information(
                self,
                "缓存到本地",
                f"「{video.title}」的 {len(pages)} 个分P都已经在本地了。",
            )
            return
        answer = QMessageBox.question(
            self,
            "缓存到本地",
            f"「{video.title}」共 {len(pages)} 个分P,其中 {cached} 个已在本地。\n"
            f"将串行缓存剩余 {remaining} 个(已缓存的自动跳过),下载期间可以正常播放。\n"
            "进度在「本地缓存 → 下载任务」里,任务可以随时暂停。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self.downloader.enqueue(video):
            QMessageBox.information(
                self, "缓存到本地", "这个视频没有可缓存的分P,或者已经在任务列表里了。"
            )
            return
        self.status_label.setText(
            f"已开始缓存「{video.title}」(共 {len(pages)} 个分P)"
        )

    # ------------------------------------------------------------ 导航

    def _on_nav_selected(self, key: str) -> None:
        """侧栏入口被点:切换中间的内容页。

        "本地缓存"与"最近播放"两页在切过来时都会**重读一次库**:用户可能在应用之外
        删过文件、或用 sqlite 工具动过库,而且这两页在别的页面显示期间不会刷新。
        其它页面只是切页,没有这种"内容会过期"的问题。

        Args:
            key: :data:`~bilibili_music.ui.widgets.sidebar.NAV_ITEMS` 里的键。
        """
        if key == "cache":
            self._show_page(self.cache_page, "cache")
            self._refresh_cache_page()
            return
        if key == "history":
            self._show_page(self.history_page, "history")
            self._refresh_history_page()
            return
        placeholder = _PLACEHOLDER_PAGES.get(key)
        if placeholder is None:
            self._show_page(self.results_page, "results")
            return
        title, hint = placeholder
        self.placeholder_page.set_content(title, hint)
        self._show_page(self.placeholder_page, key)

    def _on_playlist_selected(self, name: str) -> None:
        """点了某个歌单:歌单功能还没做,先给一张说清楚原因的占位页。"""
        self.placeholder_page.set_content(name, _PLAYLIST_HINT)
        self._show_page(self.placeholder_page, None)

    def _on_create_playlist(self) -> None:
        """点了"新建歌单":同上,进占位页。"""
        self.placeholder_page.set_content("新建歌单", _PLAYLIST_HINT)
        self._show_page(self.placeholder_page, None)

    def _show_page(self, page: QWidget, nav_key: str | None) -> None:
        """切换内容页,并让侧栏的选中态跟上。

        Args:
            page: 要显示的内容页。
            nav_key: 对应的侧栏按键;``None`` 表示这一页没有侧栏入口(歌单类页面),
                此时侧栏上不选中任何页面入口。
        """
        self.pages.setCurrentWidget(page)
        if nav_key is not None:
            self.sidebar.set_active_page(nav_key)

    def _set_queue_visible(self, visible: bool) -> None:
        """设置队列面板的可见性,并同步播放条上那个开关的外观。

        真正的入口只有这一个方法,而不是让控件自己维护"我以为队列是开着的"。

        Args:
            visible: 是否显示队列面板。
        """
        self.queue_drawer.setVisible(bool(visible))
        self.player_bar.set_queue_visible(bool(visible))

    def _toggle_maximized(self) -> None:
        """在最大化与还原之间切换(标题栏按钮与双击标题栏都走这里)。"""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ------------------------------------------------------------ 本地缓存

    def _refresh_cache_page(self) -> None:
        """把缓存索引读一遍推给"本地缓存"页。

        每次都**重读磁盘**并剪掉"文件已经不在"的条目:用户可能在应用之外删过文件,或者
        上一轮进程异常退出留下了孤儿记录 —— 只有重读才对得上。两件事都很便宜(读一个
        小 JSON,剪枝只在真变了时才落盘),不值得为此维护一份内存副本。

        为什么要先 ``reload`` 再 ``prune``:剪枝要依据**磁盘上现存的**文件名,
        而内存里那份可能是几分钟前读的。
        """
        self.cache.index.reload()
        self.cache.prune()
        self.cache_page.set_tracks(self.cache.index.entries(), self.cache.size_bytes())

    def _cache_queue_items(self) -> list[QueueItem]:
        """把"本地缓存"页**当前显示**的歌摊成队列项(过滤后的那一批)。

        双击某一首 = 把眼前这张列表整列变成队列 —— 与搜索结果页同一套行为,
        于是"本地缓存"顺带就是一个离线歌单。

        Returns:
            队列项列表,顺序与页面上的行号一致。
        """
        return [self._queue_item_of(entry) for entry in self.cache_page.visible_entries()]

    @staticmethod
    def _queue_item_of(entry: PlayableEntry) -> QueueItem:
        """把一条记录(缓存索引的或播放历史的)变成队列项。

        分P序号**必须**一起带上:记录里存的是那一P(可能是第 3P),不带的话
        解析器会退回视频级 cid —— 那是第 1P,多P合集于是串歌(领域铁律)。

        Args:
            entry: 索引记录或播放记录。

        Returns:
            可直接入队的项。
        """
        return QueueItem(video=video_from_entry(entry), page_index=entry.page_index)

    def _on_cache_activated(self, row: int) -> None:
        """双击缓存里的一首:整个可见列表成为队列,从这一行开始播。

        这条路径**零网络请求**:``Video`` 是用索引记录拼出来的,解析器查到它有分P列表
        就不会再请求详情,紧接着又被缓存命中(见 ``audio/resolver.py``)。所以断网也能放。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        items = self._cache_queue_items()
        if not items:
            return
        self.playback.play_queue(items, start=row)

    def _on_cache_add(self, row: int) -> None:
        """点行内"+":把这一首加到播放队列末尾。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        entry = self.cache_page.entry_at(row)
        if entry is None:
            return
        self.playback.enqueue(self._queue_item_of(entry))

    def _on_cache_menu(self, row: int, position) -> None:  # noqa: ANN001 - QPoint
        """本地缓存右键菜单:播放 / 下一首播放 / 加入队列 / 从缓存删除 / 在B站打开。

        Args:
            row: 行号(以过滤后的列表为准)。
            position: 弹出位置(全局坐标)。
        """
        entry = self.cache_page.entry_at(row)
        if entry is None:
            return
        menu = QMenu(self)
        play_action = menu.addAction("播放")
        next_action = menu.addAction("下一首播放")
        queue_action = menu.addAction("加入队列")
        menu.addSeparator()
        remove_action = menu.addAction("从缓存删除")
        open_action = menu.addAction("在B站打开")

        chosen = menu.exec(position)
        if chosen is play_action:
            self._on_cache_activated(row)
        elif chosen is next_action:
            self.playback.enqueue_next(self._queue_item_of(entry))
        elif chosen is queue_action:
            self.playback.enqueue(self._queue_item_of(entry))
        elif chosen is remove_action:
            self._delete_cached(entry)
        elif chosen is open_action:
            QDesktopServices.openUrl(QUrl(video_from_entry(entry).web_url))

    def _on_cache_remove(self, row: int) -> None:
        """请求删除某一行(操作列"⋮"菜单里的那一项也会走到这里)。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        entry = self.cache_page.entry_at(row)
        if entry is not None:
            self._delete_cached(entry)

    def _delete_cached(self, entry: CachedTrack) -> None:
        """确认后删掉一条缓存(音频文件 + 索引记录)并刷新页面。

        删除**不可撤销**(再听要重新下载),所以先问一句;默认按钮是"否",避免一路回车
        就把歌删了。

        删不掉时说明原因而不是静默失败:最可能的情况是文件正被播放器占着(Windows 不允许
        删除已打开的文件),用户需要知道"要去停掉播放",而不是以为按钮坏了。

        Args:
            entry: 要删除的索引记录。
        """
        answer = QMessageBox.question(
            self,
            "删除缓存",
            f"要删除「{entry.title or entry.bvid}」的缓存吗?再听需要重新下载。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self.cache.forget(entry):
            QMessageBox.warning(
                self, "删除缓存", "这个文件正在被播放器使用,停掉播放后再试一次。"
            )
            return
        self._refresh_cache_page()

    def _on_cache_clear(self) -> None:
        """确认后清空全部缓存(**不动播放历史**)。

        清完**当场再量一次占用**:删不掉的文件(正被播放器占用)会被跳过,若一声不吭地
        只报"已清空",界面上那个占用数字就会纹丝不动地留在那里,用户完全无从理解。
        """
        if not self.cache.index.entries():
            return
        answer = QMessageBox.question(
            self,
            "清空缓存",
            "要删除所有已缓存的音频文件吗?再听需要重新下载。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.cache.clear()
        self._refresh_cache_page()
        left = self.cache.size_bytes()
        if left:
            QMessageBox.warning(
                self,
                "清空缓存",
                f"已删除 {removed} 首,但还有 {format_size(left)} 正在被使用,未能删除。",
            )

    # ------------------------------------------------------------ 下载任务

    def _show_task_dialog(self) -> None:
        """打开(或重新显示)下载任务对话框。

        实例**懒建、只建一个**:非模态窗口关掉只是隐藏,任务照样在后台跑,
        再点"下载任务"应该看到同一条列表而不是又开一个窗。
        信号接到下载器上直接调它的方法:控件只发"用户想干什么",
        改哪个任务、怎么改由调度器决定(``AGENTS.md`` 第 4 节)。
        """
        if self.task_dialog is None:
            self.task_dialog = TaskDialog(self.downloader.page_progress, self)
            self.task_dialog.pause_requested.connect(self.downloader.pause)
            self.task_dialog.resume_requested.connect(self.downloader.resume)
            self.task_dialog.remove_requested.connect(self.downloader.remove)
            self.task_dialog.pause_all_requested.connect(self.downloader.pause_all)
            self.task_dialog.resume_all_requested.connect(self.downloader.resume_all)
            self.task_dialog.clear_finished_requested.connect(
                self.downloader.clear_finished
            )
        self.task_dialog.set_tasks(self.downloader.tasks())
        self.task_dialog.show()
        self.task_dialog.raise_()
        self.task_dialog.activateWindow()

    def _open_cache_dir(self) -> None:
        """在系统文件管理器里打开缓存目录。

        用 Qt 自带的 :class:`QDesktopServices` 而不是自己调 ``explorer`` / ``open`` /
        ``xdg-open``:它跳平台、不引依赖,也不用自己拼命令行(拼命令行最容易在带空格的
        路径上翻车)。

        目录不在时先建出来再打开:``AudioCache`` 建过它,但用户可能手动删掉了,
        而"点打开目录却发现目录不存在"这种失败完全没必要让用户看到。
        """
        directory = self.cache.root
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "打开缓存目录", f"缓存目录不可用:{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            QMessageBox.warning(
                self, "打开缓存目录", f"没能打开文件管理器,缓存目录在:\n{directory}"
            )

    def _on_tasks_changed(self) -> None:
        """任务集合、状态或"刚有分P落盘"变了:刷新角标、缓存页与对话框。

        "刚有分P落盘"也算结构变化(而不是普通进度):它会往缓存索引里添记录,
        正开着的"本地缓存"页要跟着出新行。
        """
        self.cache_page.set_task_summary(self.downloader.active_count())
        if self.pages.currentWidget() is self.cache_page:
            self._refresh_cache_page()
        if self.task_dialog is not None and self.task_dialog.isVisible():
            self.task_dialog.set_tasks(self.downloader.tasks())

    def _on_task_updated(self, bvid: str) -> None:
        """某个任务的进度数字变了:只刷新对话框里那一行。

        进度回调每秒会来好几次,这里**不能**重建整张列表,更不能刷新缓存页。
        """
        if self.task_dialog is None or not self.task_dialog.isVisible():
            return
        task = self.downloader.task(bvid)
        if task is not None:
            self.task_dialog.update_task(task)

    def _on_task_failed(self, bvid: str, message: str) -> None:
        """某个任务失败挂起:弹窗告知(失败必须让用户看见)。

        失败弹窗是**模态**的:任务已经停下且整条队列也停了,用户必须知道这件事,
        否则他会以为还在慢慢下。弹窗里同时告诉他去哪儿重试。
        """
        task = self.downloader.task(bvid)
        title = task.display_title if task is not None else bvid
        where = (
            f"第 {task.page_index} 个分P"
            if task is not None and task.page_index
            else "某个分P"
        )
        QMessageBox.warning(
            self,
            "缓存失败",
            f"「{title}」的{where}没能缓存下来,任务已暂停(排队中的任务也一并暂停了)。\n\n"
            f"{message}\n\n"
            "可以在「本地缓存 → 下载任务」里点「继续」重试,或者移除这条任务。",
        )

    def _now_playing_page(self) -> tuple[str, int] | None:
        """取"播放器当前正在处理的那一分P"的 ``(bvid, cid)``;没有则 ``None``。

        下载器用它避开"两条管线同时写同一个缓存文件"。注意判据是**编排层当前指向的
        那一P**,而不是"媒体正在出声":切歌后解析器还在下载的那一两秒,文件已经在写了。
        分P序号只有编排层知道(合集播到第 3P 时队列项记的仍是入队那一P),
        所以要经 ``playback.page_index`` 换算回 ``cid``。
        """
        item = self.playback.current
        if item is None:
            return None
        page = item.video.page(self.playback.page_index)
        if page is None:
            return None
        return (item.video.bvid, page.cid)

    # ------------------------------------------------------------ 最近播放

    def _refresh_history_page(self) -> None:
        """把播放历史读一遍推给"最近播放"页。

        与缓存页不同,这里**不需要剪枝**:历史记录不指向任何文件,不存在"文件已被手删"
        这回事 —— 它记的是"听过什么",而听过这件事不会因为文件没了就没发生。
        """
        self.history_page.set_entries(self.history.entries())

    def _history_queue_items(self) -> list[QueueItem]:
        """把"最近播放"页**当前显示**的歌摊成队列项(过滤后的那一批)。

        双击某一首 = 把眼前这张列表整列变成队列 —— 与搜索结果页、缓存页同一套行为。

        Returns:
            队列项列表,顺序与页面上的行号一致。
        """
        return [self._queue_item_of(entry) for entry in self.history_page.visible_entries()]

    def _on_history_activated(self, row: int) -> None:
        """双击最近播放里的一首:整个可见列表成为队列,从这一行开始播。

        已缓存的歌走缓存快路径(零网络请求),没缓存的照常解析下载 —— 与在搜索结果里
        双击没有区别,区别只在"这一行的数据从哪来"。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        items = self._history_queue_items()
        if not items:
            return
        self.playback.play_queue(items, start=row)

    def _on_history_add(self, row: int) -> None:
        """点行内"+":把这一首加到播放队列末尾。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        entry = self.history_page.entry_at(row)
        if entry is None:
            return
        self.playback.enqueue(self._queue_item_of(entry))

    def _on_history_menu(self, row: int, position) -> None:  # noqa: ANN001 - QPoint
        """最近播放右键菜单:播放 / 下一首播放 / 加入队列 / 从历史中删除 / 在B站打开。

        Args:
            row: 行号(以过滤后的列表为准)。
            position: 弹出位置(全局坐标)。
        """
        entry = self.history_page.entry_at(row)
        if entry is None:
            return
        menu = QMenu(self)
        play_action = menu.addAction("播放")
        next_action = menu.addAction("下一首播放")
        queue_action = menu.addAction("加入队列")
        menu.addSeparator()
        remove_action = menu.addAction("从历史中删除")
        open_action = menu.addAction("在B站打开")

        chosen = menu.exec(position)
        if chosen is play_action:
            self._on_history_activated(row)
        elif chosen is next_action:
            self.playback.enqueue_next(self._queue_item_of(entry))
        elif chosen is queue_action:
            self.playback.enqueue(self._queue_item_of(entry))
        elif chosen is remove_action:
            self._delete_history_entry(entry)
        elif chosen is open_action:
            QDesktopServices.openUrl(QUrl(video_from_entry(entry).web_url))

    def _on_history_remove(self, row: int) -> None:
        """请求删掉某一行(操作列"⋮"菜单里的那一项也会走到这里)。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        entry = self.history_page.entry_at(row)
        if entry is not None:
            self._delete_history_entry(entry)

    def _delete_history_entry(self, entry: HistoryEntry) -> None:
        """确认后删掉一条播放记录(**不碰缓存文件**)并刷新页面。

        确认框里的"已缓存的音频不会被删除"是刻意写的:用户对"删一条记录"的预期与
        "删一首歌"很容易混淆,而后者是不可逆的 —— 一句话就能避开这个误会。

        Args:
            entry: 要删除的记录。
        """
        answer = QMessageBox.question(
            self,
            "从历史中删除",
            f"要把「{entry.title or entry.bvid}」从最近播放里删掉吗?"
            "已缓存的音频不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.history.remove(entry.bvid, entry.cid)
        self._refresh_history_page()

    def _on_history_clear(self) -> None:
        """确认后清空全部播放记录(**不碰缓存文件**)。"""
        if not self.history.entries():
            return
        answer = QMessageBox.question(
            self,
            "清空历史",
            "要删除全部播放记录吗?已缓存的音频不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.history.clear()
        self._refresh_history_page()

    # ------------------------------------------------------------ 编排层回调

    def _on_track_changed(self, item: QueueItem) -> None:
        """换曲目:先把上一首的进度落盘,再更新界面。

        合集内部换分P也走这里(队列项没变,只是播的那一P变了),所以分P选择器要一起刷新。
        """
        self._remember_position()
        # 清掉上一首的封面(空 URL 会让加载器立刻发 failed → 显示占位图)。
        # 顺带把"在飞的旧封面请求"作废,否则它回来时会被当成当前封面贴上。
        self.cover_loader.load("")
        self.status_label.setText(f"正在解析「{item.title}」…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # 不定长进度,表示"忙碌"
        self.result_list.set_highlight(self._result_row_for(item.video.bvid))
        self.queue_drawer.set_current(self.playback.queue.current_index)
        self._sync_page_selector()

    def _on_audio_ready(self, resolved: ResolvedAudio) -> None:
        """音源就绪:更新标题、音质、时长,并记下"上次播的是哪一首"。

        **"最近播放"就在这里写一条**(而不是在换队列项时):到这一步才说明音频真的下好、
        真的要出声了 —— 解析失败或用户快速切歌的那些项不会在历史里留下痕迹。
        一次播放只写这一条,``position_changed`` 那种高频信号不参与。
        """
        self.progress.setVisible(False)
        track = resolved.track
        self.player_bar.set_now_playing(
            resolved.display_title,
            f"{resolved.subtitle} · {track.label}",
        )
        self.status_label.setText(f"已缓存 {resolved.path.name}")
        self.setWindowTitle(f"BiliMusic - {resolved.display_title}")
        self._mark_page_count(resolved)
        # 详情此刻才补全,分P列表到这会儿才可用 —— 再刷一次选择器,它才显示得出分P标题
        self._sync_page_selector()
        # "本地缓存"页也要跟着走:这一首刚被记进索引,而且它可能正是正在播的那一行
        self.cache_page.set_playing(resolved.video.bvid, resolved.page.cid)
        if self.pages.currentWidget() is self.cache_page:
            self._refresh_cache_page()
        self._remember_history(resolved.video, resolved.page)
        # 封面是异步的,而且允许失败:拿不到就用占位图,不打断播放
        self.cover_loader.load(resolved.video.cover_https)
        # 位置的落盘交给切歌与退出,这里只更新内存里的"上次播的是哪一首"
        self._config.last_bvid = resolved.video.bvid
        self._config.last_cid = resolved.page.cid

    def _remember_history(self, video: Video, page: Page) -> None:
        """把刚开播的这一首记进"最近播放";这一页正开着的话顺手刷新。

        只在**看得见这一页**时重读库,与缓存页同一套取舍:看不见的页面不值得为它反复查库。
        播放中的那一行标记则不分页面,因为它只影响这一页自己的渲染。

        Args:
            video: 正在播放的视频(详情已补全)。
            page: 正在播放的分P。
        """
        self.history.record(history_entry_for(video, page))
        self.history_page.set_playing(video.bvid, page.cid)
        if self.pages.currentWidget() is self.history_page:
            self._refresh_history_page()

    def _on_cover_loaded(self, url: str, pixmap: QPixmap) -> None:
        """封面到了就贴上;若已经切歌则忽略。

        加载器内部已经拦了一道过期响应,这里再确认一次是因为"当前"的含义在这里更严:
        只有仍是正在播的那一首的封面才配上屏。
        """
        if self.cover_loader.is_current(url):
            self.player_bar.set_cover(pixmap)

    def _on_cover_failed(self, url: str) -> None:
        """封面拿不到:退回占位图,不提示用户(为一张图打断播放不划算)。"""
        if url and not self.cover_loader.is_current(url):
            return
        self.player_bar.set_cover(None)

    def _on_queue_changed(self) -> None:
        """队列内容变了:重建抽屉列表并高亮当前项。"""
        self.queue_drawer.set_items(
            self.playback.queue.items, self.playback.queue.current_index
        )
        self._sync_page_selector()

    def _on_mode_changed(self, mode) -> None:  # noqa: ANN001 - PlayMode
        """播放模式变了:同步按钮外观并落盘(值没变就不写文件)。"""
        self.player_bar.set_mode(mode)
        if self._config.play_mode != mode:
            self._config.play_mode = mode
            self._save_config()

    def _on_position_changed(self, position: int, duration: int) -> None:
        """播放位置变化:推给播放条,并记住毫秒数备落盘。"""
        self.player_bar.set_position(position, duration)
        self._position_ms = max(0, position)

    def _on_progress(self, done: int, total: int) -> None:
        """缓存进度:总长已知时显示百分比,未知时只显示已收字节。"""
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(done / total * 100))
            self.status_label.setText(
                f"缓存中 {done / 1048576:.1f} / {total / 1048576:.1f} MB"
            )
        else:
            self.status_label.setText(f"缓存中 {done / 1048576:.1f} MB")

    def _on_volume_changed(self, volume: float) -> None:
        """音量变了:转给播放器并落盘。"""
        self.playback.set_volume(volume)
        level = int(round(volume * 100))
        if self._config.volume != level:
            self._config.volume = level
            self._save_config()

    def _on_next(self) -> None:
        """下一首:到底了就如实说,不让按钮看起来像坏了。"""
        if not self.playback.next():
            self.status_label.setText("已经是最后一首")

    def _on_stopped(self) -> None:
        """编排层说没有下一项了。"""
        self.progress.setVisible(False)
        self.status_label.setText("播放结束")

    def _on_error(self, message: str) -> None:
        """解析或播放失败:状态栏 + 弹窗(失败必须让用户看见)。"""
        self._fail(message)

    # ------------------------------------------------------------ 窗口状态

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        """窗口状态变化时让标题栏的最大化按钮跟上。

        最大化/还原可能来自标题栏按钮、双击标题栏,也可能是系统快捷键或拖到屏幕边缘,
        统一在这里同步,比在每个触发点各写一遍可靠。
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())

    # ------------------------------------------------------------ 配置

    def _remember_position(self) -> None:
        """把"上一首播到哪"写进配置。

        位置信号每几百毫秒就来一次,每次都落盘会把磁盘写花,所以只在**切歌**与**退出**
        这两个时刻保存。此时 ``last_bvid`` / ``last_cid`` 记的仍是上一首
        (新一首的 ``audio_ready`` 还没到),正好对上这个位置。
        """
        self._config.last_position_ms = self._position_ms
        self._save_config()

    def _save_config(self) -> None:
        """写配置;失败只提示,绝不让播放中断。"""
        try:
            self.config_store.save(self._config)
        except OSError as exc:
            self.status_label.setText(f"配置保存失败:{exc}")

    # ------------------------------------------------------------ 工具

    def _result_row_for(self, bvid: str) -> int:
        """在搜索结果里找某个视频的行号,找不到返回 ``-1``。"""
        for row, video in enumerate(self._videos):
            if video.bvid == bvid:
                return row
        return -1

    def _mark_page_count(self, resolved: ResolvedAudio) -> None:
        """详情补全后,把结果表里那一行的"分P"列填成真实数量。"""
        if not resolved.video.pages:
            return
        row = self._result_row_for(resolved.video.bvid)
        if row >= 0:
            self.result_list.set_cell(row, _PAGES_COLUMN, str(len(resolved.video.pages)))

    def _sync_page_selector(self) -> None:
        """把"当前视频 + 正在播的分P"推给播放条上的分P选择器。

        分P列表取自队列项持有的 ``Video``:解析器补全详情后会**写回同一个对象**
        (见 ``audio.resolver._after_detail``),所以详情到达前后都能读它 —— 只是到达前
        列表还是空的,选择器会先禁用、只显示分P序号。

        "正在播哪一P"读编排层的 :attr:`~bilibili_music.audio.playback.PlaybackController.page_index`:
        合集播到第 3P 时队列项记的仍是入队时的分P,拿 ``item.page_index`` 会指错行。

        没有当前项时推空列表:选择器显示占位并禁用,而不是留着上一首的分P。
        """
        item = self.playback.current
        if item is None:
            self.player_bar.set_pages((), 0)
            return
        self.player_bar.set_pages(item.video.pages, self.playback.page_index)

    def _fail(self, message: str) -> None:
        """统一失败出口:收进度条、恢复按钮、提示用户。"""
        self.progress.setVisible(False)
        self.status_label.setText(message)
        QMessageBox.warning(self, "出错了", message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        """退出前落盘进度,并停掉在飞的解析、下载与播放。"""
        self._config.last_position_ms = self._position_ms
        self._save_config()
        self.playback.stop()
        # 批量缓存也要收尾:取消在飞下载(顺带清掉半截的 .part)并把当前任务写成"暂停"
        self.downloader.shutdown()
        self.client.close()
        # 库连接在退出时显式关掉:WAL 的收尾交给 sqlite 自己做,不必留着连接等进程结束
        self.library.close()
        super().closeEvent(event)


def run(argv: list[str] | None = None) -> int:
    """应用入口。

    Args:
        argv: 命令行参数;``None`` 表示用 ``sys.argv``(由 ``bilimusic`` 脚本或
            ``python -m bilibili_music`` 进来时都是这个情况)。

    Returns:
        进程退出码,取自 ``QApplication.exec()``。
    """
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("BiliMusic")
    # 应用级图标:任务栏与 Alt+Tab 用得到,窗口级在 MainWindow 里另设一份
    app_icon = app_icon_path()
    if app_icon is not None:
        app.setWindowIcon(QIcon(str(app_icon)))
    apply_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
