"""主题单元测试:应用级 ``QPalette`` 与图标调色板的对应关系。

不触网、不写盘。需要 ``QApplication``(``apply_theme`` 会改应用状态),所以沿用
``test_icons.py`` 的做法:先设离屏平台,再复用已有实例。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bilibili_music.core.config import THEMES  # noqa: E402
from bilibili_music.ui.icons import DARK, LIGHT  # noqa: E402
from bilibili_music.ui.theme import apply_theme, build_palette  # noqa: E402


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


class TestBuildPalette(unittest.TestCase):
    """按主题名构造应用级调色板。"""

    def test_light_uses_style_defaults(self) -> None:
        """浅色返回默认 ``QPalette``:等于"交给样式自己决定",比手写一套更贴近平台观感。"""
        self.assertEqual(build_palette("light"), QPalette())

    def test_dark_changes_window_and_text(self) -> None:
        """深色必须真的改掉底色与文字色,否则等于没切。"""
        palette = build_palette("dark")
        default_window = QPalette().color(QPalette.ColorRole.Window)
        self.assertNotEqual(palette.color(QPalette.ColorRole.Window), default_window)
        # 比 QColor 而不是比 name():name() 会把十六进制转成小写,"#EAEAEA" 与
        # "#eaeaea" 本是同一个颜色,拿字符串比较会假失败
        self.assertEqual(palette.color(QPalette.ColorRole.WindowText), QColor(DARK.text))
        self.assertEqual(palette.color(QPalette.ColorRole.Base), QColor("#232323"))

    def test_dark_accent_comes_from_the_icon_palette(self) -> None:
        """强调色与图标调色板**同源**,避免"主题色在两处各写一遍,改一处忘一处"。"""
        palette = build_palette("dark")
        self.assertEqual(
            palette.color(QPalette.ColorRole.Highlight), QColor(DARK.accent)
        )
        self.assertEqual(
            palette.color(QPalette.ColorRole.HighlightedText), QColor(DARK.on_accent)
        )

    def test_dark_dimms_disabled_text(self) -> None:
        """禁用态要单独给色,否则深色下"禁用"和"可点"看起来一模一样。"""
        palette = build_palette("dark")
        disabled = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
        )
        self.assertEqual(disabled, QColor(DARK.disabled))
        self.assertNotEqual(disabled, palette.color(QPalette.ColorRole.ButtonText))

    def test_unknown_theme_raises_and_lists_alternatives(self) -> None:
        """未知主题名要立刻报错,并把可用取值写在消息里。"""
        with self.assertRaises(ValueError) as ctx:
            build_palette("neon")
        for name in THEMES:
            self.assertIn(name, str(ctx.exception))


class TestApplyTheme(unittest.TestCase):
    """把主题应用到应用实例。"""

    def setUp(self) -> None:
        """每个用例结束后恢复浅色,免得污染同进程里其它测试模块。"""
        self.app = QApplication.instance()
        assert self.app is not None
        self.addCleanup(apply_theme, self.app, "light")

    def test_returns_icon_palette_of_the_same_theme(self) -> None:
        """返回值必须是同主题的图标调色板 —— 控件要用它重新染色 SVG。"""
        self.assertIs(apply_theme(self.app, "dark"), DARK)
        self.assertIs(apply_theme(self.app, "light"), LIGHT)

    def test_application_palette_actually_changes(self) -> None:
        """应用级调色板要真的被换掉,并且能切回来。"""
        apply_theme(self.app, "dark")
        dark_window = self.app.palette().color(QPalette.ColorRole.Window).name()
        self.assertEqual(dark_window, "#2b2b2b")
        apply_theme(self.app, "light")
        self.assertNotEqual(
            self.app.palette().color(QPalette.ColorRole.Window).name(), dark_window
        )

    def test_unknown_theme_leaves_application_state_alone(self) -> None:
        """未知主题要在动应用状态**之前**失败,不能留下半套配色。"""
        apply_theme(self.app, "dark")
        before = self.app.palette().color(QPalette.ColorRole.Window).name()
        with self.assertRaises(ValueError):
            apply_theme(self.app, "neon")
        self.assertEqual(
            self.app.palette().color(QPalette.ColorRole.Window).name(), before
        )


if __name__ == "__main__":
    unittest.main()
