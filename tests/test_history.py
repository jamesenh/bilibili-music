"""最近播放的单元测试:记录、去重、排序、上限裁剪与容错。

不触网、不碰 Qt。落盘类用例把目录指向 ``tests/_scratch/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒;
见 ``AGENTS.md`` 第 7.1 节)。

钉住四件事:

1. **同一首只留最近一次**(B1):重复播放是把那一行提到最前并刷新时间,而不是多出一行;
2. **上限生效**:超过 ``history_limit`` 就按播放时间从旧到新丢掉;
3. **删历史不碰缓存**:两个功能同库不同表,这一条必须有用例钉住 —— 否则某次改动把
   "清空历史"写成删整个库,用户下载好的音频就没了;
4. 库坏了、行被手改坏了都只当"没有这一条",不影响播放。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.cache_index import video_from_entry  # noqa: E402
from bilibili_music.core.config import DEFAULT_HISTORY_LIMIT  # noqa: E402
from bilibili_music.core.history import (  # noqa: E402
    HistoryEntry,
    PlayHistory,
    history_entry_for,
)
from bilibili_music.core.library_db import DB_FILE_NAME, LibraryDb  # noqa: E402
from bilibili_music.core.models import Page, Video  # noqa: E402

#: 落盘类用例的临时目录根(与 test_cache_index 同一套做法,不用 tempfile)。
_SCRATCH_ROOT = Path(__file__).resolve().parent / "_scratch"

#: 造坏数据用的原始 SQL:绕过记录类直接往表里塞一行(模拟用户拿 sqlite 工具手改库)。
_INSERT_RAW = (
    "INSERT INTO play_history (bvid, cid, page_index, title, author, page_title, "
    "multipart, duration, cover_url, played_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _entry(
    bvid: str = "BV1",
    cid: int = 100,
    *,
    played_at: float = 1000.0,
    title: str = "晴天",
    author: str = "周杰伦",
    page_index: int = 1,
    multipart: bool = False,
    duration: int = 269,
) -> HistoryEntry:
    """造一条播放记录(默认参数凑成"单P视频里的一首歌")。"""
    return HistoryEntry(
        bvid=bvid,
        cid=cid,
        page_index=page_index,
        title=title,
        author=author,
        page_title=title,
        multipart=multipart,
        duration=duration,
        cover_url="https://i0.hdslb.com/a.jpg",
        played_at=played_at,
    )


class _ScratchCase(unittest.TestCase):
    """给需要落盘的用例准备一个干净目录(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建空目录并把 ``self.tmp`` 指向它。"""
        self.tmp = _SCRATCH_ROOT / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """删掉本用例的目录(清理失败不让用例变红)。"""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self) -> LibraryDb:
        """在沙箱目录里建一个本地库句柄。"""
        return LibraryDb(self.tmp / DB_FILE_NAME)

    def _history(self, *, limit: int = DEFAULT_HISTORY_LIMIT) -> PlayHistory:
        """在沙箱目录里建一个历史读写。"""
        return PlayHistory(self._db(), limit=limit)


class TestHistoryEntryFor(unittest.TestCase):
    """从播放结果造记录(纯函数 + 领域铁律)。"""

    def test_multipart_uses_the_pages_own_title_and_duration(self) -> None:
        """多P合集必须用**分P自己**的标题与时长(视频级 duration 是所有分P之和)。"""
        video = Video(
            bvid="BV1",
            title="合集",
            author="UP",
            cover_url="//i0.hdslb.com/a.jpg",
            duration=9999,
            cid=11,
            pages=[Page(1, 11, "第一首", 180), Page(2, 22, "第二首", 200)],
        )
        page2 = video.page(2)
        assert page2 is not None
        entry = history_entry_for(video, page2)
        self.assertEqual(entry.bvid, "BV1")
        self.assertEqual(entry.cid, 22)
        self.assertEqual(entry.page_index, 2)
        self.assertEqual(entry.title, "第二首")
        self.assertEqual(entry.duration, 200)
        self.assertEqual(entry.multipart, True)
        self.assertEqual(entry.author, "UP")
        self.assertEqual(entry.cover_url, "https://i0.hdslb.com/a.jpg")

    def test_single_page_video_is_not_marked_multipart(self) -> None:
        """单P视频不该被标成合集:否则副标题会多出一个没有信息量的 ``P1``。"""
        video = Video(
            bvid="BV1", title="单曲", author="UP", cid=11, pages=[Page(1, 11, "单曲", 180)]
        )
        page = video.first_page()
        assert page is not None
        self.assertFalse(history_entry_for(video, page).multipart)

    def test_played_at_defaults_to_zero(self) -> None:
        """没给时间戳时留 0,由 :meth:`PlayHistory.record` 填当前时间。"""
        video = Video(bvid="BV1", title="单曲", cid=11, pages=[Page(1, 11, "单曲", 180)])
        page = video.first_page()
        assert page is not None
        self.assertEqual(history_entry_for(video, page).played_at, 0.0)


class TestHistoryEntryToVideo(_ScratchCase):
    """历史记录也能还原成可播放目标(与缓存索引共用同一个转换函数)。"""

    def test_video_from_entry_accepts_a_history_entry(self) -> None:
        """``video_from_entry`` 认的是 :class:`PlayableEntry` 协议,不只认索引记录。

        于是"点一行就播"这条路径只有一份实现 —— 历史里点播与缓存页点播不会各写一遍。
        """
        entry = _entry(cid=222, page_index=3, title="第三首", duration=200)
        video = video_from_entry(entry)
        self.assertEqual(video.bvid, "BV1")
        self.assertEqual(video.cid, 222)
        page = video.page(3)
        assert page is not None
        self.assertEqual(page.cid, 222)
        self.assertEqual(video.duration, 200)


class TestPlayHistoryRecord(_ScratchCase):
    """写入、去重与排序。"""

    def test_record_then_entries(self) -> None:
        """记一笔再读回来,字段要原样保留。"""
        history = self._history()
        self.assertTrue(history.record(_entry()))
        entries = history.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "晴天")
        self.assertEqual(entries[0].author, "周杰伦")
        self.assertEqual(entries[0].played_at, 1000.0)

    def test_entries_are_newest_first(self) -> None:
        """最近播放的排在前面(界面直接按这个顺序显示)。"""
        history = self._history()
        history.record(_entry(cid=1, played_at=100.0, title="旧的"))
        history.record(_entry(cid=2, played_at=200.0, title="新的"))
        self.assertEqual([e.title for e in history.entries()], ["新的", "旧的"])

    def test_replaying_the_same_track_moves_it_to_the_front(self) -> None:
        """同一首再播一次:提到最前、刷新时间,**不多出一行**(B1)。"""
        history = self._history()
        history.record(_entry(cid=1, title="A", played_at=100.0))
        history.record(_entry(cid=2, title="B", played_at=200.0))
        history.record(_entry(cid=1, title="A", played_at=300.0))
        entries = history.entries()
        self.assertEqual([e.title for e in entries], ["A", "B"])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].played_at, 300.0)

    def test_same_video_different_pages_are_separate_entries(self) -> None:
        """同一视频的不同分P是**两首歌**:主键里必须含 ``cid``(领域铁律)。"""
        history = self._history()
        history.record(_entry(cid=1, title="第一首", page_index=1))
        history.record(_entry(cid=2, title="第二首", page_index=2, played_at=2000.0))
        self.assertEqual(len(history.entries()), 2)

    def test_zero_played_at_is_filled_with_now(self) -> None:
        """调用方没给时间戳时补当前时间(否则会排到列表最下面)。"""
        history = self._history()
        history.record(_entry(played_at=0.0))
        self.assertGreater(history.entries()[0].played_at, 0)

    def test_invalid_entries_are_rejected(self) -> None:
        """缺 bvid 或 cid 非正的记录既播不了也删不掉,不该写进库。"""
        history = self._history()
        self.assertFalse(history.record(_entry(bvid="")))
        self.assertFalse(history.record(_entry(cid=0)))
        self.assertEqual(history.entries(), ())

    def test_find_returns_the_entry(self) -> None:
        """按 ``(bvid, cid)`` 取记录(界面用它判断"这一行是不是正在播的那一首")。"""
        history = self._history()
        history.record(_entry(cid=7, title="找到我"))
        found = history.find("BV1", 7)
        assert found is not None
        self.assertEqual(found.title, "找到我")
        self.assertIsNone(history.find("BV1", 8))


class TestPlayHistoryTrim(_ScratchCase):
    """条数上限(默认 200,可手改 ``config.json``)。"""

    def test_limit_drops_the_oldest(self) -> None:
        """超限时丢掉**最旧**的,而不是最新的。"""
        history = self._history(limit=3)
        for index in range(5):
            history.record(_entry(cid=index + 1, title=f"第{index}首", played_at=100.0 + index))
        entries = history.entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual([e.title for e in entries], ["第4首", "第3首", "第2首"])

    def test_explicit_limit_argument_wins(self) -> None:
        """``trim`` 可以显式指定要保留的条数(测试与一次性清理会用到)。"""
        history = self._history()
        for index in range(4):
            history.record(_entry(cid=index + 1, played_at=100.0 + index))
        self.assertEqual(history.trim(2), 2)
        self.assertEqual(len(history.entries()), 2)

    def test_non_positive_limit_keeps_everything(self) -> None:
        """上限为 0 或负数表示不裁剪(构造参数允许,配置层则会把非正值收敛成默认值)。"""
        history = self._history(limit=0)
        for index in range(5):
            history.record(_entry(cid=index + 1, played_at=100.0 + index))
        self.assertEqual(len(history.entries()), 5)
        self.assertEqual(history.trim(-1), 0)

    def test_trim_reports_nothing_when_under_the_limit(self) -> None:
        """没超限时一条都不删(界面上的条数不该莫名其妙变少)。"""
        history = self._history(limit=10)
        history.record(_entry())
        self.assertEqual(history.trim(), 0)
        self.assertEqual(history.trim(10), 0)


class TestPlayHistoryRemoval(_ScratchCase):
    """删除与清空 —— **都不许碰到缓存表**。"""

    def _seed_cache_row(self) -> None:
        """往缓存表里放一行,用来证明删历史不会波及它。"""
        self._db().execute(
            "INSERT INTO cached_tracks (file_name, bvid, cid) VALUES (?, ?, ?)",
            ("a.m4a", "BV1", 100),
        )

    def test_remove_drops_only_that_entry(self) -> None:
        """删一条不影响另一条。"""
        history = self._history()
        history.record(_entry(cid=1))
        history.record(_entry(cid=2))
        self.assertTrue(history.remove("BV1", 1))
        self.assertEqual([e.cid for e in history.entries()], [2])
        self.assertFalse(history.remove("BV1", 1))

    def test_remove_keeps_the_cached_audio_record(self) -> None:
        """删一条播放记录**不碰**缓存记录:缓存文件还在磁盘上,索引也必须留着。

        两者同库不同表,这条用例是"某次改动别把两张表一起清了"的防线。
        """
        self._seed_cache_row()
        history = self._history()
        history.record(_entry())
        self.assertTrue(history.remove("BV1", 100))
        self.assertEqual(len(self._db().query("SELECT * FROM cached_tracks")), 1)

    def test_clear_empties_the_history_and_keeps_the_cache(self) -> None:
        """清空历史同理:只清 ``play_history``。"""
        self._seed_cache_row()
        history = self._history()
        history.record(_entry(cid=1))
        history.record(_entry(cid=2))
        self.assertEqual(history.clear(), 2)
        self.assertEqual(history.entries(), ())
        self.assertEqual(history.clear(), 0)
        self.assertEqual(len(self._db().query("SELECT * FROM cached_tracks")), 1)


class TestPlayHistoryTolerance(_ScratchCase):
    """库不可用、行被手改坏时的行为:只当"没有这一条"。"""

    def _broken(self) -> PlayHistory:
        """指向一个开不起来的库的历史读写。"""
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        return PlayHistory(LibraryDb(blocker / "sub" / DB_FILE_NAME))

    def test_unavailable_database_degrades_quietly(self) -> None:
        """库不可用时:写返回 False、读返回空、删返回 False、清空返回 0,全都不抛异常。"""
        history = self._broken()
        self.assertFalse(history.record(_entry()))
        self.assertEqual(history.entries(), ())
        self.assertIsNone(history.find("BV1", 100))
        self.assertFalse(history.remove("BV1", 100))
        self.assertEqual(history.clear(), 0)

    def test_bad_rows_are_skipped_one_by_one(self) -> None:
        """坏行单条跳过,其余照常读出来。"""
        db = self._db()
        db.execute(_INSERT_RAW, ("", 1, 1, "没bvid", "", "", 0, 100, "", 1.0))
        db.execute(_INSERT_RAW, ("BV1", 0, 1, "没cid", "", "", 0, 100, "", 2.0))
        db.execute(_INSERT_RAW, ("BV1", "not-a-number", 1, "坏cid", "", "", 0, 100, "", 3.0))
        history = PlayHistory(db)
        self.assertTrue(history.record(_entry(cid=5, title="好的")))
        self.assertEqual([e.title for e in history.entries()], ["好的"])

    def test_wrong_types_are_coerced(self) -> None:
        """类型不对时收敛到合法值,而不是丢弃整条记录。"""
        db = self._db()
        db.execute(
            _INSERT_RAW,
            ("BV1", "12", 0, "手改的", "", "", 1, -5, "", "oops"),
        )
        entry = PlayHistory(db).entries()[0]
        self.assertEqual(entry.cid, 12)
        self.assertEqual(entry.page_index, 1)
        self.assertEqual(entry.duration, 0)
        self.assertTrue(entry.multipart)
        self.assertEqual(entry.played_at, 0.0)


if __name__ == "__main__":
    unittest.main()
