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

侧栏入口里"发现"还没有对应功能(路线图 M3)—— 点开是一张把话说清楚的占位页,而不是点了
没反应(见 :class:`~bilibili_music.ui.widgets.placeholder.PlaceholderPage`)。"播放队列"不是
页面入口,它是右侧队列面板的开关,只有播放条上那一个按钮(见 ``PlayerBar.queue_button``)。

**"我的歌单"就是 B站 收藏夹(路线图 M5 S1)**:登录后把收藏夹列表填进侧栏
(:meth:`MainWindow._on_folders`),点一个就在内容区打开收藏夹页
(:class:`~bilibili_music.ui.widgets.fav_page.FavPage`)。登录态**只认** ``nav`` 的
``isLogin``:收藏夹接口在未登录时也返回 ``code=0``(只是 ``data`` 是 ``null``),
拿它判断会得到假阳性(见 README「账号链路」)。

收藏夹列表还带两个本地操作(侧栏「我的歌单」那两行按钮,见
:class:`~bilibili_music.ui.widgets.sidebar.Sidebar`):**「刷新」**重新从B站取一遍列表
(:meth:`MainWindow._on_refresh_playlists`)—— 列表是服务端的东西,在网页上新建 / 删除
之后本机不会自己知道;**「显示/隐藏」**打开
:class:`~bilibili_music.ui.widgets.fav_visibility_dialog.FavVisibilityDialog`,挑哪些夹子
出现在侧栏(:meth:`MainWindow._open_fav_visibility_dialog`)。后者是**纯本机偏好**,落
``config.json`` 的 ``fav_hidden_ids``,B站 上的收藏夹一个都不会动 —— 界面上也这么写,
免得用户以为自己在删东西。两条纪律与别的异步请求一致:刷新期间禁按钮挡连点(每次刷新
都是真请求,风控口径见 README「坑 3」)、用 ``_folders_token`` 让迟到的列表响应作废
(否则"刷新完再登出"会被一份旧列表把侧栏重新填满)。

**"本地缓存"页(路线图 M2.2)是真的**:它列出 ``AudioCache`` 索引里的已缓存音轨,
支持过滤、离线点播与删除;这一页的数据来自索引(见 ``core/cache_index.py``),而
"索引怎么维护"是解析层的事 —— 这里只负责在需要的时候读一遍推给界面
(见 :meth:`MainWindow._refresh_cache_page`)。

**"最近播放"页也是真的**:每次音频真正就绪时(``audio_ready``)往
:class:`~bilibili_music.core.history.PlayHistory` 记一条,切到这一页时读出来。
记录与缓存索引共用一个本地库(``core/library_db.py`` 下的 ``library.db``),但**分表**:
"清空缓存"不会影响历史。

**搜索历史也是真的**:每次提交搜索往 ``core/search_history.py`` 记一条(同一个词只留
最近一次、库里保留 50 条),点搜索框时在它下方给出最近搜过的词、输入时按**包含匹配**
过滤(最多显示 10 条),点一条就填回输入框并立即搜索。它同样落在 ``library.db`` 里,
但**另起一张表** —— "清空播放历史"不会顺手清掉搜索词,反之亦然。

**搜索结果是分页加载的**:接口一次只给一页(见 ``api.bilibili.py`` 的 ``search_video``),
列表滚到接近底部时自动取下一页并追加,失败时底部会出现一个"重试"按钮。页码、去重与
停止条件全在这里裁决(见 :meth:`MainWindow._decide_has_more`),``TrackList`` 只报
"用户快到底了"。

依赖以参数注入的只有"可替换的外部资源"(客户端 / 音频缓存 / 封面缓存 / 配置 / 编排器 /
本地库 / 会话存储):默认全部走真实实现,测试可以塞替身进来,于是界面接线能被自动化验证,
而不是只能靠肉眼点。**本地库必须与缓存一起注入同一个实例** —— 两者写的是同一个库文件,
各建一个连接虽然也能跑,但测试里一个漏注入就会写到用户真实的配置目录。
**会话存储也必须显式注入**:它的默认路径是用户真实的配置目录,漏注入的用例会把凭据写到
真实用户身上。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QUrl, Slot, qVersion
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
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

from ..api.bilibili import (
    FAV_ITEM_TYPE_VIDEO,
    AccountInfo,
    BilibiliClient,
    FavFolder,
    FavItem,
    SearchResult,
)

#: ``api`` 里那个"一页收藏夹内容"的数据类,与 ``ui.widgets.FavPage``(页面控件)**重名**。
#: 这里显式改名导入,免得两个 ``FavPage`` 在同一个文件里互相遮蔽 —— 那种错误在
#: "看起来能用、只是参数类型不对"的表象下极难查。
from ..api.bilibili import FavPage as FavPageData
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
from ..core.logging_setup import (
    environment_summary,
    export_logs,
    get_logger,
    log_root,
    setup_logging,
)
from ..core.models import (
    Page,
    PlayableEntry,
    Video,
    format_count,
    format_size,
    video_web_url,
)
from ..core.queue import QueueItem
from ..core.search_history import SearchHistory
from ..core.session import Session, SessionStore, parse_cookie_header, session_from_cookies
from ..net.base import FetchHandle
from .cover_loader import CoverLoader
from .icons import app_icon_path
from .theme import apply_theme
from .widgets import (
    AccountDialog,
    CachePage,
    FavPage,
    FavVisibilityDialog,
    FramelessWindow,
    HistoryPage,
    PlaceholderPage,
    PlayerBar,
    PlaylistEntry,
    QueueDrawer,
    SearchSuggest,
    Sidebar,
    TaskDialog,
    TitleBar,
    TrackList,
    TrackRow,
)
from .widgets.sidebar import EMPTY_PLAYLIST_HINT
from .widgets.track_list import split_title_prefix

__all__ = ["MainWindow", "run"]

#: 本模块的日志器。命名空间由 ``core.logging_setup`` 统一决定 —— 不在那棵树下就落不了盘。
_LOGGER = get_logger(__name__)

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
#:
#: 只剩"新建歌单":真正的歌单(收藏夹)已经能用了,而"在 B站 里新建一个收藏夹"属于写回类
#: 账号操作,不在 M5 的授权范围内(``AGENTS.md`` 1.4)。
_NEW_PLAYLIST_HINT = "新建收藏夹属于写回类账号操作,当前版本不做(见 AGENTS.md 1.4)"


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
        session_store: SessionStore | None = None,
        log_dir: Path | None = None,
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
            session_store: 登录凭据的读写;``None`` 表示用平台默认路径
                (测试**必须**注入沙箱路径,否则会动到真实用户的凭据)。
            log_dir: 日志目录(「打开目录」/「导出日志」两个入口用);``None`` 表示用
                平台默认目录。**可注入是为了让测试不往用户真实的 ``~/Library/Logs``
                里写文件** —— 与其它可注入资源同一套路。
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
        #: 登录凭据的读写。**默认值是用户真实的配置目录** —— 测试必须注入沙箱路径
        self.sessions = session_store if session_store is not None else SessionStore()
        #: 日志目录。默认值是平台默认目录;``run()`` 会把 ``setup_logging`` 实际用的那个
        #: 路径传进来(重配过级别/目录时两边才不会说法不一)
        self.log_dir: Path = Path(log_dir) if log_dir is not None else log_root()
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
        #: 搜索历史的读写(与最近播放同库**不同表**)。上限是模块里的常量而不是配置项:
        #: 它只决定搜索框下拉里能给多少提示,没有值得让用户去手改配置的分量。
        self.search_history = SearchHistory(self.library)
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
        self.fav_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )

        # ---------------------------------------------------------- 账号与收藏夹
        #: 当前登录账号;``None`` 表示未登录(或凭据还没校验完)。**只由 nav 的结果写**,
        #: 别处不得凭"收藏夹接口没报错"就断定已登录
        self._account: AccountInfo | None = None
        #: 当前收藏夹列表(接口返回顺序)
        self._folders: list[FavFolder] = []
        #: 当前打开的收藏夹 id;``0`` 表示还没选
        self._fav_media_id = 0
        #: 已加载到第几页(第 1 页记 1);0 表示一条都没加载
        self._fav_page_no = 0
        #: 是否有收藏夹内容请求在飞(挡住重复点"加载更多")
        self._fav_loading = False
        #: 请求序号:切收藏夹会让在飞的旧请求作废,否则旧响应会追加进新收藏夹的列表里
        self._fav_token = 0
        #: 在飞的收藏夹内容请求句柄(切换时取消)
        self._fav_handle: FetchHandle | None = None
        #: 被用户手动隐藏的收藏夹 id(``config.json`` 的 ``fav_hidden_ids``)。
        #: 只影响侧栏列不列出来,不改 B站 上的任何东西
        self._hidden_fav_ids: set[int] = set(self._config.fav_hidden_ids)
        #: 收藏夹**列表**是否有请求在飞(挡住连点「刷新」:每次刷新都是真请求)
        self._folders_loading = False
        #: 列表请求序号。与内容分页的 ``_fav_token`` **分开**:一次刷新与一次翻页互不相干,
        #: 共用序号会让其中一个把另一个误判成过期响应
        self._folders_token = 0
        #: 收藏夹「显示 / 隐藏」对话框。**每次打开重建**:列表会随刷新与换账号而变,复用
        #: 一个旧对话框就要处理"控件对不上数据"的一堆边界;它里面也没有凭据那类必须
        #: 只留一份的东西(与 ``account_dialog`` 的取舍不同)
        self.visibility_dialog: FavVisibilityDialog | None = None

        self._build_ui()
        #: 下载任务对话框。**懒建、只建一个**:非模态窗口关掉只是隐藏,
        #: 再点"下载任务"应该看到同一条列表,而不是又开一个窗
        self.task_dialog: TaskDialog | None = None
        #: 账号对话框。同样懒建、只建一个:它承载输入框里的凭据,重复建会在内存里
        #: 留下多份副本
        self.account_dialog: AccountDialog | None = None
        self._connect()
        self._apply_config()
        # 启动时先把任务角标刷对(上次没下完的任务还在库里等着)
        self.cache_page.set_task_summary(self.downloader.active_count())
        # 恢复上次的登录态(有凭据才发请求;没有就什么都不做)
        self._restore_session()

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
        self.fav_page = FavPage(self.fav_covers)
        self.placeholder_page = PlaceholderPage()
        self.pages.addWidget(self.results_page)
        self.pages.addWidget(self.cache_page)
        self.pages.addWidget(self.history_page)
        self.pages.addWidget(self.fav_page)
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

        # 搜索历史下拉框:普通子控件,**浮在内容区之上**(不是 Qt.Popup 顶层窗口 ——
        # 那会抓走键盘、让输入框失焦,用户就没法边打字边看历史被过滤了。见
        # ``ui/widgets/search_suggest.py`` 的模块 docstring)。父对象取**中间内容控件**
        # ``root`` 而不是主窗口本身:``QMainWindow`` 会把直接挂在它下面的子控件收进自己的
        # 布局管理,浮层的位置就不再由我们说了算;挂在 ``root`` 下才锚得住搜索框
        self.search_suggest = SearchSuggest(root)

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
        # 搜索历史下拉框:数据来自 search_history(过滤规则在 core 里),交互(点搜索框
        # 展开、输入时过滤、点别处收起并清焦点)由控件自己接管
        self.search_suggest.set_source(self.search_history.suggest)
        self.search_suggest.attach(self.title_bar.search_input)
        self.search_suggest.term_activated.connect(self._on_suggest_activated)
        # 列表只报"快到底了",要不要真去翻页由 _load_more 一处决定
        self.result_list.load_more_requested.connect(self._load_more)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximized)
        self.title_bar.close_requested.connect(self.close)

        self.sidebar.nav_selected.connect(self._on_nav_selected)
        self.sidebar.playlist_selected.connect(self._on_playlist_selected)
        self.sidebar.refresh_playlists_requested.connect(self._on_refresh_playlists)
        self.sidebar.manage_playlists_requested.connect(self._open_fav_visibility_dialog)
        self.sidebar.create_playlist_requested.connect(self._on_create_playlist)
        self.sidebar.account_requested.connect(self._open_account_dialog)
        self.sidebar.open_log_dir_requested.connect(self._open_log_dir)
        self.sidebar.export_logs_requested.connect(self._export_logs)

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

        self.fav_page.row_activated.connect(self._on_fav_activated)
        self.fav_page.row_menu_requested.connect(self._on_fav_menu)
        self.fav_page.add_requested.connect(self._on_fav_add)
        self.fav_page.load_more_requested.connect(self._on_fav_load_more)
        self.fav_page.unplayable_requested.connect(self._on_unplayable)

        self.cover_loader.loaded.connect(self._on_cover_loaded)
        self.cover_loader.failed.connect(self._on_cover_failed)
        self.list_covers.loaded.connect(self.result_list.set_cover)
        self.queue_covers.loaded.connect(self.queue_drawer.set_cover)
        self.cache_covers.loaded.connect(self.cache_page.set_cover)
        self.fav_covers.loaded.connect(self.fav_page.set_cover)
        self.history_covers.loaded.connect(self.history_page.set_cover)

    def _apply_window_icon(self) -> None:
        """设置窗口/任务栏图标。

        应用图标是位图而不是 SVG —— 平台要按 DPI 自行挑档渲染,矢量图给不了。
        具体用哪一份由 :func:`app_icon_path` 按平台决定:Windows 用满幅的 ``.ico``
        (含 16~256 共 7 档尺寸),macOS 用带留白的 ``app-macos.png``。
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

        这是**唯一**记搜索历史的地方(点历史词搜、回车搜、点按钮搜都会走到这里):
        记的是提交出去的那个关键字。
        """
        keyword = self.title_bar.search_input.text().strip()
        if not keyword:
            return
        # 同一个词再搜一次会被提到最前,去重由主键 + UPSERT 完成(见 core/search_history.py)。
        # 记在"提交"这一刻而不是每次按键:否则"周”“周杰”“周杰伦”会存成三条
        self.search_history.record(keyword)
        # 下拉框留在这里已经没有意义 —— 下面马上要把结果列表换成这个词的结果
        self.search_suggest.close_suggest()
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

    def _on_suggest_activated(self, term: str) -> None:
        """点搜索历史里的一条词:填回输入框,并立刻提交这次搜索。

        Args:
            term: 被点中的历史词(原文,已去掉首尾空白)。
        """
        # 用 setText 而不是让用户自己按回车:需求就是"点一下就搜"。setText 只发
        # textChanged 不发 textEdited,所以不会又触发一遍下拉框的过滤(见 SearchSuggest.attach)
        self.title_bar.search_input.setText(term)
        self.on_search()

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
        self._set_status(
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
        self._set_status(f"第 {self._loaded_page + 1} 页加载失败:{exc}", level=logging.WARNING)
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
            self._set_status(f"已加载 {loaded} 条,继续下滑加载更多")
        elif self._loaded_page >= _SEARCH_MAX_PAGES:
            self._set_status(
                f"已加载 {loaded} 条(最多加载 {_SEARCH_MAX_PAGES} 页)"
            )
        else:
            self._set_status(f"已加载全部 {loaded} 条")

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
        self._set_status(f"已加入播放队列:{video.title}")

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

    # ------------------------------------------------------------ 账号与收藏夹

    def _open_account_dialog(self) -> None:
        """打开账号对话框。

        懒建且只建一个:对话框里放着用户粘贴的凭据,重复建会在内存里留下多份副本;
        每次打开都按**当前**登录态刷一遍面板,而不是让对话框自己记状态。
        """
        if self.account_dialog is None:
            self.account_dialog = AccountDialog(self.sessions.path, self)
            self.account_dialog.login_requested.connect(self._on_login_requested)
            self.account_dialog.logout_requested.connect(self._on_logout_requested)
        account = self._account
        self.account_dialog.set_account(
            account.uname if account is not None else "",
            account.mid if account is not None else 0,
        )
        self.account_dialog.show()
        self.account_dialog.raise_()

    def _restore_session(self) -> None:
        """启动时恢复上次保存的登录态。

        **没有凭据就什么都不做**(不发请求、不碰网络)。有凭据时先注入网络层,再用
        ``nav`` 问一次服务端"它还有效吗" —— 启动时多一次请求是值得的:不然界面会拿着
        一份早就失效的凭据显示"已登录",用户点开收藏夹却什么都没有。
        """
        session = self.sessions.load()
        if session is None:
            return
        self.client.backend.set_session_cookies(session.cookies)
        # 先用本地记住的昵称把界面点亮(离线启动也能看到"上次登录的是谁"),随后用 nav 校正
        self.sidebar.set_account(session.uname)
        self._verify_session(session)

    def _verify_session(self, session: Session) -> None:
        """用 ``nav`` 校验一份凭据,并按结果落盘或清理。

        Args:
            session: 要校验的凭据(已经注入网络层)。
        """

        def on_success(info: AccountInfo) -> None:
            """服务端回话了:认就保存并展开收藏夹,不认就清干净。"""
            if not info.is_login:
                self._drop_session("登录凭据已失效,请重新登录")
                return
            stored = session_from_cookies(session.cookies, uname=info.uname, mid=info.mid)
            try:
                self.sessions.save(stored)
            except OSError as exc:
                # 落盘失败不算登录失败:这一次会话照样能用,只是下次启动要重新粘贴
                self._fail(f"登录成功,但凭据没能保存到磁盘:{exc}")
            self._apply_account(info)

        def on_error(exc: Exception) -> None:
            """请求失败:这**不是**凭据失效,绝不删凭据。"""
            self._set_status(f"登录态暂时没校验成功(网络问题):{exc}", level=logging.WARNING)

        self.client.fetch_nav(on_success=on_success, on_error=on_error)

    def _on_login_requested(self, raw_cookie: str) -> None:
        """用户粘贴了 Cookie 并点了登录。

        **校验通过才落盘**:先把凭据注入网络层问一次 ``nav``,服务端认了再写文件 ——
        否则一次粘错就会把原来好用的凭据覆盖掉,而且用户下次启动还得再踩一次。

        Args:
            raw_cookie: 粘贴的 Cookie 头原文。
        """
        cookies = parse_cookie_header(raw_cookie)
        self.client.backend.set_session_cookies(cookies)

        def on_success(info: AccountInfo) -> None:
            """服务端回话了。"""
            if not info.is_login:
                # 粘进来的凭据不管用:把它从 jar 里撤掉,别让它继续跟着每个请求发出去
                self.client.backend.clear_session_cookies()
                self.client.backend.warm_up(force=True)
                if self.account_dialog is not None:
                    self.account_dialog.set_error("这个凭据登录不了,请重新复制一份 Cookie")
                return
            self._apply_account(info)
            try:
                self.sessions.save(
                    session_from_cookies(cookies, uname=info.uname, mid=info.mid)
                )
            except OSError as exc:
                self._fail(f"登录成功,但凭据没能保存到磁盘:{exc}")
                return
            if self.account_dialog is not None:
                self.account_dialog.set_account(info.uname, info.mid)
            self._set_status(f"已登录:{info.uname}")

        def on_error(exc: Exception) -> None:
            """请求本身失败了(网络 / 风控):如实说,不写文件。"""
            if self.account_dialog is not None:
                self.account_dialog.set_error(f"校验登录态失败:{exc}")

        self.client.fetch_nav(on_success=on_success, on_error=on_error)

    def _on_logout_requested(self) -> None:
        """用户点了登出:删凭据文件 + 把网络层里的凭据也清掉。

        两步缺一不可:只删文件的话,SESSDATA 还在内存的 jar 里,后续请求照样是登录态
        用户以为登出了,其实没有。清完 jar 还要 ``warm_up(force=True)`` —— 那一步会把
        匿名 Cookie(``buvid3`` / ``b_nut``)一起清掉,而 ``warm_up()`` 默认不会重复预热,
        不强制补一次的话后续请求更容易被风控(README「坑 3」)。
        """
        removed = self.sessions.clear()
        self.client.backend.clear_session_cookies()
        self.client.backend.warm_up(force=True)
        self._apply_account(None)
        if self.account_dialog is not None:
            self.account_dialog.set_account("", 0)
        self._set_status("已登出" + ("(本机凭据文件已删除)" if removed else ""))

    def _drop_session(self, message: str) -> None:
        """凭据被服务端拒了:清内存 + 清文件 + 把界面退回未登录。

        Args:
            message: 给用户看的中文原因。
        """
        self.client.backend.clear_session_cookies()
        self.client.backend.warm_up(force=True)
        self.sessions.clear()
        self._apply_account(None)
        if self.account_dialog is not None:
            self.account_dialog.set_error(message)
        else:
            self._set_status(message)

    # ------------------------------------------------------------ 底部状态行

    def _set_status(self, text: str, *, level: int = logging.INFO) -> None:
        """更新底部状态文字,并把它记进日志。

        底部那行文字就是本应用的**用户操作时间线**:谁在什么时候点了什么、结果如何,
        只有它在记。日志从这里取最省力 —— 不必在几十个回调里各写一遍日志调用,而且
        永远不会出现"界面上显示了、日志里没有"的不一致。

        空串表示"清掉旧提示"(例如切页、登出),那不是一个事件,**不落盘**。

        Args:
            text: 要显示的中文文案。沿用原有文案,不因为加日志而改写。
            level: 对应的日志级别,失败类提示传 ``logging.WARNING``。
        """
        self.status_label.setText(text)
        if text:
            _LOGGER.log(level, "界面状态:%s", text)

    def _on_unplayable(self, text: str) -> None:
        """列表里某一条因为取不到地址而播不了:提示用户并记一条 WARNING。

        Args:
            text: 栏页(或列表)给出的中文说明。
        """
        self._set_status(text, level=logging.WARNING)

    def _apply_account(self, info: AccountInfo | None) -> None:
        """把登录态推给界面;已登录时顺带拉一遍收藏夹列表。

        Args:
            info: ``nav`` 的结果;``None``(或 ``is_login`` 为假)表示未登录。
        """
        self._account = info if (info is not None and info.is_login) else None
        self.sidebar.set_playlist_actions_enabled(self._account is not None)
        if self._account is None:
            self.sidebar.set_account("")
            # 登出 / 凭据失效时把在飞的列表请求作废:否则它回来时会按"还登录着"的口径
            # 把侧栏重新填满(用户都已经登出了,那一排夹子却还在)
            self._folders_token += 1
            self._folders_loading = False
            self.sidebar.set_refresh_busy(False)
            self._folders = []
            self.sidebar.set_playlists((), empty_hint=EMPTY_PLAYLIST_HINT)
            self._fav_media_id = 0
            self._fav_page_no = 0
            self.fav_page.set_folder("")
            self.fav_page.set_items([])
            self.fav_page.set_has_more(False)
            return
        self.sidebar.set_account(self._account.uname)
        if self.account_dialog is not None:
            self.account_dialog.set_account(self._account.uname, self._account.mid)
        self._refresh_folders()

    def _refresh_folders(self) -> None:
        """(重新)取一遍"我创建的收藏夹"并填进侧栏。

        登录、启动恢复与「刷新」按钮都走这里。``_folders_loading`` 挡连点:这个接口也是
        一次真实请求,连点两下就是两次(风控口径见 README「坑 3」)。

        响应回来前先自增 ``_folders_token``:登出或又刷新一次之后,这份列表就过期了,
        迟到的响应必须丢掉(与切收藏夹时的 ``_fav_token`` 同一类防护)。
        """
        if self._account is None or self._folders_loading:
            return
        self._folders_loading = True
        self._folders_token += 1
        token = self._folders_token
        self.sidebar.set_refresh_busy(True)
        self.client.fetch_fav_folders(
            self._account.mid,
            on_success=lambda folders: self._on_folders(folders, token),
            on_error=lambda exc: self._on_folders_failed(exc, token),
        )

    def _on_folders(self, folders: list[FavFolder], token: int) -> None:
        """收藏夹列表回来了:过掉"被隐藏"的那一层,再换成侧栏条目。

        Args:
            folders: 接口返回的收藏夹(可能是空列表)。
            token: 请求序号;与当前序号对不上说明这次列表已经过期,直接丢弃。
        """
        if token != self._folders_token:
            return
        self._folders_loading = False
        self.sidebar.set_refresh_busy(False)
        self._folders = list(folders)
        self._render_playlists()
        if not self._folders and self._account is not None:
            self._set_status(f"账号「{self._account.uname}」下没有收藏夹")
            return
        if self.status_label.text().startswith(("登录态暂时", "账号「")):
            self._set_status("")

    def _on_folders_failed(self, exc: Exception, token: int) -> None:
        """收藏夹列表没取到:如实说,并保住手里已有的那份列表。

        **不把侧栏清空**:这个失败很可能来自一次手动「刷新」(网络抖一下、撞上 412),
        把用户正看着的列表擦掉换一句报错,比报错本身更糟。真正的"没有列表"只出现在
        登录后的第一次请求上,那时侧栏本来就是空的。

        Args:
            exc: 失败原因。
            token: 请求序号;过期响应直接丢弃。
        """
        if token != self._folders_token:
            return
        self._folders_loading = False
        self.sidebar.set_refresh_busy(False)
        self._set_status(f"收藏夹列表没取到:{exc}", level=logging.WARNING)

    def _playlist_empty_hint(self) -> str:
        """侧栏一个歌单都不显示时,那句说明该写什么。

        三种空态必须分得清,否则用户会以为"登录丢了"或者"我的收藏夹没了":

        * 未登录 —— 说清楚登录之后才会有;
        * 登录了但账号下一个夹子都没有 —— 如实说,而不是继续提示去登录;
        * 有夹子但全被隐藏 —— 告诉他去哪恢复(隐藏是本机设置,不是数据没了)。

        Returns:
            给侧栏 ``set_playlists(empty_hint=...)`` 用的中文文案。
        """
        if self._account is None:
            return EMPTY_PLAYLIST_HINT
        if not self._folders:
            return "这个账号下还没有收藏夹"
        return "收藏夹都被隐藏了,点上面的「显示/隐藏」可以放出来"

    def _render_playlists(self) -> None:
        """按"当前收藏夹列表 + 隐藏集合"重建侧栏「我的歌单」。

        隐藏只在这里生效(侧栏列不列出来):正在打开的收藏夹页不被抽走 —— 用户隐藏的可能
        正是他在听的那个夹子,把内容页一起关掉比留着更打扰。
        """
        visible = [
            folder
            for folder in self._folders
            if folder.media_id not in self._hidden_fav_ids
        ]
        self.sidebar.set_playlists(
            [self._entry_of(folder) for folder in visible],
            empty_hint=self._playlist_empty_hint(),
        )

    def _on_refresh_playlists(self) -> None:
        """点了「刷新」:重新取一遍收藏夹列表。

        没有任何缓存可复用 —— 每次都是真请求,这正是这个按钮存在的意义(在 B站 网页上
        新建 / 删除了收藏夹,本机要能看见变化)。正在刷新时 :meth:`_refresh_folders`
        会直接返回,按钮也已经被禁掉。
        """
        if self._account is None:
            self._set_status("还没登录,先点侧栏的账号按钮登录")
            return
        self._set_status("正在刷新收藏夹列表…")
        self._refresh_folders()

    def _open_fav_visibility_dialog(self) -> None:
        """打开「显示 / 隐藏收藏夹」弹窗,确定后落盘并重建侧栏。

        弹窗只负责收集勾选,写配置与重画侧栏由 :meth:`_set_hidden_folders` 做 —— 与
        ``AccountDialog`` 同一条纪律(``AGENTS.md`` 第 4 节)。
        """
        if self._account is None:
            self._set_status("还没登录,先点侧栏的账号按钮登录")
            return
        if not self._folders:
            self._set_status("还没取到收藏夹列表,先点「刷新」取一遍")
            return
        dialog = FavVisibilityDialog(
            self._playlist_entries(), self._hidden_fav_ids, self
        )
        # 只建一个引用并挂到 self 上:模态对话框在 exec() 期间必须活着,而这里还要让
        # 测试能拿到它(与 account_dialog / task_dialog 同一口径)
        self.visibility_dialog = dialog
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._set_hidden_folders(dialog.hidden_media_ids())
        finally:
            self.visibility_dialog = None

    def _playlist_entries(self) -> list[PlaylistEntry]:
        """把接口给的收藏夹翻成侧栏 / 弹窗都认的展示结构。

        两处共用同一份转换(而不是各写一遍):弹窗里的勾选项必须与侧栏里的按钮一一对应,
        条数与私密记号少了一个都会让用户在两边看到不一样的东西。
        """
        return [self._entry_of(folder) for folder in self._folders]

    @staticmethod
    def _entry_of(folder: FavFolder) -> PlaylistEntry:
        """把一条收藏夹元信息摊成侧栏条目。

        Args:
            folder: 接口返回的收藏夹。

        Returns:
            侧栏与「显示/隐藏」弹窗共用的展示数据。
        """
        return PlaylistEntry(
            media_id=folder.media_id,
            title=folder.title,
            media_count=folder.media_count,
            is_private=folder.is_private,
        )

    def _set_hidden_folders(self, media_ids: Sequence[int]) -> None:
        """保存用户勾掉的收藏夹,并重建侧栏。

        只落配置 + 重画侧栏:正在打开的收藏夹页**不动** —— 用户隐藏的可能正是他在听的
        那个夹子,把内容页抽走比留着更打扰。

        Args:
            media_ids: 用户选择隐藏的 ``media_id``(弹窗返回的那一份)。
        """
        hidden: set[int] = set()
        for raw in media_ids:
            media_id = int(raw)
            if media_id > 0:
                hidden.add(media_id)
        self._hidden_fav_ids = hidden
        # 写的是排序后的列表:``ConfigStore.save`` 还会再收敛一次(非法值不落盘),这里
        # 排好序只是让"配置文件里那一行长什么样"稳定可读
        self._config.fav_hidden_ids = sorted(hidden)
        self._save_config()
        self._render_playlists()
        count = len(hidden)
        if count:
            self._set_status(f"已隐藏 {count} 个收藏夹(只影响侧栏显示)")
        else:
            self._set_status("已显示全部收藏夹")

    def _load_fav_page(self, page_no: int, *, append: bool) -> None:
        """取某个收藏夹的一页内容。

        切夹子 / 翻页都走这里,并用 ``_fav_token`` 让"已经过时的响应"作废 —— 否则
        128 条的收藏夹还没回来、用户已经点了另一个夹子,旧响应会追加进新夹子的列表。

        Args:
            page_no: 页码,从 1 开始。
            append: 为真时追加到列表末尾(翻页),否则整体替换(切夹子)。
        """
        if not self._fav_media_id:
            return
        self._fav_token += 1
        token = self._fav_token
        if self._fav_handle is not None:
            self._fav_handle.cancel()
        self._fav_loading = True
        self.fav_page.set_loading(True)

        def on_success(page: FavPageData) -> None:
            """响应到达:先确认它还是"当前这一次"的响应。"""
            if token != self._fav_token:
                return
            self._fav_handle = None
            self._fav_loading = False
            if append:
                self.fav_page.append_items(page.items)
            else:
                self.fav_page.set_items(page.items)
            self._fav_page_no = page_no
            self.fav_page.set_has_more(page.has_more)
            self.fav_page.set_loading(False)
            if not page.has_more:
                self._set_status("")

        def on_error(exc: Exception) -> None:
            """失败:留在当前页,给一个能再点的出口(页脚按钮还在)。"""
            if token != self._fav_token:
                return
            self._fav_handle = None
            self._fav_loading = False
            self.fav_page.set_failed(f"加载收藏夹失败:{exc}")

        self._fav_handle = self.client.fetch_fav_page(
            self._fav_media_id,
            page=page_no,
            on_success=on_success,
            on_error=on_error,
        )

    def _on_fav_load_more(self) -> None:
        """点了"加载更多":取下一页并追加。"""
        if not self._fav_media_id or self._fav_loading:
            return
        self._load_fav_page(self._fav_page_no + 1, append=True)

    def _with_video(self, bvid: str, on_ready: Callable[[Video], None]) -> None:
        """先补一次详情,拿到 ``pages`` 之后再执行动作。

        收藏夹条目**没有 ``cid``**(实测只有分P数量),所以"播放 / 加入队列"这些需要
        ``Video`` 的动作都得先补一次 ``view``。命中详情缓存时 ``fetch_video`` 是同步回调的,
        所以调用方不要假定回调发生在方法返回之后。

        Args:
            bvid: 目标视频 BV 号。
            on_ready: 详情到手后的回调。
        """

        def on_success(video: Video) -> None:
            """详情到手。"""
            on_ready(video)

        def on_error(exc: Exception) -> None:
            """拿不到详情:说清是哪一条失败,不动队列。"""
            self._set_status(f"取视频详情失败({bvid}):{exc}", level=logging.WARNING)

        self.client.fetch_video(bvid, on_success=on_success, on_error=on_error)

    def _on_fav_activated(self, row: int) -> None:
        """双击收藏夹里的一条:补详情后**只播这一条**。

        这里与搜索结果 / 缓存页的"整列变成队列"**故意不同**:那些页面的条目手上就有
        ``Video``(搜索结果有详情缓存的可能、缓存页能从索引重建),而收藏夹条目要变成队列
        就得为每一条各发一次 ``view`` —— 一页 20 条就是 20 次请求,还都在限速下排队。
        多P合集内部仍然会自动往下播(那是播放层的事),所以听歌体验不受影响。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        item = self.fav_page.entry_at(row)
        if item is None or not item.is_playable:
            return
        self._with_video(
            item.bvid,
            lambda video: self.playback.play_queue([QueueItem(video=video)], start=0),
        )

    def _on_fav_add(self, row: int) -> None:
        """点行内"+":补详情后加到队列末尾。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        item = self.fav_page.entry_at(row)
        if item is None or not item.is_playable:
            return

        def ready(video: Video) -> None:
            """详情到手:入队并提示。"""
            self.playback.enqueue(QueueItem(video=video))
            self._set_status(f"已加入播放队列:{video.title}")

        self._with_video(item.bvid, ready)

    def _on_fav_menu(self, row: int, position) -> None:  # noqa: ANN001 - QPoint
        """收藏夹右键菜单:播放 / 下一首播放 / 加入队列 / 缓存 / 在B站打开。

        除"在B站打开"外都要先补详情:收藏夹条目没有 ``cid`` 和分P列表,而缓存任务
        必须按每个分P的 ``cid`` 落盘。"在B站打开"是唯一拿到 bvid 就能立刻做的事。

        Args:
            row: 行号(以过滤后的列表为准)。
            position: 全局坐标,交给 ``QMenu.exec``。
        """
        item = self.fav_page.entry_at(row)
        if item is None or not item.is_playable:
            return
        menu = QMenu(self)
        play_action = menu.addAction("播放")
        next_action = menu.addAction("下一首播放")
        queue_action = menu.addAction("加入队列")
        menu.addSeparator()
        cache_action = menu.addAction("缓存到本地")
        menu.addSeparator()
        open_action = menu.addAction("在B站打开")

        chosen = menu.exec(position)
        if chosen is play_action:
            self._on_fav_activated(row)
        elif chosen is next_action:
            self._with_video(
                item.bvid,
                lambda video: self.playback.enqueue_next(QueueItem(video=video)),
            )
        elif chosen is queue_action:
            self._on_fav_add(row)
        elif chosen is cache_action:
            self._on_fav_cache(row)
        elif chosen is open_action:
            QDesktopServices.openUrl(QUrl(video_web_url(item.bvid)))

    def _on_fav_cache(self, row: int) -> None:
        """缓存收藏夹中的一个视频及其全部分P。

        收藏夹接口只返回视频级元数据,没有每首歌实际必须使用的 ``cid``。因此先用
        :meth:`_with_video` 补详情,再复用搜索结果的批量缓存入口;这样确认文案、已缓存
        跳过、串行下载和任务列表都保持同一套规则。

        Args:
            row: 行号(以过滤后的列表为准)。
        """
        item = self.fav_page.entry_at(row)
        if item is None or not item.is_playable:
            return
        self._with_video(item.bvid, self._cache_all_pages)

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
        self._set_status(f"正在获取「{video.title}」的分P列表…")
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
        self._set_status(
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

    @Slot(str)
    def _on_playlist_selected(self, media_id_text: str) -> None:
        """点了某个收藏夹:打开收藏夹页并加载第一页内容。

        切夹子时**先让在飞的旧请求作废**(token +1)再发新的 —— 否则 128 条的收藏夹
        还没回来、用户已经点了另一个夹子,旧响应会把内容追加到新夹子的列表里
        (与 ``audio/resolver.py`` 的 ``_alive`` 是同一类防护)。侧栏以文本发 id,因为 B站
        的 ``media_id`` 可能超过 Qt ``int`` 的有符号 32 位上限;这里才回到 Python ``int``。

        Args:
            media_id_text: 收藏夹 id 的十进制文本(侧栏信号带的就是它;名字可以重复,不能当键)。
        """
        if not media_id_text.isdecimal():
            return
        media_id = int(media_id_text)
        folder = next((f for f in self._folders if f.media_id == media_id), None)
        if folder is None:
            # 侧栏是按上一次的收藏夹列表画的,列表刚被刷新过时可能对不上
            return
        self._fav_media_id = media_id
        self._fav_page_no = 0
        self.fav_page.set_folder(
            folder.title, is_private=folder.is_private, media_count=folder.media_count
        )
        self.fav_page.set_items([])
        self.fav_page.set_has_more(False)
        self._show_page(self.fav_page, None)
        self._load_fav_page(1, append=False)

    def _on_create_playlist(self) -> None:
        """点了"新建歌单"(在B站新建收藏夹属于写回操作,当前版本不做)。"""
        self.placeholder_page.set_content("新建歌单", _NEW_PLAYLIST_HINT)
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

    # ------------------------------------------------------------ 日志入口

    def _open_log_dir(self) -> None:
        """在系统文件管理器里打开日志目录。

        这个入口是给**开发者自己**排障用的。它敢直接给用户用,是因为日志目录与凭据目录
        是物理隔离的(见 ``core/logging_setup`` 的模块 docstring):打开它看不到
        ``session.json``。
        """
        directory = self.log_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "打开日志目录", f"日志目录不可用:{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            QMessageBox.warning(
                self, "打开日志目录", f"没能打开文件管理器,日志目录在:\n{directory}"
            )

    def _export_logs(self) -> None:
        """把日志打包成一个 zip,交给用户发给开发者。

        导出前**先说明包里有什么、没有什么**:这是知情同意,不是多此一举 ——
        日志里会有用户搜过的关键词与视频 ID(参数取值全抹了,但 ``api`` 层会记显式入参),
        而包发给别人之前,用户有权知道自己在发什么。
        """
        answer = QMessageBox.question(
            self,
            "导出日志",
            "即将导出一份日志压缩包。\n\n"
            "包含:最近的运行日志、应用版本与系统信息。\n"
            "不包含:账号凭据、收藏夹内容、本地音频。\n\n"
            "日志里会带上你搜索过的关键词与视频 ID,请只发给可信的人。继续?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        default_name = f"BiliMusic-logs-{datetime.now().strftime('%Y%m%d')}.zip"
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出日志", str(Path.home() / default_name), "压缩包 (*.zip)"
        )
        if not chosen:
            return
        summary = environment_summary(
            level=self._config.log_level,
            log_dir=self.log_dir,
            # Qt 版本只有界面层拿得到:``core`` 不许 import Qt(AGENTS.md 4.1)
            extra={"Qt": qVersion()},
        )
        try:
            path = export_logs(Path(chosen), log_dir=self.log_dir, summary=summary)
        except OSError as exc:
            # 写盘类失败都是 OSError(无权限、磁盘满、文件名非法),统一变成一句人话;
            # 导出失败绝不能把界面炸掉
            self._set_status(f"导出日志失败:{exc}", level=logging.WARNING)
            QMessageBox.warning(self, "导出日志", f"导出失败:{exc}")
            return
        self._set_status(f"日志已导出:{path}")

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
        self._set_status(f"正在解析「{item.title}」…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # 不定长进度,表示"忙碌"
        self.result_list.set_highlight(self._result_row_for(item.video.bvid))
        # 收藏夹页按 bvid 标记"正在播的是哪一条"(收藏夹条目是视频级的)
        self.fav_page.set_playing(item.video.bvid)
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
        self._set_status(f"已缓存 {resolved.path.name}")
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
        """缓存进度:总长已知时显示百分比,未知时只显示已收字节。

        这里的文字**故意不走** :meth:`_set_status`:一次下载会回调上百次,逐条落盘会把
        真正有用的日志冲掉,而且"缓存到哪了"在 ``audio.downloader`` 的开始/结束日志里
        已经有更好的记法(带字节数与耗时)。
        """
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
            self._set_status("已经是最后一首")

    def _on_stopped(self) -> None:
        """编排层说没有下一项了。"""
        self.progress.setVisible(False)
        self._set_status("播放结束")

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
            self._set_status(f"配置保存失败:{exc}", level=logging.WARNING)

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
        self._set_status(message, level=logging.WARNING)
        QMessageBox.warning(self, "出错了", message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        """退出前落盘进度,并停掉在飞的解析、下载与播放。"""
        # 搜索历史下拉框装的是**应用级**事件过滤器,它的命比窗口长:窗口关掉之前必须
        # 摘掉,否则它会被继续调用到一个正在拆的窗口上(见 SearchSuggest.detach)
        self.search_suggest.detach()
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
    # 配置只读一次:日志级别必须在窗口建起来**之前**就知道(启动阶段的日志同样要落盘),
    # 所以先把 store 建好传进窗口,避免窗口再 load 一遍(两次结果可能不一样)
    config_store = ConfigStore()
    config = config_store.load()
    log_path = setup_logging(level=config.log_level)
    window = MainWindow(
        config_store=config_store,
        # 把 ``setup_logging`` 实际用的目录传进窗口:两个入口(打开/导出)必须操作
        # 同一份日志,不能各自去猜一次平台默认路径
        log_dir=log_path.parent if log_path is not None else None,
    )
    if log_path is None:
        # 建档失败(目录不可写、磁盘满)不能让应用起不来,但用户必须知道:
        # 否则他会以为“日志已经记下了”,把一份空报告发过来
        window._set_status(  # noqa: SLF001 - 同一模块内的私有接线
            "日志未能启用(目录不可写),排障信息不会落盘", level=logging.WARNING
        )
    _LOGGER.info("界面已就绪,日志文件 %s", log_path if log_path is not None else "(未启用)")
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
