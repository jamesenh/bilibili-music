"""搜索历史的单元测试:记录、去重、排序、过滤、上限裁剪与容错。

不触网、不碰 Qt。落盘类用例把目录指向 ``tests/_scratch/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒;
见 ``AGENTS.md`` 第 7.1 节)。

钉住五件事:

1. **同一个词只留最近一次**:重搜是把那一行提到最前,而不是多出一行;
2. **"最近搜索的在最前"**:下拉框的顺序就是这个顺序,乱了用户得自己找;
3. **存 50 条、显示 10 条**:过滤要能命中第 10 条以前的词 —— 这正是"存得比显示多"
   唯一的理由,必须有用例钉住;
4. **按包含匹配、大小写不敏感**:B站关键字里混着 ``MV`` / ``live`` 这类拉丁字母;
5. 库坏了、行被手改坏了都只当"没有这一条",且**不碰**播放历史那张表。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.history import HistoryEntry, PlayHistory  # noqa: E402
from bilibili_music.core.library_db import DB_FILE_NAME, LibraryDb  # noqa: E402
from bilibili_music.core.search_history import (  # noqa: E402
    SEARCH_HISTORY_LIMIT,
    SEARCH_HISTORY_VISIBLE,
    SearchHistory,
    filter_terms,
    normalize_keyword,
)

#: 落盘类用例的临时目录根(与 test_history 同一套做法,不用 tempfile)。
_SCRATCH_ROOT = Path(__file__).resolve().parent / "_scratch"


class _Clock:
    """受控时钟:每取一次时间就往前走一秒。

    顺序断言因此与真实时间无关 —— 真实 ``time.time()`` 在同一微秒内连续记两条时,
    "谁在前"只能靠 ``rowid`` 兜底,拿它当断言会让用例带上一丝偶发性。
    """

    def __init__(self, start: float = 1000.0) -> None:
        """从 ``start`` 开始计时。

        Args:
            start: 起始时间戳(秒)。
        """
        self._now = start

    def __call__(self) -> float:
        """返回当前时刻并前进一秒。"""
        self._now += 1.0
        return self._now


class TestNormalizeKeyword(unittest.TestCase):
    """关键字的规范化(它必须与提交搜索时的口径一致)。"""

    def test_strips_surrounding_whitespace(self) -> None:
        """首尾空白要丢掉:用户很可能带着一个尾随空格按回车。"""
        self.assertEqual(normalize_keyword("  周杰伦 MV  "), "周杰伦 MV")

    def test_keeps_inner_whitespace_and_case(self) -> None:
        """词内空白与大小写**原样保留**:历史词要能一字不差地填回搜索框。"""
        self.assertEqual(normalize_keyword("Jay  Chou Live"), "Jay  Chou Live")

    def test_non_text_becomes_empty(self) -> None:
        """手改过的库里 ``keyword`` 可能是数字 : 一律当"没有这个词"。"""
        self.assertEqual(normalize_keyword(123), "")
        self.assertEqual(normalize_keyword(None), "")
        self.assertEqual(normalize_keyword("   "), "")


class TestFilterTerms(unittest.TestCase):
    """下拉框的过滤规则(纯函数,不碰库)。"""

    def test_empty_query_returns_the_newest_terms(self) -> None:
        """还没打字时给最近的若干条 —— 点一下搜索框就该看到"我上次搜了什么"。"""
        terms = ("c", "b", "a")
        self.assertEqual(filter_terms(terms, ""), ("c", "b", "a"))
        self.assertEqual(filter_terms(terms, "   "), ("c", "b", "a"))

    def test_contains_match_keeps_order_and_can_hit_the_tail(self) -> None:
        """**包含匹配**:输入 ``杰伦`` 要能命中 ``周杰伦 MV``(不是前缀匹配)。"""
        terms = ("晴天", "周杰伦 MV", "周杰伦 演唱会", "夜曲")
        self.assertEqual(
            filter_terms(terms, "杰伦"), ("周杰伦 MV", "周杰伦 演唱会")
        )

    def test_match_is_case_insensitive(self) -> None:
        """大小写不敏感:B站关键字里混着 ``MV`` / ``live``,用户不会记得当初敲的是什么。"""
        self.assertEqual(filter_terms(("Jay MV",), "jay mv"), ("Jay MV",))
        self.assertEqual(filter_terms(("jay 4K",), "4k"), ("jay 4K",))

    def test_no_match_returns_empty(self) -> None:
        """一条都匹配不上时返回空:上层据此收起下拉框,而不是留一个空浮层。"""
        self.assertEqual(filter_terms(("晴天", "夜曲"), "杰伦"), ())

    def test_limit_truncates_and_zero_means_no_limit(self) -> None:
        """默认按显示上限截断;``0`` 表示"不限制"(给调用方留一个口子)。"""
        terms = tuple(f"词{i}" for i in range(20))
        self.assertEqual(len(filter_terms(terms, "")), SEARCH_HISTORY_VISIBLE)
        self.assertEqual(len(filter_terms(terms, "", limit=3)), 3)
        self.assertEqual(len(filter_terms(terms, "", limit=0)), 20)


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

    def _history(
        self, db: LibraryDb | None = None, *, limit: int = SEARCH_HISTORY_LIMIT
    ) -> SearchHistory:
        """建一个注入受控时钟的搜索历史(顺序断言与真实时间无关)。

        Args:
            db: 库句柄;``None`` 表示在沙箱目录里现建一个。
            limit: 库里最多保留几条。
        """
        self.clock = _Clock()
        return SearchHistory(
            db if db is not None else self._db(), limit=limit, now=self.clock
        )


class TestSearchHistory(_ScratchCase):
    """搜索历史的读写。"""

    def test_entries_are_newest_first(self) -> None:
        """顺序就是下拉框的显示顺序:最近搜索的在最前。"""
        history = self._history()
        for keyword in ("a", "b", "c"):
            self.assertTrue(history.record(keyword))
        self.assertEqual(history.entries(), ("c", "b", "a"))

    def test_the_same_word_is_kept_once_and_moved_to_the_front(self) -> None:
        """重搜同一个词:只把它提到最前,不多出一行(去重由主键 + UPSERT 完成)。"""
        history = self._history()
        for keyword in ("a", "b", "a"):
            history.record(keyword)
        self.assertEqual(history.entries(), ("a", "b"))

    def test_blank_keyword_is_not_recorded(self) -> None:
        """空白关键字不算一次搜索(与 ``on_search`` 的早退口径一致)。"""
        history = self._history()
        self.assertFalse(history.record("   "))
        self.assertFalse(history.record(""))
        self.assertEqual(history.entries(), ())

    def test_trim_keeps_only_the_newest(self) -> None:
        """超过上限时按搜索时间从旧到新丢掉,留下的仍是最新的那几条。"""
        history = self._history(limit=3)
        for keyword in ("a", "b", "c", "d", "e"):
            history.record(keyword)
        self.assertEqual(history.entries(), ("e", "d", "c"))

    def test_default_limit_is_fifty_entries(self) -> None:
        """默认保留 50 条:存得比显示多,过滤才有机会命中更早的词。"""
        history = self._history()
        for index in range(SEARCH_HISTORY_LIMIT + 5):
            history.record(f"词{index:03d}")
        entries = history.entries()
        self.assertEqual(len(entries), SEARCH_HISTORY_LIMIT)
        self.assertEqual(entries[0], f"词{SEARCH_HISTORY_LIMIT + 4:03d}")
        self.assertNotIn("词000", entries)

    def test_suggest_shows_at_most_ten(self) -> None:
        """下拉框最多给 10 条(需求写死的显示上限)。"""
        history = self._history()
        for index in range(20):
            history.record(f"词{index:02d}")
        self.assertEqual(len(history.suggest("")), SEARCH_HISTORY_VISIBLE)
        self.assertEqual(history.suggest("")[0], "词19")

    def test_suggest_can_reach_beyond_the_visible_ten(self) -> None:
        """存 50 条的意义就在这里:第 10 条以前的词也要能被过滤命中。"""
        history = self._history()
        for index in range(20):
            history.record(f"词{index:02d}")
        # "词05" 排在显示窗口(最近 10 条)之外
        self.assertEqual(history.suggest("词05"), ("词05",))

    def test_suggest_matches_contains_and_ignores_case(self) -> None:
        """下拉框按**包含匹配**过滤,且大小写不敏感。"""
        history = self._history()
        history.record("周杰伦 MV")
        history.record("晴天")
        self.assertEqual(history.suggest("杰伦"), ("周杰伦 MV",))
        self.assertEqual(history.suggest("mv"), ("周杰伦 MV",))

    def test_broken_db_degrades_to_no_history(self) -> None:
        """库打不开时只当"没有历史":搜索本身绝不能因为历史存不下来而失败。"""
        # 让库文件的父路径是一个**普通文件**,建目录那一步必定失败
        (self.tmp / "not-a-dir").write_text("x", encoding="utf-8")
        history = self._history(LibraryDb(self.tmp / "not-a-dir" / DB_FILE_NAME))
        self.assertFalse(history.record("周杰伦"))
        self.assertEqual(history.entries(), ())
        self.assertEqual(history.suggest("周"), ())
        self.assertEqual(history.trim(), 0)

    def test_hand_edited_rows_are_ignored(self) -> None:
        """手改坏的行走这里被丢掉(空串 / 二进制),不影响其余条目。

        故意用 BLOB 而不是数字:``keyword`` 是 TEXT 列,SQLite 会按类型亲和性把数字转成
        文本存进去 —— 那样它就成了一条看着正常的历史,测不出"非文本要被丢掉"。
        """
        db = self._db()
        history = self._history(db)
        history.record("周杰伦")
        insert = "INSERT INTO search_history (keyword, searched_at) VALUES (?, ?)"
        db.execute(insert, ("", 5.0))
        db.execute(insert, (b"\x01\x02", 6.0))
        self.assertEqual(history.entries(), ("周杰伦",))

    def test_clearing_play_history_keeps_search_history(self) -> None:
        """两个功能同库不同表:"清空播放历史"不该顺手把搜索词清掉。"""
        db = self._db()
        history = self._history(db)
        history.record("周杰伦")
        plays = PlayHistory(db)
        plays.record(
            HistoryEntry(
                bvid="BV1",
                cid=100,
                page_index=1,
                title="晴天",
                author="周杰伦",
                page_title="晴天",
                multipart=False,
                duration=269,
                cover_url="https://i0.hdslb.com/a.jpg",
                played_at=1.0,
            )
        )
        plays.clear()
        self.assertEqual(plays.entries(), ())
        self.assertEqual(history.entries(), ("周杰伦",))


if __name__ == "__main__":
    unittest.main()
