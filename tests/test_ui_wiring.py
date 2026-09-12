"""界面接线测试:在离屏平台上真的把主窗口造出来,验证信号接对了。

这是 M1 里唯一能**自动**验证界面接线的办法。MVP 阶段界面没有测试,改一处信号连接
只能手动开应用去点 —— 而"双击搜索结果会不会真的换掉队列""点下一首会不会真的前进"
这类问题正是拆 widget 时最容易接错的。

离屏平台(``QT_QPA_PLATFORM=offscreen``)让 ``QWidget`` 在无显示器环境里也能构造。

**不触网、不出声**:解析器与播放器都换成替身(真实解析器会发请求,真实播放器会构造
``QMediaPlayer``)。``QMessageBox`` 被替换成记录器,否则一旦走到报错路径,模态弹窗会把
测试挂住。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置,否则无显示环境下起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PySide6.QtGui import QColor, QPalette, QPixmap, QPixmapCache  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, Signal  # noqa: E402

from bilibili_music.api.bilibili import SearchResult, track_from_quality  # noqa: E402
from bilibili_music.audio.playback import PlaybackController  # noqa: E402
from bilibili_music.audio.resolver import ResolvedAudio  # noqa: E402
from bilibili_music.core.config import AppConfig, ConfigStore  # noqa: E402
from bilibili_music.core.models import Page, Video  # noqa: E402
from bilibili_music.core.queue import PlayMode  # noqa: E402
from bilibili_music.ui.icons import DARK  # noqa: E402
from bilibili_music.ui.main_window import MainWindow  # noqa: E402

#: 落盘类用例的临时目录根(与 test_config 同一套做法:不用 tempfile)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``;进程里已经有一个就复用。

    测试进程只允许存在一个应用实例,而 ``tests/test_icons.py`` 也会建 —— 复用是
    唯一可行的做法(两处都用 ``QApplication``,不用 ``QGuiApplication``)。
    """
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


# ====================================================================== 替身


@dataclass(slots=True)
class _Pending:
    """一次解析请求的回调组,交给用例自己触发。"""

    on_success: Callable[[ResolvedAudio], None]
    on_error: Callable[[Exception], None]


class _FakeResolver:
    """解析器替身:记录请求,把回调攒下来供用例触发。"""

    def __init__(self) -> None:
        """建一个没有在飞请求的替身。"""
        self.calls: list[tuple[Video, int, int | None]] = []
        self.cancel_count = 0
        self._pending: _Pending | None = None

    def resolve(
        self,
        video: Video,
        *,
        page_index: int = 1,
        quality_id: int | None = None,
        on_success: Callable[[ResolvedAudio], None],
        on_error: Callable[[Exception], None],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """记录一次解析请求(不发任何网络动作)。"""
        self.calls.append((video, page_index, quality_id))
        self._pending = _Pending(on_success, on_error)

    def cancel(self) -> None:
        """记录一次取消并丢弃待触发的回调。"""
        self.cancel_count += 1
        self._pending = None

    def succeed(self, resolved: ResolvedAudio) -> None:
        """触发最近一次请求的成功回调。"""
        assert self._pending is not None, "没有在飞的解析请求"
        self._pending.on_success(resolved)

    def fail(self, exc: Exception) -> None:
        """触发最近一次请求的失败回调。"""
        assert self._pending is not None, "没有在飞的解析请求"
        self._pending.on_error(exc)


class _FakePlayer(QObject):
    """播放器替身:提供编排层与界面用到的那四个信号。"""

    state_changed = Signal(bool)
    position_changed = Signal(int, int)
    track_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self) -> None:
        """建一个"什么都没加载"的播放器替身。"""
        super().__init__()
        self.loaded: list[Path] = []
        self.seeks: list[int] = []
        self.volumes: list[float] = []
        self.stop_count = 0
        self.play_count = 0
        self.pause_count = 0
        self.toggle_count = 0
        self._position_ms = 0
        self._is_playing = False

    @property
    def is_playing(self) -> bool:
        """是否处于播放状态。"""
        return self._is_playing

    @property
    def position_ms(self) -> int:
        """当前播放位置;用例直接改它来模拟"播到一半"。"""
        return self._position_ms

    def load(self, path: Path, *, autoplay: bool = True) -> None:
        """记录一次加载。"""
        self.loaded.append(Path(path))
        self._is_playing = autoplay

    def play(self) -> None:
        """记录一次继续播放。"""
        self.play_count += 1
        self._is_playing = True

    def pause(self) -> None:
        """记录一次暂停。"""
        self.pause_count += 1
        self._is_playing = False

    def toggle(self) -> None:
        """记录一次播放/暂停切换。"""
        self.toggle_count += 1
        self._is_playing = not self._is_playing

    def stop(self) -> None:
        """记录一次停止。"""
        self.stop_count += 1
        self._is_playing = False

    def seek(self, position_ms: int) -> None:
        """记录一次跳转。"""
        self.seeks.append(int(position_ms))

    def set_volume(self, volume: float) -> None:
        """记录一次音量设置。"""
        self.volumes.append(float(volume))


class _FakeClient:
    """接口客户端替身:只实现界面用到的 ``search_video`` / ``fetch_cover`` / ``close``。"""

    def __init__(self, videos: list[Video]) -> None:
        """记录要返回的搜索结果。

        Args:
            videos: 每次搜索都返回这一批视频。
        """
        self.videos = list(videos)
        self.keywords: list[str] = []
        self.closed = 0
        self.cover_calls: list[str] = []
        self._cover_callbacks: dict[str, tuple[Callable, Callable]] = {}

    def search_video(
        self,
        keyword: str,
        *,
        page: int = 1,
        on_success: Callable[[SearchResult], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """同步回调一条搜索结果(界面只关心"填表对不对")。"""
        self.keywords.append(keyword)
        on_success(SearchResult(videos=list(self.videos), total=len(self.videos)))

    def fetch_cover(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """记录封面请求,把回调攒下来交给用例触发(按 URL 存,能模拟慢响应)。"""
        self.cover_calls.append(url)
        self._cover_callbacks[url] = (on_success, on_error)

    def succeed_cover(self, url: str, data: bytes) -> None:
        """触发某个封面 URL 的成功回调。"""
        self._cover_callbacks[url][0](data)

    def fail_cover(self, url: str, exc: Exception) -> None:
        """触发某个封面 URL 的失败回调。"""
        self._cover_callbacks[url][1](exc)

    def close(self) -> None:
        """记录一次关闭。"""
        self.closed += 1


# ====================================================================== 工具


def _video(bvid: str, *, pages: int = 1) -> Video:
    """造一个带分P与封面的视频样本(纯内存)。"""
    return Video(
        bvid=bvid,
        title=f"视频{bvid}",
        author="某UP",
        cid=1000,
        cover_url=f"//i0.hdslb.com/{bvid}.jpg",
        pages=[
            Page(index=i + 1, cid=1000 + i, title=f"第{i + 1}首", duration=180)
            for i in range(pages)
        ],
    )


def _png_bytes() -> bytes:
    """造一段**真能解码**的 PNG,用来区分"有封面"与"只有占位图"。"""
    pixmap = QPixmap(4, 4)
    pixmap.fill(Qt.GlobalColor.red)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


def _resolved(video: Video, *, page_index: int = 1, quality_id: int = 30280) -> ResolvedAudio:
    """按视频造一份解析结果,路径是假的(不会被真正读取)。"""
    page = video.page(page_index)
    assert page is not None
    return ResolvedAudio(
        video=video,
        page=page,
        track=track_from_quality(quality_id),
        path=Path(f"C:/fake/{video.bvid}.m4a"),
    )


class _WindowCase(unittest.TestCase):
    """装好替身与主窗口的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建逐用例的临时目录、替身与主窗口;并拦掉模态弹窗。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        # 用 cleanup 而不是 tearDown 来删目录:cleanup 是**后进先出**的,所以这条
        # 最先注册、最后执行。个别用例会额外建窗口并用 addCleanup(window.close)
        # 注册关闭 —— 关闭会写配置,若把删目录放在 tearDown 里,就会在删除之后
        # 又被写回来,在工作区里留下 tests/_scratch 的残骸。
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # QPixmapCache 是进程级共享的:不清的话,上一个用例缓存过的封面会让这一个
        # 用例直接命中缓存、不发请求(表现为"封面请求列表是空的")
        QPixmapCache.clear()

        self.videos = [_video("A"), _video("B", pages=2), _video("C")]
        self.client = _FakeClient(self.videos)
        self.resolver = _FakeResolver()
        self.player = _FakePlayer()
        self.playback = PlaybackController(self.resolver, self.player)  # type: ignore[arg-type]
        self.store = ConfigStore(self.tmp / "config.json")

        # 模态弹窗在离屏测试里会把用例挂住,换成记录器
        self.warnings: list[str] = []
        patcher = mock.patch.object(
            QMessageBox, "warning", lambda *args, **kwargs: self.warnings.append(args[2])
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.window = MainWindow(
            client=self.client,  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            config_store=self.store,
            playback=self.playback,
        )

    def tearDown(self) -> None:
        """关掉窗口(触发退出清理);临时目录由 cleanup 负责删。"""
        self.window.close()

    def _search(self, keyword: str = "周杰伦") -> None:
        """走一遍"输入关键字 + 点搜索"。"""
        self.window.search_input.setText(keyword)
        self.window.on_search()

    def _search_and_play(self, row: int) -> None:
        """走一遍"搜索 + 双击第 row 行"。"""
        self._search()
        self.window.result_list.row_activated.emit(row)


# ====================================================================== 用例


class TestSearchWiring(_WindowCase):
    """搜索与结果列表。"""

    def test_search_fills_the_result_list(self) -> None:
        """搜索成功后结果表要填满,分P列在详情补全前显示占位符。"""
        self._search()
        self.assertEqual(self.client.keywords, ["周杰伦"])
        self.assertEqual(self.window.result_list.rowCount(), 3)
        self.assertEqual(self.window.result_list.item(0, 0).text(), "视频A")
        self.assertEqual(self.window.result_list.item(0, 3).text(), "?")

    def test_empty_keyword_does_not_search(self) -> None:
        """空关键字不发请求(否则会白挨一次风控)。"""
        self.window.search_input.setText("   ")
        self.window.on_search()
        self.assertEqual(self.client.keywords, [])

    def test_double_click_replaces_the_queue_from_that_row(self) -> None:
        """双击搜索结果:整个结果成为队列,并从那一行开始播。"""
        self._search_and_play(1)
        self.assertEqual(len(self.playback.queue), 3)
        self.assertEqual(self.playback.current.video.bvid, "B")
        self.assertEqual(self.resolver.calls[-1][0].bvid, "B")

    def test_track_list_maps_double_click_to_a_row_number(self) -> None:
        """Qt 的 doubleClicked 信号要能折算成行号,否则双击不会有反应。"""
        self._search()
        rows: list[int] = []
        self.window.result_list.row_activated.connect(rows.append)
        self.window.result_list.doubleClicked.emit(
            self.window.result_list.model().index(2, 0)
        )
        self.assertEqual(rows, [2])

    def test_page_count_is_filled_after_detail_is_known(self) -> None:
        """详情补全后,结果表里那一行的分P数要变成真实值。"""
        self._search_and_play(1)
        self.resolver.succeed(_resolved(self.videos[1]))
        self.assertEqual(self.window.result_list.item(1, 3).text(), "2")


class TestPlayerBarWiring(_WindowCase):
    """播放条的按钮与滑块。"""

    def test_play_button_toggles(self) -> None:
        """播放/暂停按钮要走到编排层的 toggle。"""
        self._search_and_play(0)
        self.window.player_bar.play_button.click()
        self.assertEqual(self.player.toggle_count, 1)

    def test_next_button_advances(self) -> None:
        """下一首要真的换歌。"""
        self._search_and_play(0)
        self.window.player_bar.next_button.click()
        self.assertEqual(self.playback.current.video.bvid, "B")

    def test_next_at_the_end_reports_instead_of_doing_nothing(self) -> None:
        """最后一首再点下一首要给出提示,不能让按钮看起来像坏了。"""
        self._search_and_play(2)
        self.window.player_bar.next_button.click()
        self.assertEqual(self.window.status_label.text(), "已经是最后一首")

    def test_previous_button_goes_back(self) -> None:
        """上一首要回到前一首。"""
        self._search_and_play(2)
        self.window.player_bar.previous_button.click()
        self.assertEqual(self.playback.current.video.bvid, "B")

    def test_mode_button_cycles_and_persists(self) -> None:
        """模式按钮按顺序切换,并把结果落盘。"""
        self.window.player_bar.mode_button.click()
        self.assertIs(self.playback.queue.mode, PlayMode.REPEAT_ALL)
        self.assertIs(self.store.load().play_mode, PlayMode.REPEAT_ALL)

    def test_quality_combo_drives_quality(self) -> None:
        """选中音质下拉框要带着档位重新解析当前曲目。"""
        self._search_and_play(0)
        index = self.window.player_bar.quality_combo.findData(30216)
        self.window.player_bar.quality_combo.setCurrentIndex(index)
        self.assertEqual(self.playback.quality_id, 30216)
        self.assertEqual(self.resolver.calls[-1][2], 30216)

    def test_volume_change_applies_and_persists(self) -> None:
        """音量滑块要同时作用到播放器与配置。"""
        self.window.player_bar.volume_slider.setValue(30)
        self.assertEqual(self.player.volumes[-1], 0.3)
        self.assertEqual(self.store.load().volume, 30)

    def test_seek_is_forwarded(self) -> None:
        """拖完进度条要把目标位置交给编排层。"""
        self._search_and_play(0)
        self.window.player_bar.position_slider.setRange(0, 200_000)
        self.window.player_bar.position_slider.setValue(12_345)
        self.window.player_bar.position_slider.sliderReleased.emit()
        self.assertEqual(self.player.seeks[-1], 12_345)


class TestQueueDrawerWiring(_WindowCase):
    """右侧队列抽屉。"""

    def test_drawer_lists_the_queue_and_marks_current(self) -> None:
        """队列内容与数量要跟着编排层走。"""
        self._search_and_play(1)
        self.assertEqual(self.window.queue_drawer.list.rowCount(), 3)
        self.assertEqual(self.window.queue_drawer.count_label.text(), "3 首")
        self.assertEqual(self.window.queue_drawer.list.highlighted_row(), 1)

    def test_activating_a_row_jumps_to_it(self) -> None:
        """双击队列某一行要跳到那一首。"""
        self._search_and_play(0)
        self.window.queue_drawer.row_activated.emit(2)
        self.assertEqual(self.playback.current.video.bvid, "C")

    def test_remove_requested_removes_from_the_queue(self) -> None:
        """抽屉发出的"移除"要作用到队列上。"""
        self._search_and_play(0)
        self.window.queue_drawer.remove_requested.emit(2)
        self.assertEqual(len(self.playback.queue), 2)

    def test_clear_requested_empties_the_queue(self) -> None:
        """清空按钮要清空队列。"""
        self._search_and_play(0)
        self.window.queue_drawer.clear_requested.emit()
        self.assertEqual(len(self.playback.queue), 0)

    def test_collapse_hides_the_list(self) -> None:
        """折叠后只留标题栏,列表与清空按钮都藏起来。"""
        self.assertFalse(self.window.queue_drawer.collapsed)
        self.window.queue_drawer.toggle_button.click()
        self.assertTrue(self.window.queue_drawer.collapsed)
        self.assertTrue(self.window.queue_drawer.list.isHidden())
        self.assertTrue(self.window.queue_drawer.clear_button.isHidden())

    def test_expanding_again_restores_the_list(self) -> None:
        """再点一次要能展开回来。"""
        self.window.queue_drawer.toggle_button.click()
        self.window.queue_drawer.toggle_button.click()
        self.assertFalse(self.window.queue_drawer.collapsed)
        self.assertFalse(self.window.queue_drawer.list.isHidden())


class TestPlaybackFeedbackWiring(_WindowCase):
    """编排层信号回到界面的那一半。"""

    def test_audio_ready_updates_now_playing(self) -> None:
        """音源就绪后标题、副标题与状态栏都要更新。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        self.assertIn("视频A", self.window.player_bar.title_label.text())
        self.assertIn("192K", self.window.player_bar.subtitle_label.text())
        self.assertIn("A.m4a", self.window.status_label.text())
        self.assertTrue(self.window.progress.isHidden())  # 缓存完成,进度条收起

    def test_progress_bar_tracks_cache_progress(self) -> None:
        """下载进度要显示成百分比。"""
        self._search_and_play(0)
        self.playback.progress.emit(512, 1024)
        self.assertEqual(self.window.progress.value(), 50)
        self.assertIn("缓存中", self.window.status_label.text())

    def test_state_changed_drives_the_play_button(self) -> None:
        """播放状态要换掉播放按钮的图标与提示。"""
        self.player.state_changed.emit(True)
        self.assertEqual(self.window.player_bar.play_button.toolTip(), "暂停")
        self.player.state_changed.emit(False)
        self.assertEqual(self.window.player_bar.play_button.toolTip(), "播放")

    def test_position_changed_updates_the_bar(self) -> None:
        """位置信号要驱动进度条与时间标签。"""
        self.player.position_changed.emit(65_000, 180_000)
        self.assertEqual(self.window.player_bar.duration_label.text(), "3:00")
        self.assertEqual(self.window.player_bar.position_label.text(), "1:05")

    def test_track_finished_walks_pages_within_the_ui(self) -> None:
        """合集内部播完一P要自动接下一P(整条链路走界面拿到的信号)。"""
        self._search_and_play(1)
        self.player.track_finished.emit()
        self.assertEqual(self.resolver.calls[-1][0].bvid, "B")
        self.assertEqual(self.resolver.calls[-1][1], 2)

    def test_stopped_shows_a_message(self) -> None:
        """编排层说没有下一项时状态栏要说话。"""
        self.playback.stopped.emit()
        self.assertEqual(self.window.status_label.text(), "播放结束")

    def test_resolve_failure_shows_a_warning(self) -> None:
        """解析失败必须让用户看见(这里用记录器代替模态弹窗)。"""
        self._search_and_play(0)
        self.resolver.fail(RuntimeError("音源没了"))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("音源没了", self.warnings[0])
        self.assertIn("音源没了", self.window.status_label.text())


class TestConfigWiring(_WindowCase):
    """配置的读取与落盘。"""

    def test_saved_volume_and_mode_are_applied_on_start(self) -> None:
        """启动时要套用上次的音量与播放模式。"""
        self.store.save(AppConfig(volume=42, play_mode=PlayMode.SHUFFLE))
        window = MainWindow(
            client=_FakeClient([]),  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            config_store=self.store,
            playback=PlaybackController(_FakeResolver(), _FakePlayer()),  # type: ignore[arg-type]
        )
        self.addCleanup(window.close)
        self.assertEqual(window.player_bar.volume_slider.value(), 42)
        self.assertIs(window.playback.queue.mode, PlayMode.SHUFFLE)

    def test_switch_track_persists_the_previous_position(self) -> None:
        """切歌时把上一首的进度落盘,这样"上次播到哪"才有意义。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        self.player.position_changed.emit(45_000, 180_000)
        self.window.player_bar.next_button.click()
        saved = self.store.load()
        self.assertEqual(saved.last_bvid, "A")
        self.assertEqual(saved.last_cid, 1000)
        self.assertEqual(saved.last_position_ms, 45_000)

    def test_close_persists_position_and_releases_resources(self) -> None:
        """退出时要落盘进度、停播放并关掉网络客户端。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        self.player.position_changed.emit(9_000, 180_000)
        self.window.close()
        self.assertEqual(self.store.load().last_position_ms, 9_000)
        self.assertGreaterEqual(self.player.stop_count, 1)
        self.assertEqual(self.client.closed, 1)

    def test_config_save_failure_does_not_break_playback(self) -> None:
        """配置写不进去时只提示,不能中断播放。"""
        with mock.patch.object(
            ConfigStore, "save", side_effect=OSError("磁盘满了")
        ):
            self.window.player_bar.volume_slider.setValue(10)
        self.assertIn("配置保存失败", self.window.status_label.text())
        self.assertEqual(self.player.volumes[-1], 0.1)


class TestCoverWiring(_WindowCase):
    """封面:请求时机、显示、失败兜底与"切歌作废旧响应"。"""

    def _play_and_request_cover(self) -> str:
        """播第一首并返回它请求的封面地址。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        return self.client.cover_calls[-1]

    def test_audio_ready_requests_the_https_cover(self) -> None:
        """详情就绪后要去取封面,而且用的是升级成 https 的地址。"""
        self._play_and_request_cover()
        self.assertEqual(
            self.client.cover_calls, [self.videos[0].cover_https]
        )
        self.assertTrue(self.client.cover_calls[0].startswith("https://"))

    def test_cover_is_shown_when_it_arrives(self) -> None:
        """封面到达后要真的贴上,而不是一直挂占位图。"""
        url = self._play_and_request_cover()
        self.assertFalse(self.window.player_bar.has_cover)
        self.client.succeed_cover(url, _png_bytes())
        self.assertTrue(self.window.player_bar.has_cover)

    def test_unreadable_bytes_fall_back_to_placeholder(self) -> None:
        """拿到的不是图片就退回占位图,不能把乱码贴上去。"""
        url = self._play_and_request_cover()
        self.client.succeed_cover(url, b"not an image")
        self.assertFalse(self.window.player_bar.has_cover)

    def test_cover_failure_is_silent(self) -> None:
        """取封面失败不干扰用户:不弹窗、不报警,只显示占位图。"""
        url = self._play_and_request_cover()
        self.client.fail_cover(url, RuntimeError("超时"))
        self.assertFalse(self.window.player_bar.has_cover)
        self.assertEqual(self.warnings, [])

    def test_switching_track_clears_the_previous_cover(self) -> None:
        """换歌要先把旧封面撤掉,否则会挂着上一首的图等新封面。"""
        url = self._play_and_request_cover()
        self.client.succeed_cover(url, _png_bytes())
        self.assertTrue(self.window.player_bar.has_cover)
        self.window.player_bar.next_button.click()
        self.assertFalse(self.window.player_bar.has_cover)

    def test_late_cover_of_the_previous_track_is_ignored(self) -> None:
        """切歌之后才回来的旧封面不许贴上去。"""
        url = self._play_and_request_cover()
        self.window.player_bar.next_button.click()
        self.client.succeed_cover(url, _png_bytes())
        self.assertFalse(self.window.player_bar.has_cover)


class TestThemeWiring(_WindowCase):
    """主题切换、落盘与启动时套用。"""

    def setUp(self) -> None:
        """跑完把应用恢复成浅色:主题是应用级的,会影响到同进程的其它测试模块。"""
        super().setUp()
        self.addCleanup(self.window._apply_theme, "light")

    def _window_color(self) -> str:
        """当前应用级窗口底色。"""
        app = QApplication.instance()
        assert app is not None
        return app.palette().color(QPalette.ColorRole.Window).name()

    def test_toggle_switches_to_dark_and_persists(self) -> None:
        """点一下切深色:应用底色变、按钮文字变、配置落盘。"""
        self.assertEqual(self.window.theme_button.text(), "深色")  # 写的是"点了会变成什么"
        self.window.theme_button.click()
        self.assertEqual(self._window_color(), "#2b2b2b")
        self.assertEqual(self.window.theme_button.text(), "浅色")
        self.assertEqual(self.store.load().theme, "dark")

    def test_toggle_twice_returns_to_light(self) -> None:
        """再点一下切回浅色并落盘。"""
        self.window.theme_button.click()
        self.window.theme_button.click()
        self.assertNotEqual(self._window_color(), "#2b2b2b")
        self.assertEqual(self.store.load().theme, "light")

    def test_saved_theme_is_applied_on_start(self) -> None:
        """启动时要套用上次的主题。"""
        self.store.save(AppConfig(theme="dark"))
        window = MainWindow(
            client=_FakeClient([]),  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            config_store=self.store,
            playback=PlaybackController(_FakeResolver(), _FakePlayer()),  # type: ignore[arg-type]
        )
        self.addCleanup(window.close)
        self.assertEqual(window.theme_button.text(), "浅色")
        self.assertEqual(self._window_color(), "#2b2b2b")

    def test_icon_colors_are_pushed_to_widgets(self) -> None:
        """图标调色板要真的下发到各控件:否则深色下图标仍是浅色主题的颜色。"""
        self.window.theme_button.click()
        self.assertIn(DARK.muted, self.window.player_bar.subtitle_label.styleSheet())
        self.assertIn(DARK.muted, self.window.queue_drawer.count_label.styleSheet())
        self.assertIn(DARK.muted, self.window.status_label.styleSheet())


if __name__ == "__main__":
    unittest.main()
