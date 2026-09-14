"""两个网络后端的接口契约测试。

为什么需要这个文件:``QtNetworkClient`` 与 ``UrllibClient`` 是两个独立实现,
只靠 "鸭子类型" 保持一致。曾经踩过两次坑:

* ``cookie_names`` 一边是属性、一边是方法,导致诊断脚本打印出绑定方法对象,
  而且 ``bool(bound_method)`` 恒为真 —— 检查"假通过",永远发现不了问题
* 早期 ``UrllibClient.download`` 引用了不存在的 ``self._sink``,直到真正调用
  才发现

所以这里不下"能不能联网",而是钉死**两个实现的公共接口必须一致**。
"""

from __future__ import annotations

import inspect
import sys
import unittest
from http.cookiejar import Cookie
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtNetwork import QNetworkCookie  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402  (必须先于 QtCore)

from bilibili_music.net.client import QtNetworkClient  # noqa: E402
from bilibili_music.net.urllib_client import UrllibClient  # noqa: E402

# 两个后端都必须提供的公共方法
REQUIRED_METHODS = (
    "warm_up",
    "get_json",
    "get_bytes",
    "download",
    "close",
    "session_cookies",
    "set_session_cookies",
    "clear_session_cookies",
)

# 签名必须一致的公共方法(用于把 sink / on_* 回调这类关键字参数对齐)
SIGNATURE_MATCHED = (
    "get_json",
    "get_bytes",
    "download",
    "warm_up",
    "close",
    "session_cookies",
    "set_session_cookies",
    "clear_session_cookies",
)


class TestBackendContract(unittest.TestCase):
    """静态契约:不触网,不发请求。"""

    @classmethod
    def setUpClass(cls) -> None:
        """只在类级别建一次 ``QApplication`` 与两个后端实例,避免每个用例重复初始化。"""
        cls.app = QApplication.instance() or QApplication([])
        cls.qt = QtNetworkClient()
        cls.urllib = UrllibClient()

    def test_both_expose_required_methods(self) -> None:
        """两个后端必须暴露同一组方法名,否则 --backend 切换会静默炸掉。

        ``REQUIRED_METHODS`` 是上层唯一依赖的调用面(预热、取 JSON、取字节、下载、关闭),
        只用一个后端跑通不代表切换后还能跑,所以逐个方法名在两侧都做 ``callable`` 检查。
        """
        for name in REQUIRED_METHODS:
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(self.qt, name, None)), f"Qt 缺少 {name}")
                self.assertTrue(callable(getattr(self.urllib, name, None)), f"urllib 缺少 {name}")

    def test_cookie_names_is_a_property_on_both(self) -> None:
        """必须是属性 —— 方法会让 ``bool()`` 恒为真,检查静默失效。"""
        for cls in (QtNetworkClient, UrllibClient):
            with self.subTest(backend=cls.__name__):
                self.assertIsInstance(
                    inspect.getattr_static(cls, "cookie_names"),
                    property,
                    f"{cls.__name__}.cookie_names 必须是 property",
                )

    def test_cookie_names_returns_sorted_list(self) -> None:
        """``cookie_names`` 必须返回排好序的 ``list``,保证诊断信息与日志稳定可比对。

        若返回 ``set`` 或乱序列表,同一份 Cookie 在两次运行里会打印成不同顺序,
        排查风控问题时无法直接对比差异。
        """
        for backend in (self.qt, self.urllib):
            with self.subTest(backend=type(backend).__name__):
                value = backend.cookie_names
                self.assertIsInstance(value, list)
                self.assertEqual(value, sorted(value))

    def test_method_signatures_match(self) -> None:
        """关键字参数名必须一致,否则换后端就炸。"""
        for name in SIGNATURE_MATCHED:
            with self.subTest(method=name):
                qt_sig = inspect.signature(getattr(self.qt, name))
                ur_sig = inspect.signature(getattr(self.urllib, name))
                self.assertEqual(
                    list(qt_sig.parameters),
                    list(ur_sig.parameters),
                    f"{name} 的参数名不一致: Qt={list(qt_sig.parameters)} urllib={list(ur_sig.parameters)}",
                )

    def test_download_accepts_sink(self) -> None:
        """``download`` 必须收 ``sink``:早期 urllib 版误用 self._sink,调用即崩。"""
        for backend in (self.qt, self.urllib):
            with self.subTest(backend=type(backend).__name__):
                params = inspect.signature(backend.download).parameters
                self.assertIn("sink", params)
                self.assertIn("on_success", params)
                self.assertIn("on_error", params)
                self.assertIn("on_progress", params)

    def test_get_json_uses_callback_names(self) -> None:
        """``get_json`` 的回调参数必须叫 ``on_success`` / ``on_error``,与全仓库回调命名约定一致。

        调用方普遍用关键字传参,一旦某个后端把参数改名,先跑通的实现会让另一侧在
        运行时抛 ``TypeError``,而静态检查阶段本可以发现这个问题。
        """
        for backend in (self.qt, self.urllib):
            with self.subTest(backend=type(backend).__name__):
                params = inspect.signature(backend.get_json).parameters
                self.assertIn("on_success", params)
                self.assertIn("on_error", params)

    def test_close_is_idempotent(self) -> None:
        """重复 close 不应抛异常。"""
        qt = QtNetworkClient()
        ur = UrllibClient()
        qt.close()
        qt.close()
        ur.close()
        ur.close()

    def test_session_cookies_round_trip_on_both(self) -> None:
        """会话导入导出必须往返一致,否则重启后登录态会静默丢失。

        这条不触网:只往 jar 里灌一份凭据再读出来。两个后端的域/路径口径必须完全一样,
        所以同一个断言跑两遍 —— 换 ``--backend`` 时凭据读写才不会有"一边能存一边读不出"。
        """
        cookies = {"SESSDATA": "a%2Cb%2Cc%2A11", "bili_jct": "deadbeef", "DedeUserID": "42"}
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.set_session_cookies(cookies)
                exported = backend.session_cookies()
                for name, value in cookies.items():
                    self.assertEqual(exported.get(name), value, f"{name} 没有往返成功")

    def test_session_cookies_returns_a_dict(self) -> None:
        """必须是 ``dict``(而不是 list/迭代器):调用方直接拿它落盘或注回 jar。"""
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                self.assertIsInstance(backend.session_cookies(), dict)

    def test_session_cookies_excludes_other_domains(self) -> None:
        """非 B站 域的 Cookie 不许进会话文件。

        这里刻意碰内部 jar:公开接口没法往别的域写 Cookie,而这正是要钉死的边界 ——
        一份"会话文件"里混进别的站点凭据,既无意义又多一份泄露面。
        """
        foreign = "not-bilibili"
        qt = QtNetworkClient()
        jar = qt._manager.cookieJar()  # noqa: SLF001  (契约测试必须直接摆布 jar)
        cookie = QNetworkCookie(b"other", b"x")
        cookie.setDomain(".example.com")
        cookie.setPath("/")
        jar.setCookiesFromUrl([cookie], QUrl("https://example.com/"))
        self.assertNotIn(foreign, qt.session_cookies())

        ur = UrllibClient()
        ur._cookie_jar.set_cookie(  # noqa: SLF001
            Cookie(
                version=0,
                name="other",
                value="x",
                port=None,
                port_specified=False,
                domain=".example.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
        self.assertNotIn(foreign, ur.session_cookies())

    def test_set_session_cookies_ignores_blank_entries(self) -> None:
        """空名字与空取值不许进 jar —— 它们会在 ``cookie_names`` 里造出假阳性。"""
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.set_session_cookies({"": "x", "SESSDATA": "   ", "bili_jct": "ok"})
                self.assertNotIn("", backend.cookie_names)
                self.assertEqual(backend.session_cookies(), {"bili_jct": "ok"})

    def test_clear_session_cookies_empties_the_jar(self) -> None:
        """登出必须真的把凭据从 jar 里去掉,否则"登出了还能接着用"。

        两个后端都要清干净 —— 只删文件不动 jar 是最容易漏的一步:SESSDATA 还在内存里,
        后续请求照样是登录态,用户以为已经登出,实际没有。
        """
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.set_session_cookies({"SESSDATA": "x", "bili_jct": "y"})
                self.assertTrue(backend.session_cookies())
                backend.clear_session_cookies()
                self.assertEqual(backend.session_cookies(), {})
                self.assertEqual(backend.cookie_names, [])

    def test_clear_session_cookies_is_idempotent(self) -> None:
        """重复登出(或从未登录就登出)不该抛异常。"""
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.clear_session_cookies()
                backend.clear_session_cookies()
                self.assertEqual(backend.session_cookies(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
