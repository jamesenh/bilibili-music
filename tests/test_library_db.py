"""本地库(``library.db``)的单元测试:连接、建表、容错与路径。

不触网、不碰 Qt。落盘类用例把目录指向 ``tests/_scratch/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒;
见 ``AGENTS.md`` 第 7.1 节)。

钉住三件事:

1. 建表是**幂等**的:每次打开都执行一遍 ``CREATE TABLE IF NOT EXISTS``,不必先探测文件;
2. 库不可用时**不抛异常**,只说"没有数据" —— 上层于是自然降级(索引退回盲查、
   历史显示空列表)。这是 ``core`` 里唯一一处允许"静默失败"的地方,理由见模块 docstring;
3. 数据跨实例可见(另一个连接写进去的,这边查得到)。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.config import CONFIG_FILE_NAME, config_root  # noqa: E402
from bilibili_music.core.library_db import (  # noqa: E402
    DB_FILE_NAME,
    SCHEMA_VERSION,
    LibraryDb,
    library_db_path,
)

#: 落盘类用例的临时目录根(与 test_cache_index 同一套做法,不用 tempfile)。
_SCRATCH_ROOT = Path(__file__).resolve().parent / "_scratch"


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

    def _db(self, name: str = DB_FILE_NAME) -> LibraryDb:
        """在沙箱目录里建一个本地库句柄。"""
        return LibraryDb(self.tmp / name)


class TestLibraryDbPath(unittest.TestCase):
    """库文件的位置(它决定"清空缓存会不会带走历史")。"""

    def test_default_path_sits_next_to_the_config_file(self) -> None:
        """库放**配置目录**、与 ``config.json`` 同级:两者都是用户数据,该跟着漫游。"""
        self.assertEqual(library_db_path(), config_root() / DB_FILE_NAME)
        self.assertEqual(library_db_path().parent, config_root())
        self.assertNotEqual(library_db_path().name, CONFIG_FILE_NAME)

    def test_path_is_injectable(self) -> None:
        """路径可注入:测试与脚本因此能把库指到任意目录,不碰用户真实数据。"""
        self.assertEqual(LibraryDb(Path("/tmp/x/library.db")).path, Path("/tmp/x/library.db"))


class TestLibraryDbSchema(_ScratchCase):
    """连接与建表。"""

    def test_first_query_creates_parent_directories_and_the_file(self) -> None:
        """目录还不存在时要自己建出来,否则首次启动就查不了(也更写不了)。"""
        db = LibraryDb(self.tmp / "nested" / "deeper" / DB_FILE_NAME)
        self.assertEqual(db.query("SELECT 1 AS ok")[0]["ok"], 1)
        self.assertTrue(db.path.exists())

    def test_all_tables_exist(self) -> None:
        """缓存索引、播放历史与下载任务在同一个库里各占一张表(互不牽连)。"""
        names = {
            row["name"]
            for row in self._db().query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertLessEqual({"cached_tracks", "play_history", "download_tasks"}, names)

    def test_schema_version_is_written(self) -> None:
        """结构版本写进 ``user_version``:以后改表时能看出这份库是哪一代建的。"""
        self.assertEqual(int(self._db().query("PRAGMA user_version")[0][0]), SCHEMA_VERSION)

    def test_journal_mode_is_wal(self) -> None:
        """开 WAL:写入不阻塞读取,进程被杀也不会留下半截事务。"""
        self.assertEqual(str(self._db().query("PRAGMA journal_mode")[0][0]).lower(), "wal")

    def test_reopening_an_existing_database_keeps_the_schema(self) -> None:
        """第二次打开(下次启动)不该报错,也不该丢数据。"""
        first = self._db()
        first.execute(
            "INSERT INTO cached_tracks (file_name, bvid, cid) VALUES (?, ?, ?)",
            ("a.m4a", "BV1", 100),
        )
        second = self._db()
        self.assertTrue(second.available)
        self.assertEqual(len(second.query("SELECT * FROM cached_tracks")), 1)

    def test_close_then_reuse_works(self) -> None:
        """``close()`` 之后还能再连上(退出时关连接不该让对象变成一次性用品)。"""
        db = self._db()
        self.assertTrue(db.available)
        db.close()
        self.assertEqual(db.query("SELECT 1 AS ok")[0]["ok"], 1)


class TestLibraryDbQueries(_ScratchCase):
    """读写接口。"""

    def test_execute_returns_the_affected_row_count(self) -> None:
        """写语句返回受影响行数,调用方靠它回答"真的删掉了吗"。"""
        db = self._db()
        db.execute(
            "INSERT INTO cached_tracks (file_name, bvid, cid) VALUES (?, ?, ?)",
            ("a.m4a", "BV1", 100),
        )
        db.execute(
            "INSERT INTO cached_tracks (file_name, bvid, cid) VALUES (?, ?, ?)",
            ("b.m4a", "BV2", 200),
        )
        self.assertEqual(db.execute("DELETE FROM cached_tracks WHERE bvid = ?", ("BV1",)), 1)
        self.assertEqual(db.execute("DELETE FROM cached_tracks"), 1)

    def test_query_returns_rows_in_insertion_order(self) -> None:
        """查询按 ``rowid`` 顺序返回;索引与历史都靠它表达"谁先谁后"。"""
        db = self._db()
        for name in ("a.m4a", "b.m4a", "c.m4a"):
            db.execute(
                "INSERT INTO cached_tracks (file_name, bvid, cid) VALUES (?, ?, ?)",
                (name, "BV1", 100),
            )
        self.assertEqual(
            [row["file_name"] for row in db.query("SELECT * FROM cached_tracks")],
            ["a.m4a", "b.m4a", "c.m4a"],
        )

    def test_rows_are_addressable_by_column_name(self) -> None:
        """行对象要能按列名取值:按位置取会在以后加列时静默错位。"""
        row = self._db().query("SELECT 7 AS answer")[0]
        self.assertEqual(row["answer"], 7)


class TestLibraryDbTolerance(_ScratchCase):
    """库不可用时的一切都要"当没有数据",而不是抛异常。"""

    def _broken(self) -> LibraryDb:
        """指向一个**被文件挡住的路径**的库(开库必然失败)。"""
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        return LibraryDb(blocker / "sub" / DB_FILE_NAME)

    def test_available_is_false(self) -> None:
        """开不了库时 ``available`` 要如实报 False,让上层能走降级路径。"""
        self.assertFalse(self._broken().available)

    def test_execute_returns_none(self) -> None:
        """写失败返回 ``None``,绝不抛异常(调用方是播放解析流程)。"""
        db = self._broken()
        self.assertIsNone(db.execute("DELETE FROM cached_tracks"))

    def test_query_returns_empty(self) -> None:
        """读失败返回空列表,于是上层自然显示"没有数据"。"""
        self.assertEqual(self._broken().query("SELECT * FROM cached_tracks"), [])

    def test_close_on_a_never_opened_database_is_harmless(self) -> None:
        """从没连上过的库也能安全关闭(退出路径不该因为这点小事报错)。"""
        db = self._broken()
        db.close()
        self.assertFalse(db.available)

    def test_bad_sql_is_swallowed(self) -> None:
        """语句写错也只是"没有结果",不会把异常抛到界面上。"""
        db = self._db()
        self.assertIsNone(db.execute("DELETE FROM table_that_does_not_exist"))
        self.assertEqual(db.query("SELECT * FROM table_that_does_not_exist"), [])


if __name__ == "__main__":
    unittest.main()
