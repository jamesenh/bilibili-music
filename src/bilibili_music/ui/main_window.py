"""主窗口:组装各块 widget,并把界面事件接到播放编排上。

**这里只做接线**。搜索、解析、下载、切歌的判断分别在 ``api`` / ``audio`` 里,界面负责把
用户操作转成方法调用、把编排层发来的信号渲染成界面状态(``AGENTS.md`` 第 4 节)。

版式(对着设计稿):

::

    ┌──────────────────────────────────────────────────────┐
    │ [图标] [搜索框 ..............] [搜索]      [-][□][×] │  ← TitleBar(自绘)
    ├────────┬──────────────────────────┬──────────────────┤
    │ Sidebar│ 内容页(QStackedWidget)   │ QueueDrawer      │
    │ 发现   │  搜索结果页 / 占位页      │ 播放队列         │
    │ ...    │                          │                  │
    ├────────┴──────────────────────────┴──────────────────┤
    │ [封面] 曲名/UP主   [传输控件]  [进度]  [分P/音质/音量] │  ← PlayerBar
    └──────────────────────────────────────────────────────┘

窗口是**无边框**的(``FramelessWindow``),所以最小化/最大化/关闭落在自绘标题栏里。

底部播放条上的**分P选择器**是"多P合集内部换歌"的入口:合集在队列里只占一行,但每个
分P都是一首独立的歌,所以换分P既不改队列,也不在搜索结果列表里 —— 它显示当前正在播
那个视频的分P,选中的目标交给 ``audio.playback`` 走既有的播放切换流程。

侧栏入口里"发现""本地缓存"以及"我的歌单"对应的功能分别属于路线图 M3 / M2 / M5,
当前都还没有实现 —— 点开是一张把话说清楚的占位页,而不是点了没反应(见
:class:`~bilibili_music.ui.widgets.placeholder.PlaceholderPage`)。"播放队列"不是一页,
它是右侧队列面板的开关。

依赖以参数注入的只有"可替换的外部资源"(客户端 / 音频缓存 / 封面缓存 / 配置 / 编排器):
默认全部走真实实现,测试可以塞替身进来,于是界面接线能被自动化验证,而不是只能靠肉眼点。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..api.bilibili import BilibiliClient, SearchResult
from ..audio.playback import PlaybackController
from ..audio.player import PlayerController
from ..audio.resolver import AudioResolver, ResolvedAudio
from ..core.cache import AudioCache
from ..core.config import ConfigStore
from ..core.cover_cache import CoverCache
from ..core.models import Video, format_count
from ..core.queue import QueueItem
from .cover_loader import CoverLoader
from .icons import app_icon_path
from .theme import apply_theme
from .widgets import (
    FramelessWindow,
    PlaceholderPage,
    PlayerBar,
    QueueDrawer,
    Sidebar,
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

#: 侧栏里那些"还没有对应功能"的入口 -> ``(占位页标题, 说明)``。
#:
#: 说明里写上路线图编号,是为了让"为什么这页是空的"有据可查,而不是一句"敬请期待"。
_PLACEHOLDER_PAGES: dict[str, tuple[str, str]] = {
    "discover": ("发现", "排行榜与内容发现还没实现(路线图 M3)"),
    "cache": ("本地缓存", "本地曲库还没实现,要先有缓存索引(路线图 M2.1)"),
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
    """

    def __init__(
        self,
        client: BilibiliClient | None = None,
        *,
        cache: AudioCache | None = None,
        cover_cache: CoverCache | None = None,
        config_store: ConfigStore | None = None,
        playback: PlaybackController | None = None,
    ) -> None:
        """建依赖、建界面、接线,并套用配置。

        Args:
            client: 接口客户端。
            cache: 音频缓存。
            cover_cache: 封面磁盘缓存。
            config_store: 配置读写。
            playback: 播放编排器。
        """
        super().__init__()
        self.setWindowTitle("BiliMusic")
        self.resize(1180, 760)
        self._apply_window_icon()

        self.client = client if client is not None else BilibiliClient()
        self.cache = cache if cache is not None else AudioCache()
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

        self._videos: list[Video] = []
        #: 最近一次位置信号里的毫秒数;落盘只在切歌与退出时做(见 _remember_position)
        self._position_ms = 0
        self._config = self.config_store.load()
        #: 播放条封面用的加载器。它只关心"当前这一首",所以还要配合 is_current 过滤
        self.cover_loader = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )
        #: 搜索列表与队列面板各自一个加载器:两边都会整体换内容,共用一个的话
        #: "列表换了一批"会把另一边还在排队的封面一起清掉。
        #: 三个加载器**共用同一个磁盘缓存**:同一张图在播放条与列表里是同一个 URL,
        #: 谁的队列先走到就落盘,另一个直接命中,不会重复下载。
        self.list_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )
        self.queue_covers = CoverLoader(
            self.client.fetch_cover, cache=self.cover_cache
        )

        self._build_ui()
        self._connect()
        self._apply_config()

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
        self.placeholder_page = PlaceholderPage()
        self.pages.addWidget(self.results_page)
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
        """建列表下方的"状态文字 + 缓存进度条"这一行。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusLabel")
        row.addWidget(self.status_label, 1)

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
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximized)
        self.title_bar.close_requested.connect(self.close)

        self.sidebar.nav_selected.connect(self._on_nav_selected)
        self.sidebar.playlist_selected.connect(self._on_playlist_selected)
        self.sidebar.create_playlist_requested.connect(self._on_create_playlist)

        self.cover_loader.loaded.connect(self._on_cover_loaded)
        self.cover_loader.failed.connect(self._on_cover_failed)
        self.list_covers.loaded.connect(self.result_list.set_cover)
        self.queue_covers.loaded.connect(self.queue_drawer.set_cover)

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
        """发起搜索(回车或点按钮都会走到这里)。

        请求是异步的:先把按钮禁掉再发,否则用户连点会叠出多个搜索请求,
        在这个接口上等于自找风控。
        """
        keyword = self.title_bar.search_input.text().strip()
        if not keyword:
            return
        self.title_bar.search_button.setEnabled(False)
        self.query_label.setText(f"「{keyword}」")
        self.total_label.setText("")
        self.status_label.setText(f"正在搜索「{keyword}」…")
        self.client.search_video(
            keyword,
            on_success=self._on_search_done,
            on_error=self._on_search_failed,
        )

    def _on_search_done(self, result: SearchResult) -> None:
        """搜索成功:填表并把分P列标成未知(要等详情接口才知道)。"""
        self.title_bar.search_button.setEnabled(True)
        self._videos = list(result.videos)
        self.result_list.set_tracks(
            [self._track_row_of(video) for video in self._videos]
        )
        self.result_list.set_highlight(-1)
        self.total_label.setText(f"共找到 {result.total} 个视频")
        self.status_label.setText(f"本页 {len(result.videos)} 条")

    def _on_search_failed(self, exc: Exception) -> None:
        """搜索失败:恢复按钮并如实报错。"""
        self.title_bar.search_button.setEnabled(True)
        self._fail(f"搜索失败:{exc}")

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
        """搜索结果右键菜单:播放 / 下一首播放 / 加入队列 / 在B站打开。"""
        video = self._video_at(row)
        if video is None:
            return
        menu = QMenu(self)
        play_action = menu.addAction("播放")
        next_action = menu.addAction("下一首播放")
        queue_action = menu.addAction("加入队列")
        menu.addSeparator()
        open_action = menu.addAction("在B站打开")

        chosen = menu.exec(position)
        if chosen is play_action:
            self._on_result_activated(row)
        elif chosen is next_action:
            self.playback.enqueue_next(QueueItem(video=video))
        elif chosen is queue_action:
            self.playback.enqueue(QueueItem(video=video))
        elif chosen is open_action:
            QDesktopServices.openUrl(QUrl(video.web_url))

    # ------------------------------------------------------------ 导航

    def _on_nav_selected(self, key: str) -> None:
        """侧栏入口被点:队列是面板开关,其余是页面切换。

        Args:
            key: :data:`~bilibili_music.ui.widgets.sidebar.NAV_ITEMS` 里的键。
        """
        if key == "queue":
            # 按钮的 check 状态就是期望的可见性(侧栏只负责把它翻过来)
            self._set_queue_visible(self.sidebar.nav_buttons["queue"].isChecked())
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
        """统一设置队列面板的可见性,并同步两处开关的外观。

        播放条与侧栏各有一个队列开关,它们必须显示同一个状态 —— 所以真正的入口只有
        这一个方法,而不是让两个控件各自维护"我以为队列是开着的"。

        Args:
            visible: 是否显示队列面板。
        """
        self.queue_drawer.setVisible(bool(visible))
        self.player_bar.set_queue_visible(bool(visible))
        self.sidebar.set_queue_visible(bool(visible))

    def _toggle_maximized(self) -> None:
        """在最大化与还原之间切换(标题栏按钮与双击标题栏都走这里)。"""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

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
        """音源就绪:更新标题、音质、时长,并记下"上次播的是哪一首"。"""
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
        # 封面是异步的,而且允许失败:拿不到就用占位图,不打断播放
        self.cover_loader.load(resolved.video.cover_https)
        # 位置的落盘交给切歌与退出,这里只更新内存里的"上次播的是哪一首"
        self._config.last_bvid = resolved.video.bvid
        self._config.last_cid = resolved.page.cid

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
        """退出前落盘进度,并停掉在飞的解析与播放。"""
        self._config.last_position_ms = self._position_ms
        self._save_config()
        self.playback.stop()
        self.client.close()
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
