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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402  (必须先于 QtCore)

from bilibili_music.net.client import QtNetworkClient  # noqa: E402
from bilibili_music.net.urllib_client import UrllibClient  # noqa: E402

# 两个后端都必须提供的公共方法
REQUIRED_METHODS = ("warm_up", "get_json", "get_bytes", "download", "close")

# 签名必须一致的公共方法(用于把 sink / on_* 回调这类关键字参数对齐)
SIGNATURE_MATCHED = ("get_json", "get_bytes", "download", "warm_up", "close")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
