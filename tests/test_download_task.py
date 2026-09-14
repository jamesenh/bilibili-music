"""下载任务(``core/download_task.py``)的单元测试:任务模型与它的持久化。

不触网、不碰 Qt:这一个模块只回答"任务长什么样、怎么存回来",调度与网络
在 ``tests/test_downloader.py`` 里测。

落盘类用例把目录指向 ``tests/_scratch/`` 下的自建目录,**不用**
``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒,
见 ``AGENTS.md`` 第 7.1 节)。

钉住的重点:

1. **``bvid`` 是主键**:同一个视频的任务只能有一条(重复入队是覆盖而不是多一行);
2. **不认识的状态一律当"暂停"**:库文件可以被手改,而一个"认不出来的状态"被当成
   排队中就会在下一次调度时自己跑起来;
3. **字段容错**:缺 ``bvid`` 的行丢掉、负数/坏类型收敛到合法值(库不可用时不抛异常)。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.download_task import (  # noqa: E402
    DownloadTask,
    DownloadTaskStore,
    TaskState,
    task_for_video,
    task_percent,
    task_progress_text,
)
from bilibili_music.core.library_db import LibraryDb  # noqa: E402
from bilibili_music.core.models import Page, Video  # noqa: E402

#: 落盘类用例的临时目录根(与 test_cache_index / test_history 同一套做法)。
#:
#: **带模块名**:``_scratch/<用例名>`` 在跨模块重名时会撞车 —— 本模块与
#: ``test_downloader`` 都有 ``test_clear_finished_keeps_unfinished``。Windows 上删不掉
#: 仍被打开的 ``library.db``(``rmtree(ignore_errors=True)`` 会静默失败),后跑的用例
#: 就读到前一个用例留下的任务;POSIX 允许删除打开的文件,所以 macOS 基线看不出这条。
_SCRATCH = Path(__file__).resolve().parent / "_scratch" / Path(__file__).stem


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


class TestTaskState(unittest.TestCase):
    """状态机取值、文案与解析容错。"""

    def test_state_values_are_the_stored_strings(self) -> None:
        """状态值本身就是存进库里的字符串,改名等于让老库里的任务变成未知状态。"""
        self.assertEqual(
            [state.value for state in TaskState],
            ["pending", "running", "paused", "failed", "done"],
        )

    def test_every_state_has_chinese_text(self) -> None:
        """每个状态都要有中文文案(界面直接显示它)。"""
        for state in TaskState:
            self.assertTrue(state.text)
            self.assertNotEqual(state.text, state.value)

    def test_unknown_state_falls_back_to_paused(self) -> None:
        """认不出来的状态当"暂停":一个乱码状态被当成排队中就会自己跑起来。"""
        self.assertIs(TaskState.parse("garbage"), TaskState.PAUSED)
        self.assertIs(TaskState.parse(None), TaskState.PAUSED)
        self.assertIs(TaskState.parse(""), TaskState.PAUSED)

    def test_parse_accepts_stored_strings(self) -> None:
        """库里的字符串要能原样还原成状态。"""
        for state in TaskState:
            self.assertIs(TaskState.parse(state.value), state)

    def test_parse_accepts_a_default(self) -> None:
        """兜底状态可以指定(调用方想用别的默认值时不必自己写判断)。"""
        self.assertIs(TaskState.parse("x", TaskState.PENDING), TaskState.PENDING)

    def test_active_and_finished_are_disjoint(self) -> None:
        """"会被调度"与"已结束"互斥,且失败**不在**活动状态里。"""
        self.assertTrue(TaskState.PENDING.is_active)
        self.assertTrue(TaskState.RUNNING.is_active)
        # 失败挂起等用户决定,自动把它捡起来重试就是在风控里反复撞墙
        self.assertFalse(TaskState.FAILED.is_active)
        self.assertFalse(TaskState.PAUSED.is_active)
        self.assertTrue(TaskState.DONE.is_finished)
        for state in TaskState:
            self.assertFalse(state.is_active and state.is_finished)


class TestTaskProgress(unittest.TestCase):
    """进度文本与百分比(纯函数)。"""

    def test_progress_text_counts_pages(self) -> None:
        """进度按分P个数表达:一个 200P 合集在行里放不下分P明细。"""
        task = DownloadTask(bvid="BV1", total_pages=200, done_pages=3)
        self.assertEqual(task_progress_text(task), "已完成 3 / 共 200 个分P")
        self.assertEqual(task.progress_text, task_progress_text(task))

    def test_progress_text_without_pages_yet(self) -> None:
        """还没取到详情(``total_pages`` 为 0)时如实说"还没有取到分P列表"。"""
        self.assertEqual(DownloadTask(bvid="BV1").progress_text, "还没有取到分P列表")

    def test_progress_text_when_everything_is_cached(self) -> None:
        """"一首没下"与"下完了"要给不同的说法,否则用户以为还在跑。"""
        task = DownloadTask(bvid="BV1", total_pages=3, done_pages=3)
        self.assertEqual(task.progress_text, "已完成全部 3 个分P")

    def test_done_task_with_missing_pages_says_so(self) -> None:
        """任务结束但少下了几P(那些当时正在播放、被跳过)必须写出来,不能假装下全了。"""
        task = DownloadTask(
            bvid="BV1", total_pages=3, done_pages=2, state=TaskState.DONE
        )
        self.assertEqual(task.progress_text, "已完成 2 / 共 3 个分P(还有 1 个未缓存)")

    def test_percent_uses_page_counts(self) -> None:
        """百分比按分P数算:按字节算会卡在 99%(最后几首是长曲)。"""
        self.assertEqual(task_percent(DownloadTask(bvid="BV1", total_pages=200, done_pages=1)), 0)
        self.assertEqual(task_percent(DownloadTask(bvid="BV1", total_pages=200, done_pages=50)), 25)
        self.assertEqual(task_percent(DownloadTask(bvid="BV1", total_pages=4, done_pages=1)), 25)

    def test_percent_is_clamped_and_safe(self) -> None:
        """没有分P数据时 0;计数越界(手改过库)时也不许超出 0~100。"""
        self.assertEqual(task_percent(DownloadTask(bvid="BV1")), 0)
        self.assertEqual(task_percent(DownloadTask(bvid="BV1", total_pages=3, done_pages=9)), 100)
        self.assertEqual(task_percent(DownloadTask(bvid="BV1", total_pages=3, done_pages=-5)), 0)

    def test_display_title_falls_back_to_bvid(self) -> None:
        """标题为空时退回 BV 号:列表里出现一行空白没人看得懂。"""
        self.assertEqual(DownloadTask(bvid="BV1").display_title, "BV1")
        self.assertEqual(DownloadTask(bvid="BV1", title="晴天").display_title, "晴天")


class TestTaskForVideo(unittest.TestCase):
    """从 ``Video`` 造任务的纯函数。"""

    def test_counts_pages_and_copies_display_fields(self) -> None:
        """分P总数与展示字段都取自视频对象(界面因此不必再回查 client)。"""
        task = task_for_video(_video(count=200), now=1000.0)
        self.assertEqual(task.bvid, "BV1")
        self.assertEqual(task.total_pages, 200)
        self.assertEqual(task.title, "合集BV1")
        self.assertEqual(task.author, "某UP")
        self.assertEqual(task.cover_url, "https://i0.hdslb.com/a.jpg")  # 已升级 https
        self.assertEqual(task.created_at, 1000.0)
        self.assertIs(task.state, TaskState.PENDING)

    def test_new_task_has_no_progress_yet(self) -> None:
        """新任务的分P计数从 0 起(真正的"已完成数"在下发时按缓存索引现算)。"""
        task = task_for_video(_video())
        self.assertEqual(task.done_pages, 0)
        self.assertEqual(task.bytes_done, 0)
        self.assertEqual(task.page_index, 0)


class _ScratchCase(unittest.TestCase):
    """给需要落盘的用例准备一个干净目录(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建空目录并把 ``self.tmp`` 指向它。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """删掉本用例的目录(清理失败不让用例变红)。"""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self) -> DownloadTaskStore:
        """在沙箱目录里建一个任务存储。"""
        return DownloadTaskStore(LibraryDb(self.tmp / "library.db"))


class TestDownloadTaskStore(_ScratchCase):
    """任务的读写。"""

    def test_table_is_created_on_first_use(self) -> None:
        """建表是幂等的,第一次查询就会把 ``download_tasks`` 建出来。"""
        db = LibraryDb(self.tmp / "library.db")
        names = {
            row["name"]
            for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertIn("download_tasks", names)

    def test_round_trip(self) -> None:
        """写进去再读出来,每个字段都要对得上。"""
        store = self._store()
        task = DownloadTask(
            bvid="BV1",
            title="合集",
            author="UP",
            cover_url="https://i0.hdslb.com/a.jpg",
            total_pages=200,
            done_pages=3,
            bytes_done=4096,
            page_index=4,
            state=TaskState.FAILED,
            error="HTTP 412",
            created_at=100.0,
            updated_at=200.0,
        )
        self.assertTrue(store.upsert(task))
        found = store.find("BV1")
        assert found is not None
        self.assertEqual(found.title, "合集")
        self.assertEqual(found.author, "UP")
        self.assertEqual(found.cover_url, "https://i0.hdslb.com/a.jpg")
        self.assertEqual(found.total_pages, 200)
        self.assertEqual(found.done_pages, 3)
        self.assertEqual(found.bytes_done, 4096)
        self.assertEqual(found.page_index, 4)
        self.assertIs(found.state, TaskState.FAILED)
        self.assertEqual(found.error, "HTTP 412")
        self.assertEqual(found.created_at, 100.0)

    def test_bvid_is_the_primary_key(self) -> None:
        """同一个视频只能有一条任务:重复写是覆盖,不是多出一行。"""
        store = self._store()
        store.upsert(DownloadTask(bvid="BV1", title="旧", total_pages=10))
        store.upsert(DownloadTask(bvid="BV1", title="新", total_pages=200))
        entries = store.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "新")
        self.assertEqual(entries[0].total_pages, 200)

    def test_entries_follow_creation_order(self) -> None:
        """队列顺序就是创建顺序(先来的先下)。"""
        store = self._store()
        for index, bvid in enumerate(("BV1", "BV2", "BV3"), start=1):
            store.upsert(DownloadTask(bvid=bvid, created_at=float(index) * 10))
        self.assertEqual([task.bvid for task in store.entries()], ["BV1", "BV2", "BV3"])

    def test_missing_timestamps_are_filled(self) -> None:
        """调用方不传时间戳时自动补当前时间,免得库里出现 1970 年的任务。"""
        store = self._store()
        task = DownloadTask(bvid="BV1")
        store.upsert(task)
        self.assertGreater(task.created_at, 0)
        self.assertGreaterEqual(task.updated_at, task.created_at)

    def test_remove_deletes_only_that_task(self) -> None:
        """移除一条任务不动别的任务(更不动音频文件)。"""
        store = self._store()
        store.upsert(DownloadTask(bvid="BV1"))
        store.upsert(DownloadTask(bvid="BV2"))
        self.assertTrue(store.remove("BV1"))
        self.assertEqual([task.bvid for task in store.entries()], ["BV2"])
        self.assertFalse(store.remove("BV1"))  # 已经没有这一条了

    def test_clear_finished_keeps_unfinished(self) -> None:
        """只清已完成:暂停与失败的任务还留着用户的意图,清掉等于替他放弃。"""
        store = self._store()
        store.upsert(DownloadTask(bvid="BV1", state=TaskState.DONE))
        store.upsert(DownloadTask(bvid="BV2", state=TaskState.PAUSED))
        store.upsert(DownloadTask(bvid="BV3", state=TaskState.FAILED, error="x"))
        store.upsert(DownloadTask(bvid="BV4", state=TaskState.PENDING))
        self.assertEqual(store.clear_finished(), 1)
        self.assertEqual(
            [task.bvid for task in store.entries()], ["BV2", "BV3", "BV4"]
        )

    def test_clear_removes_everything(self) -> None:
        """全清(测试与"重置"用)。"""
        store = self._store()
        store.upsert(DownloadTask(bvid="BV1"))
        store.upsert(DownloadTask(bvid="BV2", state=TaskState.DONE))
        self.assertEqual(store.clear(), 2)
        self.assertEqual(store.entries(), ())

    def test_empty_bvid_is_ignored(self) -> None:
        """没有 ``bvid`` 的任务在库里没有身份,直接拒绝(否则会写出一行谁也删不掉的数据)。"""
        store = self._store()
        self.assertFalse(store.upsert(DownloadTask(bvid="")))
        self.assertEqual(store.entries(), ())

    def test_unknown_state_string_is_read_as_paused(self) -> None:
        """手改过的库里可能有任何状态串;认不出来当暂停,绝不自动跑起来。"""
        db = LibraryDb(self.tmp / "library.db")
        db.execute(
            "INSERT INTO download_tasks (bvid, state) VALUES (?, ?)", ("BV1", "weird")
        )
        task = DownloadTaskStore(db).find("BV1")
        assert task is not None
        self.assertIs(task.state, TaskState.PAUSED)

    def test_row_without_bvid_is_dropped(self) -> None:
        """缺 ``bvid`` 的行丢掉:它既恢复不了也删不掉,留在列表里只会是个死行。"""
        db = LibraryDb(self.tmp / "library.db")
        db.execute("INSERT INTO download_tasks (bvid, title) VALUES (?, ?)", ("", "空"))
        db.execute("INSERT INTO download_tasks (bvid, title) VALUES (?, ?)", ("BV1", "好"))
        self.assertEqual([task.bvid for task in DownloadTaskStore(db).entries()], ["BV1"])

    def test_broken_values_are_clamped(self) -> None:
        """坏数据收敛到合法值而不是抛异常(库文件是可被手改的)。"""
        db = LibraryDb(self.tmp / "library.db")
        db.execute(
            "INSERT INTO download_tasks (bvid, total_pages, done_pages, bytes_done, page_index) "
            "VALUES (?, ?, ?, ?, ?)",
            ("BV1", "oops", -5, "x", "-1"),
        )
        task = DownloadTaskStore(db).find("BV1")
        assert task is not None
        self.assertEqual(task.total_pages, 0)
        self.assertEqual(task.done_pages, 0)
        self.assertEqual(task.bytes_done, 0)
        self.assertEqual(task.page_index, 0)

    def test_unavailable_database_degrades_silently(self) -> None:
        """库开不起来时写读都只是"没有数据",绝不抛异常到界面上。"""
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        store = DownloadTaskStore(LibraryDb(blocker / "sub" / "library.db"))
        self.assertFalse(store.upsert(DownloadTask(bvid="BV1")))
        self.assertEqual(store.entries(), ())
        self.assertIsNone(store.find("BV1"))
        self.assertEqual(store.clear_finished(), 0)
        self.assertFalse(store.remove("BV1"))

    def test_with_state_returns_a_copy_and_clears_the_error(self) -> None:
        """状态变化走副本(并刷新时间戳),转成非失败状态时失败原因要被清掉。"""
        task = DownloadTask(bvid="BV1", state=TaskState.FAILED, error="boom", updated_at=1.0)
        fixed = task.with_state(TaskState.PENDING)
        self.assertIs(task.state, TaskState.FAILED)  # 原对象不动
        self.assertIs(fixed.state, TaskState.PENDING)
        self.assertEqual(fixed.error, "")
        self.assertGreater(fixed.updated_at, task.updated_at)


if __name__ == "__main__":
    unittest.main()
