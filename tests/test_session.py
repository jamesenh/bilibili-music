"""``core/session.py`` 的单元测试:凭据解析、落盘、容错与登出。

不触网、不碰 Qt。落盘类用例把目录指向 ``tests/_scratch/<模块名>/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒;
见 ``AGENTS.md`` 7.1)。目录名带模块名是为了避免跨模块同名用例撞车 —— 见
``docs/ROADMAP.md`` 里记的那个 Windows 专有缺陷。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.config import config_root  # noqa: E402
from bilibili_music.core.session import (  # noqa: E402
    SESSION_FILE_NAME,
    Session,
    SessionStore,
    parse_cookie_header,
    session_from_cookies,
)

#: 落盘类用例的临时目录根(带模块名,避免跨模块同名用例共用目录)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch" / Path(__file__).stem


class TestParseCookieHeader(unittest.TestCase):
    """粘贴进来的 ``Cookie`` 头怎么解析。"""

    def test_splits_names_and_values(self) -> None:
        """分号分隔的多项要逐个拆开。"""
        raw = "SESSDATA=abc%2C123%2Cxyz%2A11; bili_jct=deadbeef; DedeUserID=42"
        self.assertEqual(
            parse_cookie_header(raw),
            {
                "SESSDATA": "abc%2C123%2Cxyz%2A11",
                "bili_jct": "deadbeef",
                "DedeUserID": "42",
            },
        )

    def test_keeps_value_untouched(self) -> None:
        """取值一律原样保留 —— 解码过的 SESSDATA 再拼回去就会失效。"""
        parsed = parse_cookie_header("SESSDATA=a%2Cb%3Dc")
        self.assertEqual(parsed["SESSDATA"], "a%2Cb%3Dc")

    def test_handles_newlines_and_blank_segments(self) -> None:
        """浏览器里复制出来的头可能带换行、末尾分号与空段,都要能吃下。"""
        raw = "SESSDATA=x;\n\n  bili_jct=y ;; DedeUserID=1;"
        self.assertEqual(
            parse_cookie_header(raw),
            {"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "1"},
        )

    def test_drops_segments_without_a_name_or_value(self) -> None:
        """没有名字、没有取值、以及不含 ``=`` 的脏段一律跳过。"""
        self.assertEqual(parse_cookie_header("; =v; novalue; a=; b=2"), {"b": "2"})

    def test_preserves_equals_inside_the_value(self) -> None:
        """取值里可以再出现 ``=``(base64 之类的凭据就有),只按第一个 ``=`` 切。"""
        self.assertEqual(parse_cookie_header("token=YWJj=="), {"token": "YWJj=="})

    def test_empty_input_gives_empty_mapping(self) -> None:
        """空串不给任何 Cookie(调用方据此判断"没给凭据")。"""
        self.assertEqual(parse_cookie_header(""), {})


class TestSessionModel(unittest.TestCase):
    """会话对象的收敛规则。"""

    def test_normalized_drops_blank_entries(self) -> None:
        """空名字与空取值都要被清掉,否则"有没有凭据"会出现假阳性。"""
        session = Session(cookies={"SESSDATA": "x", "": "y", "bili_jct": "   "}).normalized()
        self.assertEqual(session.cookies, {"SESSDATA": "x"})

    def test_normalized_clamps_mid_and_time(self) -> None:
        """``mid`` 与时间戳不能是负数。"""
        session = Session(cookies={"SESSDATA": "x"}, mid=-5, saved_at=-1.0).normalized()
        self.assertEqual(session.mid, 0)
        self.assertEqual(session.saved_at, 0.0)

    def test_is_usable_requires_sessdata(self) -> None:
        """只有 ``bili_jct`` 不算登录态;有 ``SESSDATA`` 才算。"""
        self.assertFalse(Session(cookies={"bili_jct": "x"}).is_usable)
        self.assertFalse(Session(cookies={"SESSDATA": "  "}).is_usable)
        self.assertTrue(Session(cookies={"SESSDATA": "x"}).is_usable)

    def test_session_from_cookies_stamps_time(self) -> None:
        """造会话时可以注入时间戳,免得测试依赖真实时钟。"""
        session = session_from_cookies({"SESSDATA": "x"}, uname="甲", mid=7, now=1234.5)
        self.assertEqual(session.saved_at, 1234.5)
        self.assertEqual((session.uname, session.mid), ("甲", 7))
        self.assertTrue(session.is_usable)


class TestSessionStore(unittest.TestCase):
    """会话文件的读写与容错。"""

    def setUp(self) -> None:
        """每个用例一个独立的沙箱目录。"""
        self.tmp = _SCRATCH / self._testMethodName
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.store = SessionStore(self.tmp / SESSION_FILE_NAME)

    def test_default_path_lives_next_to_config(self) -> None:
        """默认路径必须与 ``config.json`` 同目录 —— 凭据属于用户数据,该跟着漫游。"""
        self.assertEqual(SessionStore().path, config_root() / SESSION_FILE_NAME)

    def test_save_then_load_round_trip(self) -> None:
        """存进去再读出来,每个字段都要对得上。"""
        session = session_from_cookies(
            {"SESSDATA": "a%2Cb", "bili_jct": "c"}, uname="甲", mid=42, now=99.0
        )
        self.store.save(session)
        loaded = self.store.load()
        assert loaded is not None
        self.assertEqual(loaded.cookies, {"SESSDATA": "a%2Cb", "bili_jct": "c"})
        self.assertEqual((loaded.uname, loaded.mid), ("甲", 42))
        self.assertEqual(loaded.saved_at, 99.0)

    def test_save_is_atomic_and_leaves_no_part_file(self) -> None:
        """写完不能留下 ``.part`` 残片;正式文件必须存在。"""
        self.store.save(session_from_cookies({"SESSDATA": "x"}, now=1.0))
        self.assertTrue(self.store.path.exists())
        self.assertEqual(list(self.tmp.glob("*.part")), [])

    def test_save_does_not_write_blank_cookies(self) -> None:
        """收敛过的脏数据不许落盘(空取值会被清掉)。"""
        self.store.save(Session(cookies={"SESSDATA": "x", "junk": "  "}))
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["cookies"], {"SESSDATA": "x"})

    def test_load_missing_file_returns_none(self) -> None:
        """从没登录过时读不到东西,应当是 ``None`` 而不是异常。"""
        self.assertIsNone(self.store.load())

    def test_load_broken_json_returns_none(self) -> None:
        """写到一半被杀的半截 JSON 不能让应用起不来。"""
        self.store.path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.store.load())

    def test_load_non_object_returns_none(self) -> None:
        """顶层被手改成数组时也当没有凭据。"""
        self.store.path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(self.store.load())

    def test_load_missing_sessdata_returns_none(self) -> None:
        """只有 ``bili_jct`` 的文件不算登录态 —— 拿它去请求只会得到 ``-101``。"""
        self.store.path.write_text(
            json.dumps({"cookies": {"bili_jct": "x"}}), encoding="utf-8"
        )
        self.assertIsNone(self.store.load())

    def test_load_ignores_bad_types_and_unknown_keys(self) -> None:
        """字段类型不对要退回默认值,多出来的键要忽略(向前兼容)。"""
        self.store.path.write_text(
            json.dumps(
                {
                    "cookies": {"SESSDATA": "x", "bili_jct": 5},
                    "mid": "42",
                    "uname": None,
                    "saved_at": "abc",
                    "future_key": {"a": 1},
                }
            ),
            encoding="utf-8",
        )
        loaded = self.store.load()
        assert loaded is not None
        self.assertEqual(loaded.cookies, {"SESSDATA": "x"})
        self.assertEqual(loaded.mid, 42)
        self.assertEqual(loaded.uname, "")
        self.assertEqual(loaded.saved_at, 0.0)

    def test_save_creates_parent_directories(self) -> None:
        """父目录不存在时要自己建出来(首次登录时配置目录可能还不存在)。"""
        nested = SessionStore(self.tmp / "deep" / "deeper" / SESSION_FILE_NAME)
        nested.save(session_from_cookies({"SESSDATA": "x"}, now=1.0))
        self.assertTrue(nested.path.exists())

    def test_clear_removes_the_file(self) -> None:
        """登出要真的把凭据从磁盘上删掉。"""
        self.store.save(session_from_cookies({"SESSDATA": "x"}, now=1.0))
        self.assertTrue(self.store.clear())
        self.assertFalse(self.store.path.exists())

    def test_clear_is_idempotent(self) -> None:
        """重复登出不该报错,只回报"本来就没有"。"""
        self.assertFalse(self.store.clear())
        self.store.save(session_from_cookies({"SESSDATA": "x"}, now=1.0))
        self.assertTrue(self.store.clear())
        self.assertFalse(self.store.clear())

    @unittest.skipIf(sys.platform == "win32", "Windows 没有 POSIX 权限位")
    def test_file_permissions_are_owner_only(self) -> None:
        """POSIX 下落盘权限必须是 ``0o600`` —— 明文凭据不能再让同机其他用户读。"""
        self.store.save(session_from_cookies({"SESSDATA": "x"}, now=1.0))
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
