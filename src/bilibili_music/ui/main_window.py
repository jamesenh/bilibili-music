"""最小可用主窗口:搜索 -> 选中 -> 解析下载 -> 播放。

这是 MVP 验证界面,刻意保持单文件、少抽象,先证明整条链路能跑通。
后续做正式 UI 时再把列表项、歌词面板、歌单抽屉拆成独立 widget。

网络层改成 ``QNetworkAccessManager`` 之后,**界面里没有任何线程**:
搜索、解析、下载全部是回调驱动,所以不需要 QThread,也不需要跨线程信号转发。

界面层的职责边界(``AGENTS.md`` 第 4 节):只做展示与事件转发。
这里不解析任何接口 JSON(交给 ``api`` 的 ``parse_*``),也不算缓存键
(交给 ``core.cache``),更不判断音质档位(交给 ``audio.resolver``)。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..api.bilibili import BilibiliClient, SearchResult
from ..audio.player import DEFAULT_VOLUME, PlayerController
from ..audio.resolver import AudioResolver, ResolvedAudio
from ..core.cache import AudioCache
from ..core.models import Video, format_count
from .icons import app_icon_path, get_icon, palette

__all__ = ["MainWindow", "run"]

#: 播放控件图标的逻辑尺寸。按钮本身给 36×36,留出 6px 内边距。
BUTTON_ICON_SIZE = 20

#: 结果表格的列定义(标题 / UP主 / 时长 / 分P / 播放量)。
#:
#: 写成常量是为了让"列数"与"表头文字"只有一个来源 —— 加列时不会漏改
#: ``QTableWidget(0, 5)`` 里的那个 5。
_TABLE_COLUMNS = ("标题", "UP主", "时长", "分P", "播放量")


class MainWindow(QMainWindow):
    """主窗口。

    持有整条链路的四个协作对象(客户端 / 缓存 / 解析器 / 播放器),
    但本身只负责把它们的状态反映到控件上。

    属性:
        client: B站接口客户端。
        cache: 音频磁盘缓存。
        player: 播放器控制器。
        resolver: 音源解析器。
    """

    def __init__(self, client: BilibiliClient | None = None) -> None:
        """构建窗口、控件与信号连接。

        Args:
            client: 注入的接口客户端,便于复用已预热会话或换用 urllib 后端;
                ``None`` 表示就地新建一个 Qt 后端。两种情况下窗口都会在关闭时
                调用它的 ``close()``,所以不要把生命周期比窗口更长的实例传进来。
        """
        super().__init__()
        self.setWindowTitle("BiliMusic - MVP 验证")
        self.resize(940, 620)
        self._apply_window_icon()

        self.client = client or BilibiliClient()
        self.cache = AudioCache()
        self.player = PlayerController(self)
        self.resolver = AudioResolver(self.client, self.cache)

        # 图标颜色统一取自调色板,后续接深色主题只需换成 icons.palette("dark")
        self._palette = palette("light")

        self._videos: list[Video] = []
        self._current: Video | None = None
        self._current_page = 1
        # 拖动进度条时不要被播放位置回写打断
        self._seeking = False

        self._build_ui()
        self._connect()

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self) -> None:
        """一次性搭好所有控件并塞进布局。

        刻意不做"按需创建控件"的懒加载:控件总数只有十几个,启动成本可以忽略,
        而集中构建让布局结构一眼可见。
        """
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # --- 搜索行 ---
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索 B站音乐视频,例如:周杰伦 MV")
        self.search_input.returnPressed.connect(self.on_search)
        self.search_button = QPushButton("搜索")
        self.search_button.setIcon(
            get_icon(
                "search",
                self._palette.muted,
                BUTTON_ICON_SIZE,
                disabled_color=self._palette.disabled,
            )
        )
        self.search_button.setIconSize(QSize(BUTTON_ICON_SIZE, BUTTON_ICON_SIZE))
        self.search_button.clicked.connect(self.on_search)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        # --- 结果表格 ---
        self.table = QTableWidget(0, len(_TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_TABLE_COLUMNS))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(_TABLE_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self.on_row_activated)
        layout.addWidget(self.table, 1)

        # --- 分P选择(仅多P视频显示) ---
        self.page_row = QWidget()
        page_layout = QHBoxLayout(self.page_row)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(QLabel("分P"))
        self.page_combo = QComboBox()
        self.page_combo.setMinimumWidth(360)
        self.page_combo.activated.connect(self.on_page_changed)
        page_layout.addWidget(self.page_combo, 1)
        self.page_row.setVisible(False)
        layout.addWidget(self.page_row)

        # --- 播放信息 ---
        self.now_playing = QLabel("未播放")
        self.now_playing.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.now_playing)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color: {self._palette.muted};")
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # --- 播放控制 ---
        control_row = QHBoxLayout()
        # 播放/暂停共用一个按钮,靠图标和 tooltip 表达状态,不再用 ▶ / ⏸ 字符
        self.play_button = QPushButton()
        self.play_button.setFixedSize(36, 36)
        self.play_button.setIcon(self._transport_icon("play"))
        self.play_button.setIconSize(QSize(BUTTON_ICON_SIZE, BUTTON_ICON_SIZE))
        self.play_button.setToolTip("播放")
        self.play_button.clicked.connect(self.on_toggle_play)

        self.position_label = QLabel("0:00")
        self.duration_label = QLabel("0:00")

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderPressed.connect(self._on_slider_pressed)
        self.position_slider.sliderReleased.connect(self._on_slider_released)

        volume_label = QLabel("音量")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        # 与播放器默认音量保持同源,避免"控件显示 80 而实际音量是别的值"
        self.volume_slider.setValue(round(DEFAULT_VOLUME * 100))
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.valueChanged.connect(lambda v: self.player.set_volume(v / 100))

        control_row.addWidget(self.play_button)
        control_row.addWidget(self.position_label)
        control_row.addWidget(self.position_slider, 1)
        control_row.addWidget(self.duration_label)
        control_row.addWidget(volume_label)
        control_row.addWidget(self.volume_slider)
        layout.addLayout(control_row)

        self.setCentralWidget(root)

    def _connect(self) -> None:
        """把播放器的信号接到界面槽上(只连一次,避免重复 emit 触发多次刷新)。"""
        self.player.state_changed.connect(self._on_state_changed)
        self.player.position_changed.connect(self._on_position_changed)
        self.player.track_finished.connect(self._on_track_finished)
        self.player.error_occurred.connect(self._on_player_error)

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

    # ------------------------------------------------------------ 交互

    def on_search(self) -> None:
        """发起搜索(回车或点按钮都会走到这里)。

        立刻禁用按钮并置提示,再发请求 —— 请求是异步的,不先禁用的话
        连点几次会产生多个并发请求,白白消耗风控额度。
        """
        keyword = self.search_input.text().strip()
        if not keyword:
            return
        self.search_button.setEnabled(False)
        self.status_label.setText(f"正在搜索「{keyword}」…")
        # 回调式:请求发出后立即返回,界面不会卡住
        self.client.search_video(
            keyword,
            on_success=self._on_search_done,
            on_error=self._on_search_failed,
        )

    def _on_search_done(self, result: SearchResult) -> None:
        """搜索成功:填表格并显示结果统计。"""
        self.search_button.setEnabled(True)
        self._videos = result.videos
        self._fill_table()
        self.status_label.setText(
            f"共 {result.total} 条结果,本页 {len(result.videos)} 条"
        )

    def _on_search_failed(self, exc: Exception) -> None:
        """搜索失败:恢复按钮可用并弹错误。"""
        self.search_button.setEnabled(True)
        self._fail(f"搜索失败:{exc}")

    def _fill_table(self) -> None:
        """按当前搜索结果重建表格。

        整体重建而不是增量更新:一页最多 30 行,重建成本可以忽略,
        却省掉了"新结果比旧结果少时残留旧行"这类同步 bug。
        """
        self.table.setRowCount(0)
        for video in self._videos:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(video.title))
            self.table.setItem(row, 1, QTableWidgetItem(video.author))
            self.table.setItem(row, 2, QTableWidgetItem(video.duration_text))
            # 分P数只有详情接口才知道,搜索结果里先标"?"
            self.table.setItem(row, 3, QTableWidgetItem("?"))
            self.table.setItem(row, 4, QTableWidgetItem(format_count(video.play_count)))

    def on_row_activated(self, index) -> None:
        """双击某一行:播放对应的视频。

        Args:
            index: 被双击的单元格索引(取 ``row()`` 定位视频)。
        """
        row = index.row()
        if 0 <= row < len(self._videos):
            self.play_video(self._videos[row])

    def on_page_changed(self) -> None:
        """下拉框切换分P:重新解析并播放该分P。"""
        page_index = self.page_combo.currentData()
        if page_index is None or self._current is None:
            return
        self._start_resolve(self._current, int(page_index))

    def play_video(self, video: Video) -> None:
        """选中一个视频:取分P信息 -> 解析并播放第 1P。

        面板上的文字先按搜索结果里的信息填一遍,不等详情回来 ——
        详情请求有网络延迟,先填能让双击立刻有反馈。
        """
        self.resolver.cancel()
        self._current = video
        self._current_page = 1
        self.now_playing.setText(f"♪ {video.title} — {video.author}")
        self.duration_label.setText(video.duration_text)
        self.status_label.setText("正在获取分P信息…")
        self._start_resolve(video, 1)

    def _start_resolve(self, video: Video, page_index: int) -> None:
        """统一的"开始解析某个分P"入口(首次播放与自动下一首都走它)。

        进度条切成不定长(``setRange(0, 0)``)表示"忙碌中":
        这时还不知道文件总大小,给确定区间只会显示一个假的 0%。
        """
        self._current_page = page_index
        self._set_page_combo(page_index)
        self.status_label.setText("正在解析音源…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # 不定长进度,表示"忙碌"
        self.play_button.setEnabled(False)

        self.resolver.resolve(
            video,
            page_index=page_index,
            on_success=self._on_resolved,
            on_error=self._on_resolve_failed,
            on_progress=self._on_progress,
        )

    # ------------------------------------------------------------ 播放回调

    def _on_progress(self, done: int, total: int) -> None:
        """下载进度:有总长就显示百分比 + MB,没有就只显示已下载量。

        ``total`` 为 0 是常见情况 —— CDN 不一定会给 ``Content-Length``。
        """
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(done / total * 100))
            self.status_label.setText(
                f"缓存中 {done / 1048576:.1f} / {total / 1048576:.1f} MB"
            )
        else:
            self.status_label.setText(f"缓存中 {done / 1048576:.1f} MB")

    def _on_resolved(self, resolved: ResolvedAudio) -> None:
        """音源就绪:刷新分P列表、更新状态文字,然后交给播放器加载本地文件。"""
        self.progress.setVisible(False)
        self.play_button.setEnabled(True)
        self._refresh_pages(resolved.video)
        page = resolved.page
        self.status_label.setText(
            f"音质 {resolved.track.label}({resolved.track.kbps} kbps) · "
            f"P{page.index} {page.duration_text} · 已缓存 {resolved.path.name}"
        )
        self.now_playing.setText(f"♪ {resolved.display_title} — {resolved.subtitle}")
        self.player.load(resolved.path, autoplay=True)
        self.setWindowTitle(f"BiliMusic - {resolved.display_title}")

    def _on_resolve_failed(self, exc: Exception) -> None:
        """解析/下载失败:弹窗提示(消息里已带足够定位信息)。"""
        self._fail(str(exc))

    def on_toggle_play(self) -> None:
        """播放/暂停按钮的槽(状态图标由 ``state_changed`` 回写)。"""
        self.player.toggle()

    # ------------------------------------------------------------ 分P下拉框

    def _refresh_pages(self, video: Video) -> None:
        """把已知的分P列表填进下拉框,并把表格里的分P数补上。

        填充期间 ``blockSignals(True)``:``clear()`` + ``addItem()`` 会连环触发
        ``activated``,不挡掉就会在刷新列表的过程中又发起一次解析。
        """
        self.page_combo.blockSignals(True)
        self.page_combo.clear()
        for page in video.pages:
            self.page_combo.addItem(page.label, page.index)
        self.page_row.setVisible(video.is_multipart)
        self._set_page_combo(self._current_page)
        self.page_combo.blockSignals(False)

        for row, item in enumerate(self._videos):
            if item.bvid == video.bvid and video.pages:
                cell = self.table.item(row, 3)
                if cell is not None:
                    cell.setText(str(len(video.pages)))
                break

    def _set_page_combo(self, page_index: int) -> None:
        """把下拉框同步到正在播放的分P,不触发切换信号。

        必须挡信号:自动播放 P2 时若触发 ``activated``,会再发起一次对同一分P的解析,
        等于把刚下好的音频又解析一遍。
        """
        self.page_combo.blockSignals(True)
        for i in range(self.page_combo.count()):
            if self.page_combo.itemData(i) == page_index:
                self.page_combo.setCurrentIndex(i)
                break
        self.page_combo.blockSignals(False)

    # ------------------------------------------------------------ 播放器回调

    def _on_state_changed(self, playing: bool) -> None:
        """播放状态变化:同步按钮图标与 tooltip。"""
        self.play_button.setIcon(self._transport_icon("pause" if playing else "play"))
        self.play_button.setToolTip("暂停" if playing else "播放")

    def _on_position_changed(self, position: int, duration: int) -> None:
        """播放位置/总时长变化:更新进度条与两个时间标签。

        拖动中(``self._seeking``)不写回滑块位置 —— 否则用户的手和播放器会互相
        抢滑块,表现为"拖不动"。
        """
        if duration > 0:
            self.position_slider.setRange(0, duration)
        if not self._seeking:
            self.position_slider.setValue(position)
            self.position_label.setText(self.player.format_ms(position))
        self.duration_label.setText(self.player.format_ms(duration))

    def _on_track_finished(self) -> None:
        """播完自动跳下一个分P(多P合集时相当于自动下一首)。

        单P视频与最后一个分P都只是改状态文字,不做任何自动重播 ——
        音乐区合集很长,连续自动播放本身就已经够激进的了。
        """
        video = self._current
        if video is None or not video.is_multipart:
            self.status_label.setText("播放结束")
            return
        next_index = self._current_page + 1
        if video.page(next_index) is None:
            self.status_label.setText("播放结束(已是最后一个分P)")
            return
        self.status_label.setText(f"自动播放 P{next_index}…")
        self._start_resolve(video, next_index)

    def _on_slider_pressed(self) -> None:
        """按下进度条:进入"拖动中",暂停位置回写。"""
        self._seeking = True

    def _on_slider_released(self) -> None:
        """松开进度条:退出拖动状态并真正跳转。"""
        self._seeking = False
        self.player.seek(self.position_slider.value())

    def _on_player_error(self, message: str) -> None:
        """播放器报错:只改状态栏,不弹窗。

        播放失败通常只是单个文件的问题(编码不支持、文件被占用),
        弹窗会打断"自动下一首"的连续播放体验。
        """
        self.status_label.setText(f"播放错误:{message}")

    # ------------------------------------------------------------ 工具

    def _transport_icon(self, name: str) -> QIcon:
        """取一个播放控件图标(走调色板,带禁用态)。"""
        return get_icon(
            name,
            self._palette.text,
            BUTTON_ICON_SIZE,
            disabled_color=self._palette.disabled,
        )

    def _fail(self, message: str) -> None:
        """统一的失败展示:收起进度、恢复按钮、写状态栏并弹窗。"""
        self.progress.setVisible(False)
        self.play_button.setEnabled(True)
        self.status_label.setText(message)
        QMessageBox.warning(self, "出错了", message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        """关窗时按依赖顺序收尾,避免留下在途请求与还在出声的播放器。

        顺序是先取消解析(让下载回调不再碰界面)、再停播放器、最后关网络。
        """
        self.resolver.cancel()
        self.player.stop()
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
