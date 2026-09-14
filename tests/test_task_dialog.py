"""下载任务对话框与主窗口接线的单元测试。

分两半(与 ``tests/test_history_page.py`` 同一套组织方式):

* :class:`TestTaskDialogWidget` 只测控件本身(填数据、逐行刷新、按钮文案与信号转发、
  空态、汇总行),用离屏平台构造,不碰主窗口;
* :class:`TestTaskWiring` 把**真的** ``Downloader``(落在沙箱缓存与沙箱库上)接进主窗口,
  验证"右键能建任务、缓存页能打开任务对话框、角标会动、撤销失败会弹窗"。

下载信道是替身,所以既不触网也不出声;模态弹窗(``warning`` / ``question``)全部换成
记录器,否则离屏测试会挂住。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置,否则无显示环境下起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PySide6.QtCore import QObject, QUrl, Signal  # noqa: E402

from bilibili_music.api.bilibili import SearchResult  # noqa: E402
from bilibili_music.audio.downloader import Downloader  # noqa: E402
from bilibili_music.audio.playback import PlaybackController  # noqa: E402
from bilibili_music.audio.resolver import AudioResolver  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.config import ConfigStore  # noqa: E402
from bilibili_music.core.cover_cache import CoverCache  # noqa: E402
from bilibili_music.core.download_task import (  # noqa: E402
    DownloadTask,
    DownloadTaskStore,
    TaskState,
)
from bilibili_music.core.library_db import LibraryDb  # noqa: E402
from bilibili_music.core.models import AudioTrack, Page, Video  # noqa: E402
from bilibili_music.ui.main_window import MainWindow  # noqa: E402
from bilibili_music.ui.widgets import TaskDialog  # noqa: E402

#: 落盘类用例的临时目录根(与 test_cache_page 同一套做法:不用 tempfile)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"

#: 替身下载下来的一块数据。
CHUNK = b"x" * 512


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


def _video(bvid: str = "BV1", count: int = 3) -> Video:
    """造一个带 ``count`` 个分P的视频(分P序号从 1 开始,``cid`` 为 ``100 * 序号``)。"""
    return Video(
        bvid=bvid,
        title=f"合集{bvid}",
        author="某UP",
        cover_url="//i0.hdslb.com/a.jpg",
        cid=100,
        pages=[
            Page(index=index, cid=100 * index, title=f"第{index}首", duration=200)
            for index in range(1, count + 1)
        ],
    )


def _task(bvid: str = "BV1", **kwargs) -> DownloadTask:
    """造一条任务(字段随手可改,便于构造各种状态)。"""
    base: dict = {
        "bvid": bvid,
        "title": f"合集{bvid}",
        "total_pages": 3,
        "done_pages": 1,
        "bytes_done": 2048,
        "page_index": 2,
        "state": TaskState.RUNNING,
    }
    base.update(kwargs)
    return DownloadTask(**base)


# ====================================================================== 替身


class _FakeHandle:
    """请求句柄替身:只记下有没有被取消。"""

    def __init__(self) -> None:
        """造一个未取消的句柄。"""
        self.cancelled = False

    def cancel(self) -> None:
        """标记为已取消。"""
        self.cancelled = True


class _PendingBackend:
    """下载替身:挂着不回调,由用例决定何时完成(测暂停/进行中这类有时序的行为)。"""

    def __init__(self) -> None:
        """建一个空的待办列表。"""
        self.pending: list[tuple[str, object, object, object]] = []
        self.urls: list[str] = []

    def download(self, url: str, *, sink, on_success, on_error, on_progress=None):  # noqa: ANN001, ANN201
        """打开 sink 并把这次下载挂起来。"""
        sink.open()
        self.urls.append(url)
        self.pending.append((url, sink, on_success, on_error))
        return _FakeHandle()

    def finish(self) -> None:
        """让最早挂起的那次下载成功完成。"""
        _url, sink, on_success, _error = self.pending.pop(0)
        sink.write(CHUNK)  # type: ignore[attr-defined]
        on_success(sink.commit())  # type: ignore[attr-defined]


class _FakeClient:
    """接口客户端替身:详情与音轨同步回调,封面不回调(这一页不需要)。"""

    def __init__(self, video: Video, backend: object) -> None:
        """记住要回调的视频与下载信道。"""
        self.video = video
        self.backend = backend
        #: 请求过 playurl 的分P cid
        self.playurl_calls: list[int] = []

    def fetch_video(self, bvid: str, *, on_success, on_error) -> object:  # noqa: ANN001
        """详情同步回调(与真实客户端命中缓存时一致)。"""
        on_success(self.video)
        return _FakeHandle()

    def fetch_audio_tracks(self, bvid: str, cid: int, *, on_success, on_error) -> object:  # noqa: ANN001
        """音轨同步回调。"""
        self.playurl_calls.append(cid)
        on_success(
            [
                AudioTrack(
                    quality_id=30280,
                    codec="mp4a.40.2",
                    bandwidth=191_900,
                    url=f"https://cdn/{cid}.m4a",
                )
            ]
        )
        return _FakeHandle()

    def fetch_cover(self, url: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """封面请求直接忽略(本文件不验证取图链路)。"""
        return None

    def search_video(self, keyword: str, *, page: int = 1, on_success, on_error) -> None:  # noqa: ANN001
        """搜索回调空结果(这一页不用搜索)。"""
        on_success(SearchResult())

    def close(self) -> None:
        """关闭是空操作。"""


class _NoRequestClient:
    """播放侧客户端替身:**任何**请求都让用例失败(证明播放没有发请求)。"""

    def fetch_video(self, bvid: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """播放链路不该补详情。"""
        raise AssertionError("播放不该请求详情接口")

    def fetch_audio_tracks(self, bvid: str, cid: int, *, on_success, on_error) -> None:  # noqa: ANN001
        """播放链路不该请求 playurl。"""
        raise AssertionError("播放不该请求 playurl 接口")

    def fetch_cover(self, url: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """封面请求忽略。"""
        return None

    def close(self) -> None:
        """关闭是空操作。"""


class _FakePlayer(QObject):
    """播放器替身:提供编排层用到的那四个信号。"""

    state_changed = Signal(bool)
    position_changed = Signal(int, int)
    track_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self) -> None:
        """建一个"什么都没加载"的播放器替身。"""
        super().__init__()
        self._is_playing = False

    @property
    def is_playing(self) -> bool:
        """是否处于播放状态。"""
        return self._is_playing

    @property
    def position_ms(self) -> int:
        """当前播放位置(本文件不关心)。"""
        return 0

    def load(self, path: Path, *, autoplay: bool = True) -> None:
        """记录播放状态(不做任何实际播放)。"""
        self._is_playing = autoplay

    def play(self) -> None:
        """继续播放。"""
        self._is_playing = True

    def pause(self) -> None:
        """暂停。"""
        self._is_playing = False

    def stop(self) -> None:
        """停止。"""
        self._is_playing = False

    def toggle(self) -> None:
        """在播放与暂停之间切换。"""
        self._is_playing = not self._is_playing

    def seek(self, position_ms: int) -> None:
        """跳转(本文件不验证播放行为)。"""

    def set_volume(self, volume: float) -> None:
        """设置音量(本文件不验证)。"""


# ====================================================================== 控件


class TestTaskDialogWidget(unittest.TestCase):
    """下载任务对话框控件本身的行为。"""

    def _dialog(self, tasks: list[DownloadTask] | None = None) -> TaskDialog:
        """建一个对话框并填上任务(默认尺寸与主窗口无关,不需要父窗口)。"""
        dialog = TaskDialog()
        self.addCleanup(dialog.deleteLater)
        if tasks:
            dialog.set_tasks(tasks)
        return dialog

    def test_rows_follow_the_order_of_tasks(self) -> None:
        """行序就是上层给的顺序(即任务创建顺序 = 队列顺序)。"""
        dialog = self._dialog([_task("BV1"), _task("BV2"), _task("BV3")])

        shown = [
            dialog.rows_layout.itemAt(index).widget()
            for index in range(dialog.rows_layout.count() - 1)
        ]
        self.assertEqual([row.bvid for row in shown], ["BV1", "BV2", "BV3"])  # type: ignore[union-attr]
        self.assertIsNotNone(dialog.row_for("BV2"))

    def test_row_shows_title_state_and_progress(self) -> None:
        """一行要能说清"这是哪个合集、什么状态、下到哪儿了"。"""
        dialog = self._dialog([_task("BV1", title="周杰伦全MV", state=TaskState.RUNNING)])
        row = dialog.row_for("BV1")
        assert row is not None

        self.assertEqual(row.title_label.full_text(), "周杰伦全MV")
        self.assertEqual(row.state_label.text(), "缓存中")
        self.assertEqual(row.progress_bar.value(), 33)  # 1 / 3
        self.assertIn("已完成 1 / 共 3 个分P", row.detail_label.full_text())
        self.assertIn("已下载 2 KB", row.detail_label.full_text())

    def test_running_row_shows_the_current_page_percent(self) -> None:
        """当前分P的下载百分比要说出来(总长未知时不显示,而不是瞎算)。"""
        dialog = TaskDialog(lambda _bvid: (512, 1024))
        self.addCleanup(dialog.deleteLater)
        dialog.set_tasks([_task("BV1", state=TaskState.RUNNING, page_index=2)])

        row = dialog.row_for("BV1")
        assert row is not None
        self.assertIn("正在缓存 P2 (50%)", row.detail_label.full_text())

    def test_failed_row_keeps_the_reason(self) -> None:
        """失败原因留在行里:弹窗关掉之后用户很可能要回头再看一眼。"""
        dialog = self._dialog(
            [
                _task(
                    "BV1",
                    state=TaskState.FAILED,
                    error="HTTP 412",
                    page_index=7,
                )
            ]
        )
        row = dialog.row_for("BV1")
        assert row is not None
        self.assertEqual(row.state_label.text(), "已暂停(出错)")
        self.assertIn("停在 P7", row.detail_label.full_text())
        self.assertIn("失败原因:HTTP 412", row.detail_label.full_text())

    def test_action_button_says_pause_and_resume(self) -> None:
        """运行中的行给"暂停",暂停中的行给"继续" —— 同一个按钮两种含义。"""
        dialog = self._dialog([_task("BV1", state=TaskState.RUNNING)])
        row = dialog.row_for("BV1")
        assert row is not None
        self.assertEqual(row.action_button.text(), "暂停")

        dialog.set_tasks([_task("BV1", state=TaskState.PAUSED)])
        self.assertEqual(row.action_button.text(), "继续")

    def test_done_row_disables_the_action_button(self) -> None:
        """已完成的任务没什么可暂停/继续的,把按钮禁掉而不是留着让人点了没反应。"""
        dialog = self._dialog(
            [_task("BV1", state=TaskState.DONE, done_pages=3)]
        )
        row = dialog.row_for("BV1")
        assert row is not None
        self.assertFalse(row.action_button.isEnabled())
        self.assertTrue(row.remove_button.isEnabled())

    def test_row_buttons_emit_the_bvid(self) -> None:
        """行内按钮把 ``bvid`` 一起发出去,上层不必再猜行号。"""
        dialog = self._dialog([_task("BV1", state=TaskState.RUNNING), _task("BV2", state=TaskState.PAUSED)])
        paused: list[str] = []
        resumed: list[str] = []
        removed: list[str] = []
        dialog.pause_requested.connect(paused.append)
        dialog.resume_requested.connect(resumed.append)
        dialog.remove_requested.connect(removed.append)

        first = dialog.row_for("BV1")
        second = dialog.row_for("BV2")
        assert first is not None and second is not None
        first.action_button.click()
        second.action_button.click()
        second.remove_button.click()

        self.assertEqual(paused, ["BV1"])
        self.assertEqual(resumed, ["BV2"])
        self.assertEqual(removed, ["BV2"])

    def test_update_task_refreshes_a_single_row(self) -> None:
        """进度回调走单行刷新(整列表重建会白扔一堆控件)。"""
        dialog = self._dialog([_task("BV1", done_pages=1)])
        row = dialog.row_for("BV1")
        assert row is not None

        dialog.update_task(_task("BV1", done_pages=2, bytes_done=4096))

        self.assertEqual(row.progress_bar.value(), 66)
        self.assertIn("已完成 2 / 共 3 个分P", row.detail_label.full_text())

    def test_update_task_adds_a_missing_row(self) -> None:
        """刷新一个列表里没有的任务时补一行(进度与列表刷新之间存在竞态)。"""
        dialog = self._dialog()

        dialog.update_task(_task("BV9"))

        self.assertIsNotNone(dialog.row_for("BV9"))

    def test_removed_task_drops_its_row(self) -> None:
        """任务从列表里消失后,它的行控件也要跟着删掉。"""
        dialog = self._dialog([_task("BV1"), _task("BV2")])

        dialog.set_tasks([_task("BV2")])

        self.assertIsNone(dialog.row_for("BV1"))
        self.assertIsNotNone(dialog.row_for("BV2"))

    def test_empty_state_replaces_the_list(self) -> None:
        """一条任务都没有时给空态页(并告诉用户去哪儿建任务)。"""
        dialog = self._dialog()

        self.assertTrue(dialog.empty_page.isVisibleTo(dialog))
        self.assertFalse(dialog.scroll.isVisibleTo(dialog))

        dialog.set_tasks([_task("BV1")])

        self.assertFalse(dialog.empty_page.isVisibleTo(dialog))
        self.assertTrue(dialog.scroll.isVisibleTo(dialog))

    def test_summary_counts_unfinished_tasks(self) -> None:
        """抬头要一眼看出"共几个任务、还有几个没完成"。"""
        dialog = self._dialog(
            [
                _task("BV1", state=TaskState.DONE, done_pages=3),
                _task("BV2", state=TaskState.RUNNING),
                _task("BV3", state=TaskState.FAILED),
            ]
        )
        self.assertEqual(dialog.summary_label.text(), "3 个任务 · 2 个未完成")
        self.assertTrue(dialog.pause_all_button.isEnabled())
        self.assertTrue(dialog.clear_button.isEnabled())

    def test_bulk_buttons_are_disabled_when_there_is_nothing_to_do(self) -> None:
        """没有未完成任务时"全部暂停/继续"禁用;全是未完成时"清空已完成"禁用。"""
        dialog = self._dialog([_task("BV1", state=TaskState.DONE, done_pages=3)])
        self.assertFalse(dialog.pause_all_button.isEnabled())
        self.assertFalse(dialog.resume_all_button.isEnabled())
        self.assertTrue(dialog.clear_button.isEnabled())

    def test_bulk_buttons_emit_signals(self) -> None:
        """三个批量按钮分别发出各自的信号(具体做什么由调度器决定)。"""
        dialog = self._dialog(
            [_task("BV1"), _task("BV2", state=TaskState.DONE, done_pages=3)]
        )
        seen: list[str] = []
        dialog.pause_all_requested.connect(lambda: seen.append("pause"))
        dialog.resume_all_requested.connect(lambda: seen.append("resume"))
        dialog.clear_finished_requested.connect(lambda: seen.append("clear"))

        dialog.pause_all_button.click()
        dialog.resume_all_button.click()
        dialog.clear_button.click()

        self.assertEqual(seen, ["pause", "resume", "clear"])

    def test_dialog_is_not_modal(self) -> None:
        """缓存任务在后台跑,把主窗口锁住只为了让用户看进度是说不通的。"""
        dialog = self._dialog()
        self.assertFalse(dialog.isModal())


# ====================================================================== 接线


class TestTaskWiring(unittest.TestCase):
    """主窗口与下载调度器的接线。"""

    def setUp(self) -> None:
        """建沙箱目录、替身与主窗口,并拦掉模态弹窗。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.cache = AudioCache(self.tmp / "cache", db=LibraryDb(self.tmp / "library.db"))
        self.video = _video(count=3)
        self.backend = _PendingBackend()
        self.client = _FakeClient(self.video, self.backend)
        self.downloader = Downloader(
            self.client,  # type: ignore[arg-type]
            self.cache,
            store=DownloadTaskStore(self.cache.db),
        )
        self.playback = PlaybackController(
            AudioResolver(_NoRequestClient(), self.cache),  # type: ignore[arg-type]
            _FakePlayer(),
        )

        self.warnings: list[str] = []
        self.questions: list[str] = []
        self.question_answer = QMessageBox.StandardButton.Yes
        self.informations: list[str] = []
        self._patch(QMessageBox, "warning", self._record_warning)
        self._patch(QMessageBox, "question", self._record_question)
        self._patch(QMessageBox, "information", self._record_information)

        self.window = MainWindow(
            client=self.client,  # type: ignore[arg-type]
            cache=self.cache,
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=ConfigStore(self.tmp / "config.json"),
            playback=self.playback,
            downloader=self.downloader,
        )
        self.addCleanup(self.window.close)

    def _patch(self, target, name: str, replacement: Callable) -> None:
        """打一个补丁并在用例结束时撤销。"""
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record_warning(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """记录一次 ``QMessageBox.warning``(第三个参数是正文)。"""
        self.warnings.append(args[2])

    def _record_question(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        """记录一次 ``QMessageBox.question`` 并按预设回答。"""
        self.questions.append(args[2])
        return self.question_answer

    def _record_information(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """记录一次 ``QMessageBox.information``。"""
        self.informations.append(args[2])

    # ---------------------------------------------------------- 右键入口

    def test_menu_label_counts_pages(self) -> None:
        """分P数已知时菜单直接写出数量 —— 点之前就该知道这一下要下 200 首。"""
        self.assertEqual(
            MainWindow._cache_all_label(self.video), "缓存全部 3 个分P"
        )
        single = _video("BV2", count=1)
        self.assertEqual(MainWindow._cache_all_label(single), "缓存到本地")
        unknown = Video(bvid="BV3", title="还没补详情")
        self.assertEqual(MainWindow._cache_all_label(unknown), "缓存到本地")

    def test_confirming_starts_the_task(self) -> None:
        """确认后任务进入队列,状态栏告诉用户已经开始。"""
        self.window._cache_all_pages(self.video)  # noqa: SLF001 - 测的就是这个入口

        self.assertEqual(len(self.questions), 1)
        self.assertIn("共 3 个分P", self.questions[0])
        task = self.downloader.task("BV1")
        assert task is not None
        self.assertIs(task.state, TaskState.RUNNING)
        self.assertIn("已开始缓存", self.window.status_label.text())

    def test_refusing_creates_nothing(self) -> None:
        """确认框点"否"就什么都不做(不建任务、不发请求)。"""
        self.question_answer = QMessageBox.StandardButton.No
        self.window._cache_all_pages(self.video)  # noqa: SLF001

        self.assertIsNone(self.downloader.task("BV1"))
        self.assertEqual(self.client.playurl_calls, [])

    def test_fully_cached_collection_is_not_re_queued(self) -> None:
        """全部分P都在本地时不建任务,只告诉用户一声。"""
        for page in self.video.pages:
            path = self.cache.path_for("BV1", page.cid, 30280, "mp4a.40.2")
            path.write_bytes(b"z" * 8)
            self.cache.remember(
                self.video,
                page,
                AudioTrack(
                    quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url=""
                ),
                path,
            )

        self.window._cache_all_pages(self.video)  # noqa: SLF001

        self.assertEqual(self.questions, [])  # 不需要确认,因为没什么可下的
        self.assertEqual(len(self.informations), 1)
        self.assertIsNone(self.downloader.task("BV1"))

    def test_duplicate_request_is_refused_with_a_hint(self) -> None:
        """同一视频已有未完成任务时不再新建,并告诉用户去哪儿看进度。"""
        self.window._cache_all_pages(self.video)  # noqa: SLF001
        self.informations.clear()

        self.window._cache_all_pages(self.video)  # noqa: SLF001

        self.assertEqual(len(self.informations), 1)
        self.assertIn("下载任务", self.informations[0])
        self.assertEqual(len(self.questions), 1)  # 第二次没有弹确认框

    def test_unknown_page_list_is_fetched_first(self) -> None:
        """分P列表还没有时先补详情(确认框里要写清共几个分P)。"""
        unknown = Video(bvid="BV1", title="只有表面信息")
        self.client.video = self.video

        self.window._cache_all_pages(unknown)  # noqa: SLF001

        self.assertEqual(len(self.questions), 1)
        self.assertIsNotNone(self.downloader.task("BV1"))

    # ---------------------------------------------------------- 缓存页两个按钮

    def test_task_button_opens_the_dialog(self) -> None:
        """"下载任务"按钮打开对话框,内容与调度器一致(实例复用,不重复开窗)。"""
        self.window._cache_all_pages(self.video)  # noqa: SLF001

        self.window.cache_page.tasks_requested.emit()

        dialog = self.window.task_dialog
        assert dialog is not None
        self.assertTrue(dialog.isVisible())
        self.assertIsNotNone(dialog.row_for("BV1"))

    def test_dialog_row_actions_reach_the_downloader(self) -> None:
        """对话框里的"暂停"要真的作用到调度器上,并回头刷新那一行。"""
        self.window._cache_all_pages(self.video)  # noqa: SLF001
        self.window.cache_page.tasks_requested.emit()
        dialog = self.window.task_dialog
        assert dialog is not None
        row = dialog.row_for("BV1")
        assert row is not None
        self.assertEqual(row.action_button.text(), "暂停")

        row.action_button.click()

        task = self.downloader.task("BV1")
        assert task is not None
        self.assertIs(task.state, TaskState.PAUSED)
        self.assertEqual(row.action_button.text(), "继续")

    def test_task_button_shows_unfinished_count(self) -> None:
        """角标显示未完成任务数:没有任务时不留数字。"""
        self.assertEqual(self.window.cache_page.tasks_button.text(), "下载任务")

        self.window._cache_all_pages(self.video)  # noqa: SLF001

        self.assertEqual(self.window.cache_page.tasks_button.text(), "下载任务 (1)")
        self.assertIn("1 个任务未完成", self.window.cache_page.tasks_button.toolTip())

    def test_open_dir_button_opens_the_cache_root(self) -> None:
        """"打开缓存目录"用系统文件管理器打开缓存根目录。"""
        opened: list[str] = []
        self._patch(
            QDesktopServices_proxy(), "openUrl", lambda url: opened.append(url.toString()) or True
        )

        self.window.cache_page.open_dir_requested.emit()

        self.assertEqual(opened, [QUrl.fromLocalFile(str(self.cache.root)).toString()])

    def test_open_dir_failure_is_reported(self) -> None:
        """文件管理器打不开时要说清目录在哪,而不是点了没反应。"""
        self._patch(QDesktopServices_proxy(), "openUrl", lambda url: False)

        self.window.cache_page.open_dir_requested.emit()

        self.assertEqual(len(self.warnings), 1)
        self.assertIn(str(self.cache.root), self.warnings[0])

    # ---------------------------------------------------------- 失败提示

    def test_failure_pops_up_a_warning(self) -> None:
        """任务失败挂起必须弹窗(任务停了整条队列,用户得知道)。"""
        self.window._cache_all_pages(self.video)  # noqa: SLF001
        self.warnings.clear()

        self.downloader.task_failed.emit("BV1", "HTTP 412")

        self.assertEqual(len(self.warnings), 1)
        self.assertIn("合集BV1", self.warnings[0])
        self.assertIn("HTTP 412", self.warnings[0])

    def test_new_cached_page_refreshes_an_open_cache_page(self) -> None:
        """有分P落盘时,"本地缓存"页要跟着出新行(否则用户以为没下下来)。"""
        self.window.sidebar.nav_buttons["cache"].click()
        self.window._cache_all_pages(self.video)  # noqa: SLF001
        self.assertEqual(self.window.cache_page.list.rowCount(), 0)

        self.backend.finish()

        self.assertEqual(self.window.cache_page.list.rowCount(), 1)

    def test_close_stops_the_running_task(self) -> None:
        """退出要收尾:在飞下载取消、任务写成暂停(不留半截文件)。"""
        self.window._cache_all_pages(self.video)  # noqa: SLF001

        self.window.close()

        task = self.downloader.task("BV1")
        assert task is not None
        self.assertIs(task.state, TaskState.PAUSED)
        self.assertEqual(sorted(self.cache.root.glob("*.part")), [])


def QDesktopServices_proxy():  # noqa: N802 - 只是为了让补丁目标一眼看出是 Qt 的类
    """返回 ``main_window`` 里那个 ``QDesktopServices`` 引用。

    主窗口是 ``from PySide6.QtGui import QDesktopServices`` 导入的,所以补丁要打在
    ``bilibili_music.ui.main_window`` 这个名字空间上(打在 ``PySide6.QtGui`` 上不会
    影响已经导入的引用)。
    """
    from bilibili_music.ui import main_window as module

    return module.QDesktopServices


if __name__ == "__main__":
    unittest.main()
