"""批量缓存调度器(``audio/downloader.py``)的单元测试。

不触网、不出声:客户端与信道全部是替身,音频缓存与任务库落在 ``tests/_scratch/`` 里的
沙箱目录(不用 ``tempfile``,见 ``AGENTS.md`` 第 7.1 节)。

替身有两种,分别用来逼出两类行为:

* :class:`_ImmediateBackend` 同步完成下载 —— 测"一路下完"的整条链路(含写缓存索引);
* :class:`_PendingBackend` 挂着不回调,由用例决定何时完成/失败 —— 测暂停、取消、
  "同一时刻只有一个下载"这类**有时序**的行为。同步替身是测不出这些的:
  回调还没返回就已经收尾了,"并发"根本无从发生。

钉住的重点:分P串行、跳过已缓存、失败挂起整条队列、暂停丢半截文件(不做字节续传)、
重启后一律暂停、正在播放的那一P推迟一轮再试。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)。
# 这里只用 QObject/信号,但建一个 QApplication 与其他界面用例保持同一套环境。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from bilibili_music.audio.downloader import Downloader  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.download_task import (  # noqa: E402
    DownloadTask,
    DownloadTaskStore,
    TaskState,
)
from bilibili_music.core.errors import NetworkError  # noqa: E402
from bilibili_music.core.library_db import LibraryDb  # noqa: E402
from bilibili_music.core.models import AudioTrack, Page, Video  # noqa: E402

#: 落盘类用例的临时目录根(与 test_cache_index / test_history 同一套做法)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"

#: 替身下载下来的一块数据(每个分P都是这么长)。
CHUNK = b"x" * 1024


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


def _track(cid: int) -> AudioTrack:
    """造一条音轨(``url`` 里带着 ``cid``,替身信道据此区分分P)。"""
    return AudioTrack(
        quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url=f"https://cdn/{cid}.m4a"
    )


# ====================================================================== 替身


class _FakeHandle:
    """请求句柄替身:只记下有没有被取消。"""

    def __init__(self) -> None:
        """造一个未取消的句柄。"""
        self.cancelled = False

    def cancel(self) -> None:
        """标记为已取消(替身不会再回调)。"""
        self.cancelled = True


class _ImmediateBackend:
    """下载替身:同步把数据写进 sink 并回调成功。"""

    def __init__(self, *, chunk: bytes = CHUNK) -> None:
        """记录分块大小与调用痕迹。"""
        self.chunk = chunk
        #: 下载过的 url(顺序即真实发生顺序)
        self.urls: list[str] = []
        #: 进度回调收到过的 ``(已收, 总长)``
        self.progress: list[tuple[int, int]] = []

    def download(self, url: str, *, sink, on_success, on_error, on_progress=None) -> Any:  # noqa: ANN001
        """落盘一块数据并立刻报成功。"""
        self.urls.append(url)
        try:
            sink.open()
            sink.write(self.chunk)
            if on_progress is not None:
                self.progress.append((len(self.chunk), len(self.chunk)))
                on_progress(len(self.chunk), len(self.chunk))
            on_success(sink.commit())
        except Exception as exc:  # noqa: BLE001 - 与真后端的错误出口保持一致
            sink.abort()
            on_error(exc)
        return _FakeHandle()


class _PendingBackend:
    """下载替身:挂着不回调,由用例决定何时完成/失败。"""

    def __init__(self) -> None:
        """建一个空的待办列表。"""
        #: 每次下载一项 ``(url, sink, on_success, on_error, on_progress, handle)``
        self.pending: list[tuple[str, Any, Any, Any, Any, _FakeHandle]] = []

    @property
    def urls(self) -> list[str]:
        """已经发起过的下载地址。"""
        return [item[0] for item in self.pending]

    def download(self, url: str, *, sink, on_success, on_error, on_progress=None) -> Any:  # noqa: ANN001
        """打开 sink 并把这次下载挂起来。"""
        sink.open()
        handle = _FakeHandle()
        self.pending.append((url, sink, on_success, on_error, on_progress, handle))
        return handle

    def emit_progress(self, done: int, total: int) -> None:
        """对**最近一次**挂起的下载触发一次进度回调。"""
        _url, _sink, _ok, _err, on_progress, _handle = self.pending[-1]
        if on_progress is not None:
            on_progress(done, total)

    def finish(self, chunk: bytes = CHUNK) -> None:
        """让最早挂起的那次下载成功完成。"""
        _url, sink, on_success, _err, _progress, _handle = self.pending.pop(0)
        sink.write(chunk)
        on_success(sink.commit())

    def fail(self, message: str = "下载失败") -> None:
        """让最早挂起的那次下载失败。"""
        _url, sink, _ok, on_error, _progress, _handle = self.pending.pop(0)
        sink.abort()
        on_error(NetworkError(message))


class _FakeClient:
    """接口客户端替身:详情与音轨都直接回调,可指定某个分P的 playurl 失败。"""

    def __init__(
        self,
        video: Video,
        backend: Any,
        *,
        fail_cid: int | None = None,
        fail_error: Exception | None = None,
    ) -> None:
        """记录要回调的视频、信道与"哪个分P的 playurl 要失败"。"""
        self.video = video
        self.backend = backend
        self.fail_cid = fail_cid
        self.fail_error = fail_error or NetworkError("HTTP 412")
        #: 请求过 playurl 的分P cid(顺序即真实发生顺序)
        self.playurl_calls: list[int] = []
        #: 请求过详情的 bvid
        self.detail_calls: list[str] = []

    def fetch_video(self, bvid: str, *, on_success, on_error) -> Any:  # noqa: ANN001
        """详情同步回调(与真实客户端"命中缓存"时的行为一致)。"""
        self.detail_calls.append(bvid)
        on_success(self.video)
        return _FakeHandle()

    def fetch_audio_tracks(self, bvid: str, cid: int, *, on_success, on_error) -> Any:  # noqa: ANN001
        """音轨同步回调;命中的 ``fail_cid`` 走失败路径。"""
        self.playurl_calls.append(cid)
        if self.fail_cid is not None and cid == self.fail_cid:
            on_error(self.fail_error)
        else:
            on_success([_track(cid)])
        return _FakeHandle()


# ====================================================================== 基类


class _Case(unittest.TestCase):
    """沙箱缓存 + 替身客户端/信道的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建沙箱目录、缓存、任务库与下载器。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.cache = AudioCache(self.tmp / "cache", db=LibraryDb(self.tmp / "library.db"))
        self.store = DownloadTaskStore(self.cache.db)
        #: 用例可以替换它(比如让"正在播放"返回某一个分P)
        self.playing: tuple[str, int] | None = None
        #: 本用例造过的下载器(退出时统一收尾,免得半截文件句柄泄漏到下一个用例)
        self._downloaders: list[Downloader] = []

    def tearDown(self) -> None:
        """把还在跑的下载收尾:真实应用退出时也会这么做(取消 + 清掉 ``.part``)。"""
        for downloader in self._downloaders:
            downloader.shutdown()

    def _downloader(self, client: Any, *, playing: bool = True) -> Downloader:
        """造一个下载器(``playing`` 为假时不注入"正在播放"回调)。"""
        downloader = Downloader(
            client,
            self.cache,
            store=self.store,
            now_playing=(lambda: self.playing) if playing else None,
        )
        self._downloaders.append(downloader)
        return downloader

    def _files(self) -> list[Path]:
        """缓存目录里现存的音频文件。"""
        return sorted(self.cache.root.glob("*.m4a"))

    def _parts(self) -> list[Path]:
        """缓存目录里残留的半截文件(暂停/失败后必须为空)。"""
        return sorted(self.cache.root.glob("*.part"))

    def _seed_cached(self, video: Video, index: int, *, quality_id: int = 30280) -> Path:
        """预先缓存某个分P(文件 + 索引都齐,``pick_best_cached`` 才会命中)。"""
        page = video.page(index)
        assert page is not None
        path = self.cache.path_for(video.bvid, page.cid, quality_id, "mp4a.40.2")
        path.write_bytes(b"z" * 10)
        self.cache.remember(video, page, _track(page.cid), path)
        return path


# ====================================================================== 正常下载


class TestDownloadRun(_Case):
    """一路下完的整条链路。"""

    def test_downloads_every_page_in_order(self) -> None:
        """按分P顺序串行下完,文件与缓存索引都要落地。"""
        video = _video(count=3)
        backend = _ImmediateBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)

        self.assertTrue(downloader.enqueue(video))

        self.assertEqual(client.playurl_calls, [100, 200, 300])
        self.assertEqual(backend.urls, ["https://cdn/100.m4a", "https://cdn/200.m4a", "https://cdn/300.m4a"])
        self.assertEqual(len(self._files()), 3)
        self.assertEqual(len(self.cache.index.entries()), 3)
        task = downloader.task("BV1")
        assert task is not None
        self.assertIs(task.state, TaskState.DONE)
        self.assertEqual(task.done_pages, 3)
        self.assertEqual(task.bytes_done, 3 * len(CHUNK))

    def test_task_progress_is_persisted(self) -> None:
        """进度每完成一个分P就落库,中途被杀也不会从零开始。"""
        video = _video(count=3)
        backend = _ImmediateBackend()
        downloader = self._downloader(_FakeClient(video, backend))
        downloader.enqueue(video)

        stored = self.store.find("BV1")
        assert stored is not None
        self.assertIs(stored.state, TaskState.DONE)
        self.assertEqual(stored.done_pages, 3)
        self.assertEqual(stored.bytes_done, 3 * len(CHUNK))

    def test_only_one_download_is_in_flight_at_a_time(self) -> None:
        """串行是硬要求(用户明确要"避免风控/限流"):前一P没完就不发下一个请求。"""
        video = _video(count=3)
        backend = _PendingBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)
        downloader.enqueue(video)

        # 卡在第 1P 的下载上:只发过一次 playurl,只挂了一次下载
        self.assertEqual(client.playurl_calls, [100])
        self.assertEqual(len(backend.pending), 1)

        backend.finish()
        self.assertEqual(client.playurl_calls, [100, 200])
        self.assertEqual(len(backend.pending), 1)

    def test_already_cached_pages_are_skipped(self) -> None:
        """已经在本地的分P不重复下载,但仍计入"已完成"。"""
        video = _video(count=3)
        self._seed_cached(video, 2)
        backend = _ImmediateBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)

        downloader.enqueue(video)

        self.assertEqual(client.playurl_calls, [100, 300])
        task = downloader.task("BV1")
        assert task is not None
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.done_pages, 3)
        # 预缓存那 10 字节也要算进"已下载"总量里(它就是本地占用的那一部分)
        self.assertEqual(task.bytes_done, 2 * len(CHUNK) + 10)

    def test_page_progress_is_readable_while_downloading(self) -> None:
        """下载进行中时能读到当前分P的字节进度(界面靠它显示百分比)。"""
        video = _video(count=2)
        backend = _PendingBackend()
        downloader = self._downloader(_FakeClient(video, backend))
        seen: list[str] = []
        downloader.task_updated.connect(seen.append)
        downloader.enqueue(video)

        self.assertEqual(downloader.page_progress("BV1"), (0, 0))
        self.assertEqual(downloader.running(), "BV1")

        backend.emit_progress(512, 1024)

        self.assertEqual(downloader.page_progress("BV1"), (512, 1024))
        self.assertIn("BV1", seen)

    def test_enqueue_rejects_duplicates_and_empty_page_lists(self) -> None:
        """同一视频不能有两个未完成任务;没有分P列表的入队请求直接拒绝。"""
        video = _video(count=2)
        downloader = self._downloader(_FakeClient(video, _PendingBackend()))

        self.assertTrue(downloader.enqueue(video))
        self.assertTrue(downloader.blocks_new_task("BV1"))
        self.assertFalse(downloader.enqueue(video))
        self.assertFalse(downloader.enqueue(Video(bvid="BV9", title="没有分P")))

    def test_enqueue_allows_the_same_video_after_it_finished(self) -> None:
        """已完成的旧任务不拦:合集可能更新了分P,重新右键一次正好补齐。"""
        video = _video(count=1)
        downloader = self._downloader(_FakeClient(video, _ImmediateBackend()))
        downloader.enqueue(video)

        self.assertFalse(downloader.blocks_new_task("BV1"))
        self.assertTrue(downloader.enqueue(video))


# ====================================================================== 失败与暂停


class TestDownloadFailure(_Case):
    """失败即挂起整条队列。"""

    def test_failed_page_pauses_the_task_before_the_queue(self) -> None:
        """某个分P失败:当前任务挂起,排队中的任务也一并停住(不继续撞风控)。"""
        first = _video("BV1", count=3)
        second = _video("BV2", count=3)
        backend = _PendingBackend()
        client = _FakeClient(first, backend, fail_cid=200)
        downloader = self._downloader(client)
        failures: list[tuple[str, str]] = []
        downloader.task_failed.connect(lambda bvid, msg: failures.append((bvid, msg)))

        downloader.enqueue(first)
        downloader.enqueue(second)
        self.assertIs(downloader.task("BV2").state, TaskState.PENDING)  # type: ignore[union-attr]

        backend.finish()  # 第 1P 成功,第 2P 的 playurl 失败

        task = downloader.task("BV1")
        assert task is not None
        self.assertIs(task.state, TaskState.FAILED)
        self.assertEqual(task.page_index, 2)
        self.assertIn("412", task.error)
        self.assertIs(downloader.task("BV2").state, TaskState.PAUSED)  # type: ignore[union-attr]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "BV1")
        # 第二个任务一个请求都没发出去
        self.assertEqual(client.playurl_calls, [100, 200])
        self.assertEqual(downloader.running(), None)
        # 失败的那一P留下的半截文件要清掉
        self.assertEqual(self._parts(), [])

    def test_resume_after_failure_retries_the_failed_page(self) -> None:
        """继续之后从**失败的那一P**重来(不做字节级续传)。"""
        video = _video("BV1", count=2)
        backend = _PendingBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)
        downloader.enqueue(video)
        backend.fail("boom")

        self.assertIs(downloader.task("BV1").state, TaskState.FAILED)  # type: ignore[union-attr]
        self.assertTrue(downloader.resume("BV1"))
        self.assertEqual(client.playurl_calls, [100, 100])
        self.assertIs(downloader.task("BV1").state, TaskState.RUNNING)  # type: ignore[union-attr]


class TestDownloadPause(_Case):
    """暂停、取消与恢复。"""

    def test_pause_aborts_the_partial_file(self) -> None:
        """暂停丢掉半截文件(不做字节级续传),并把任务标成暂停。"""
        video = _video("BV1", count=3)
        backend = _PendingBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)
        downloader.enqueue(video)
        self.assertTrue(backend.urls)  # 已经在下第一个分P

        self.assertTrue(downloader.pause("BV1"))

        self.assertIs(downloader.task("BV1").state, TaskState.PAUSED)  # type: ignore[union-attr]
        self.assertEqual(self._parts(), [])
        self.assertEqual(downloader.running(), None)
        stored = self.store.find("BV1")
        assert stored is not None
        self.assertIs(stored.state, TaskState.PAUSED)

    def test_resume_restarts_the_current_page_from_scratch(self) -> None:
        """恢复时当前分P从头下:没有 ``Range`` 就没有"从第 400KB 继续"这回事。"""
        video = _video("BV1", count=2)
        backend = _PendingBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)
        downloader.enqueue(video)
        downloader.pause("BV1")

        self.assertTrue(downloader.resume("BV1"))

        self.assertEqual(client.playurl_calls, [100, 100])

    def test_pause_lets_the_queue_continue(self) -> None:
        """暂停一个任务不影响队列:后面排着的任务照常开跑。"""
        first = _video("BV1", count=2)
        second = _video("BV2", count=2)
        backend = _PendingBackend()
        client = _FakeClient(first, backend)
        downloader = self._downloader(client)
        downloader.enqueue(first)
        client.video = second
        downloader.enqueue(second)

        downloader.pause("BV1")

        self.assertIs(downloader.task("BV1").state, TaskState.PAUSED)  # type: ignore[union-attr]
        self.assertIs(downloader.task("BV2").state, TaskState.RUNNING)  # type: ignore[union-attr]
        self.assertEqual(downloader.running(), "BV2")

    def test_pause_all_and_resume_all(self) -> None:
        """"全部暂停"停掉整条队列,"全部继续"把它们重新排上。"""
        videos = [_video(bvid, count=2) for bvid in ("BV1", "BV2", "BV3")]
        backend = _PendingBackend()
        client = _FakeClient(videos[0], backend)
        downloader = self._downloader(client)
        for video in videos:
            client.video = video  # 替身换目标(真实场景里每个 bvid 各有一份详情)
            downloader.enqueue(video)

        self.assertEqual(downloader.pause_all(), 3)
        self.assertEqual(downloader.active_count(), 3)
        self.assertEqual(downloader.running(), None)

        self.assertEqual(downloader.resume_all(), 3)
        self.assertEqual(downloader.running(), "BV1")

    def test_remove_keeps_the_downloaded_files(self) -> None:
        """移除任务只去掉记录,已经下好的音频要留在磁盘上。"""
        video = _video("BV1", count=2)
        backend = _PendingBackend()
        downloader = self._downloader(_FakeClient(video, backend))
        downloader.enqueue(video)
        backend.finish()

        self.assertTrue(downloader.remove("BV1"))

        self.assertIsNone(downloader.task("BV1"))
        self.assertIsNone(self.store.find("BV1"))
        self.assertEqual(len(self._files()), 1)
        self.assertEqual(self._parts(), [])  # 在飞的那个被取消并清掉了

    def test_clear_finished_keeps_unfinished(self) -> None:
        """"清空已完成"只清已完成的任务,暂停与失败的都还在。"""
        done_video = _video("BV1", count=1)
        pending_video = _video("BV2", count=2)
        backend = _PendingBackend()
        client = _FakeClient(done_video, backend)
        downloader = self._downloader(client)
        downloader.enqueue(done_video)
        backend.finish()
        client.video = pending_video
        downloader.enqueue(pending_video)

        self.assertEqual(downloader.clear_finished(), 1)

        self.assertIsNone(downloader.task("BV1"))
        self.assertIsNotNone(downloader.task("BV2"))
        self.assertEqual(downloader.active_count(), 1)

    def test_shutdown_pauses_the_running_task_and_cleans_up(self) -> None:
        """退出时取消在飞下载,并把库里的状态改成"暂停"(而不是留着"缓存中")。"""
        video = _video("BV1", count=2)
        backend = _PendingBackend()
        downloader = self._downloader(_FakeClient(video, backend))
        downloader.enqueue(video)

        downloader.shutdown()

        stored = self.store.find("BV1")
        assert stored is not None
        self.assertIs(stored.state, TaskState.PAUSED)
        self.assertEqual(self._parts(), [])


# ====================================================================== 重启与播放


class TestDownloadRestore(_Case):
    """跨重启恢复。"""

    def test_unfinished_tasks_come_back_paused(self) -> None:
        """重启后未完成的任务一律是暂停的:不会一开机就自己发几百个请求。"""
        self.store.upsert(DownloadTask(bvid="BV1", state=TaskState.RUNNING, total_pages=3))
        self.store.upsert(DownloadTask(bvid="BV2", state=TaskState.PENDING, total_pages=2))
        self.store.upsert(DownloadTask(bvid="BV3", state=TaskState.DONE, total_pages=1))
        backend = _ImmediateBackend()
        client = _FakeClient(_video("BV1"), backend)

        downloader = self._downloader(client)

        self.assertIs(downloader.task("BV1").state, TaskState.PAUSED)  # type: ignore[union-attr]
        self.assertIs(downloader.task("BV2").state, TaskState.PAUSED)  # type: ignore[union-attr]
        self.assertIs(downloader.task("BV3").state, TaskState.DONE)  # type: ignore[union-attr]
        self.assertEqual(downloader.running(), None)
        self.assertEqual(client.playurl_calls, [])
        self.assertEqual(backend.urls, [])
        # 库里那个"缓存中"也要被就地改正,否则用户翻库时会被误导
        stored = self.store.find("BV1")
        assert stored is not None
        self.assertIs(stored.state, TaskState.PAUSED)

    def test_resume_after_restart_skips_what_is_already_local(self) -> None:
        """恢复时"还差哪几P"由缓存索引现算:已在本地的分P直接跳过。"""
        video = _video("BV1", count=3)
        self._seed_cached(video, 1)
        self.store.upsert(DownloadTask(bvid="BV1", state=TaskState.PAUSED, total_pages=3))
        backend = _ImmediateBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)

        self.assertTrue(downloader.resume("BV1"))

        self.assertEqual(client.playurl_calls, [200, 300])
        task = downloader.task("BV1")
        assert task is not None
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.done_pages, 3)


class TestPlayingPageIsDeferred(_Case):
    """与播放并行时的那一处撞车点。"""

    def test_page_being_played_is_deferred_and_retried(self) -> None:
        """正在播放的那一P第一轮跳过,排到末尾再试一次(那时播放多半已经走了)。"""
        video = _video("BV1", count=2)
        backend = _ImmediateBackend()
        client = _FakeClient(video, backend)
        downloader = self._downloader(client)
        calls: list[tuple[str, int]] = []

        def now_playing() -> tuple[str, int] | None:
            """第一次问"在播哪一P"时报告第 1P,之后就不在播了。"""
            calls.append(("ask", len(calls)))
            return ("BV1", 100) if len(calls) == 1 else None

        downloader._now_playing = now_playing  # noqa: SLF001 - 只替换一个回调
        downloader.enqueue(video)

        # 第 1P 被推迟,先下第 2P,末尾再回头补第 1P
        self.assertEqual(client.playurl_calls, [200, 100])
        task = downloader.task("BV1")
        assert task is not None
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.done_pages, 2)

    def test_page_still_playing_at_the_end_is_left_uncached(self) -> None:
        """一直播着那一P时任务照常结束,但如实说明还有几个没缓存。"""
        video = _video("BV1", count=2)
        backend = _ImmediateBackend()
        downloader = self._downloader(_FakeClient(video, backend))
        self.playing = ("BV1", 100)

        downloader.enqueue(video)

        task = downloader.task("BV1")
        assert task is not None
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.done_pages, 1)
        self.assertEqual(task.progress_text, "已完成 1 / 共 2 个分P(还有 1 个未缓存)")


if __name__ == "__main__":
    unittest.main()
