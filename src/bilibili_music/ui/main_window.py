"""主窗口:组装各块 widget,并把界面事件接到播放编排上。

**这里只做接线**。搜索、解析、下载、切歌的判断分别在 ``api`` / ``audio`` 里,界面负责
把用户操作转成方法调用、把编排层发来的信号渲染成界面状态(``AGENTS.md`` 第 4 节)。

这个文件此前是 500 行的单文件界面:布局、业务判断、网络回调全混在一起,多加一个面板
就要在几百行里找位置。拆出 ``ui/widgets/`` 之后,主窗口只剩组装与转发 —— 判断它是否
合格的依据是**职责**(里面还有没有业务逻辑),不是行数。

依赖以参数注入的只有"可替换的外部资源"(客户端 / 缓存 / 配置 / 编排器):默认全部走真实
实现,测试可以塞替身进来,于是界面接线能被自动化验证,而不是只能靠肉眼点。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..api.bilibili import BilibiliClient, SearchResult
from ..audio.playback import PlaybackController
from ..audio.player import PlayerController
from ..audio.resolver import AudioResolver, ResolvedAudio
from ..core.cache import AudioCache
from ..core.config import ConfigStore
from ..core.models import Video, format_count
from ..core.queue import QueueItem
from .cover_loader import CoverLoader
from .icons import Palette, app_icon_path, get_icon, palette
from .theme import apply_theme
from .widgets import PlayerBar, QueueDrawer, TrackList

__all__ = ["MainWindow", "run"]

#: 搜索结果表格的列。
_SEARCH_COLUMNS = ("标题", "UP主", "时长", "分P", "播放量")

#: "分P"列在详情补全前显示的占位符。
_UNKNOWN_PAGES = "?"


class MainWindow(QMainWindow):
    """主窗口。

    Args:
        client: 接口客户端;``None`` 时使用默认的 Qt 后端实现。
        cache: 音频缓存;``None`` 时使用平台默认缓存目录。
        config_store: 配置读写;``None`` 时使用平台默认配置路径。
        playback: 播放编排器;``None`` 时用 ``client`` 与 ``cache`` 现搭一个。
            传入替身(duck typing)即可在测试里跑通整条界面接线。
    """

    def __init__(
        self,
        client: BilibiliClient | None = None,
        *,
        cache: AudioCache | None = None,
        config_store: ConfigStore | None = None,
        playback: PlaybackController | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("BiliMusic")
        self.resize(1100, 680)
        self._apply_window_icon()

        self.client = client if client is not None else BilibiliClient()
        self.cache = cache if cache is not None else AudioCache()
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
        #: 当前主题的图标调色板;由 _apply_theme 填,各控件按它重绘
        self._colors: Palette = palette("light")
        self.cover_loader = CoverLoader(self.client.fetch_cover)

        self._build_ui()
        self._connect()
        self._apply_config()

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        """组装搜索行、结果列表、队列抽屉与播放条。"""
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(8)

        layout.addLayout(self._build_search_row())

        content = QHBoxLayout()
        content.setSpacing(8)
        content.addLayout(self._build_result_column(), 1)

        self.queue_drawer = QueueDrawer()
        content.addWidget(self.queue_drawer)
        layout.addLayout(content, 1)

        self.player_bar = PlayerBar()
        layout.addWidget(self.player_bar)

        self.setCentralWidget(root)

    def _build_search_row(self) -> QHBoxLayout:
        """建"搜索框 + 搜索按钮"这一行。"""
        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索 B站音乐视频,例如:周杰伦 MV")
        self.search_input.returnPressed.connect(self.on_search)

        self.search_button = QPushButton("搜索")
        self.search_button.setIcon(
            get_icon("search", palette("light").muted, 18)
        )
        self.search_button.clicked.connect(self.on_search)

        # 主题切换用文字而不是图标:仓库里没有合适的"月亮/太阳"图标,而按钮上写清
        # "点了会变成什么"比一个需要猜的图标更直接
        self.theme_button = QPushButton()
        self.theme_button.setToolTip("切换浅色 / 深色主题")
        self.theme_button.clicked.connect(self._toggle_theme)

        row.addWidget(self.search_input, 1)
        row.addWidget(self.search_button)
        row.addWidget(self.theme_button)
        return row

    def _build_result_column(self) -> QVBoxLayout:
        """建"结果表格 + 状态 + 进度条"这一列。"""
        column = QVBoxLayout()
        column.setSpacing(4)

        self.result_list = TrackList(_SEARCH_COLUMNS, stretch_column=0)
        self.result_list.row_activated.connect(self._on_result_activated)
        self.result_list.row_menu_requested.connect(self._on_result_menu)
        column.addWidget(self.result_list, 1)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color: {palette('light').muted};")
        column.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        column.addWidget(self.progress)
        return column

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
        self.player_bar.seek_requested.connect(self.playback.seek)
        self.player_bar.volume_changed.connect(self._on_volume_changed)

        self.queue_drawer.row_activated.connect(self.playback.jump_to)
        self.queue_drawer.remove_requested.connect(self.playback.remove_at)
        self.queue_drawer.clear_requested.connect(self.playback.clear)

        self.cover_loader.loaded.connect(self._on_cover_loaded)
        self.cover_loader.failed.connect(self._on_cover_failed)

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
        """把读到的配置推给播放器、播放条与主题(音量、播放模式、深浅色)。"""
        volume = self._config.volume / 100.0
        self.player_bar.set_volume(volume)
        self.playback.set_volume(volume)
        self.player_bar.set_mode(self._config.play_mode)
        self.playback.set_mode(self._config.play_mode)
        self._apply_theme(self._config.theme)

    # ------------------------------------------------------------ 搜索

    def on_search(self) -> None:
        """发起搜索(回车或点按钮都会走到这里)。

        请求是异步的:先把按钮禁掉再发,否则用户连点会叠出多个搜索请求,
        在这个接口上等于自找风控。
        """
        keyword = self.search_input.text().strip()
        if not keyword:
            return
        self.search_button.setEnabled(False)
        self.status_label.setText(f"正在搜索「{keyword}」…")
        self.client.search_video(
            keyword,
            on_success=self._on_search_done,
            on_error=self._on_search_failed,
        )

    def _on_search_done(self, result: SearchResult) -> None:
        """搜索成功:填表并把分P列标成未知(要等详情接口才知道)。"""
        self.search_button.setEnabled(True)
        self._videos = list(result.videos)
        self.result_list.set_rows(
            [
                (
                    video.title,
                    video.author,
                    video.duration_text,
                    _UNKNOWN_PAGES,
                    format_count(video.play_count),
                )
                for video in self._videos
            ]
        )
        self.result_list.set_highlight(-1)
        self.status_label.setText(f"共 {result.total} 条结果,本页 {len(result.videos)} 条")

    def _on_search_failed(self, exc: Exception) -> None:
        """搜索失败:恢复按钮并如实报错。"""
        self.search_button.setEnabled(True)
        self._fail(f"搜索失败:{exc}")

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

    # ------------------------------------------------------------ 编排层回调

    def _on_track_changed(self, item: QueueItem) -> None:
        """换曲目:先把上一首的进度落盘,再更新界面。"""
        self._remember_position()
        # 清掉上一首的封面(空 URL 会让加载器立刻发 failed → 显示占位图)。
        # 顺带把"在飞的旧封面请求"作废,否则它回来时会被当成当前封面贴上。
        self.cover_loader.load("")
        self.status_label.setText(f"正在解析「{item.title}」…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # 不定长进度,表示"忙碌"
        self.result_list.set_highlight(self._result_row_for(item.video.bvid))
        self.queue_drawer.set_current(self.playback.queue.current_index)

    def _on_audio_ready(self, resolved: ResolvedAudio) -> None:
        """音源就绪:更新标题、音质、时长,并记下"上次播的是哪一首"。"""
        self.progress.setVisible(False)
        track = resolved.track
        self.player_bar.set_now_playing(
            resolved.display_title,
            f"{resolved.subtitle} · {track.label}({track.kbps} kbps)",
        )
        self.status_label.setText(f"已缓存 {resolved.path.name}")
        self.setWindowTitle(f"BiliMusic - {resolved.display_title}")
        self._mark_page_count(resolved)
        # 封面是异步的,而且允许失败:拿不到就用占位图,不打断播放
        self.cover_loader.load(resolved.video.cover_https)
        # 位置的落盘交给切歌与退出,这里只更新内存里的"上次播的是哪一首"
        self._config.last_bvid = resolved.video.bvid
        self._config.last_cid = resolved.page.cid

    def _on_cover_loaded(self, url: str, pixmap: QPixmap) -> None:
        """封面到了就贴上;若已经切歌则忽略。

        加载器内部已经拦了一道过期响应,这里再确认一次是因为"当前"的含义在这里
        更严:只有仍是正在播的那一首的封面才配上屏。
        """
        if self.cover_loader.is_current(url):
            self.player_bar.set_cover(pixmap)

    def _on_cover_failed(self, url: str) -> None:
        """封面拿不到:退回占位图,不提示用户(为一张图打断播放不划算)。"""
        if url and not self.cover_loader.is_current(url):
            return
        self.player_bar.set_cover(None)

    # ------------------------------------------------------------ 主题

    def _toggle_theme(self) -> None:
        """在浅色与深色之间切换并落盘。"""
        name = "light" if self._config.theme == "dark" else "dark"
        self._config.theme = name
        self._apply_theme(name)
        self._save_config()

    def _apply_theme(self, name: str) -> None:
        """套用主题:应用级调色板 + 各控件的图标与文字颜色。

        为什么要逐块通知:``QPalette`` 只管控件的底/字/选中色,**图标是 SVG 栅格化
        出来的位图**,不重新染色就会保持旧主题的颜色。

        Args:
            name: ``"light"`` 或 ``"dark"``。

        Raises:
            ValueError: 主题名未知(由 :func:`~bilibili_music.ui.theme.apply_theme` 抛出)。
        """
        app = QApplication.instance()
        self._colors = apply_theme(app, name) if app is not None else palette(name)
        self.status_label.setStyleSheet(f"color: {self._colors.muted};")
        self.search_button.setIcon(get_icon("search", self._colors.muted, 18))
        self.player_bar.apply_palette(self._colors)
        self.queue_drawer.apply_palette(self._colors)
        # 按钮文字表示"点了会变成什么",不是当前主题
        self.theme_button.setText("浅色" if name == "dark" else "深色")

    def _on_queue_changed(self) -> None:
        """队列内容变了:重建抽屉列表并高亮当前项。"""
        self.queue_drawer.set_items(
            self.playback.queue.items, self.playback.queue.current_index
        )

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

    # ------------------------------------------------------------ 配置

    def _remember_position(self) -> None:
        """把"上一首播到哪"写进配置。

        位置信号每几百毫秒就来一次,每次都落盘会把磁盘写花,所以只在**切歌**与
        **退出**这两个时刻保存。此时 ``last_bvid`` / ``last_cid`` 记的仍是上一首
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
            self.result_list.set_cell(row, 3, str(len(resolved.video.pages)))

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
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
