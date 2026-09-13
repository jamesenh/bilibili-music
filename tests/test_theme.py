"""主题单元测试:深色 ``QPalette`` 与全局样式表。

应用是**深色单主题**(设计稿即深色,不做浅色变体),所以这里没有"按名字切主题"的用例,
改为验证三件事:调色板真的把默认色改掉了、禁用态与正常态能分清、样式表里确实带上了
强调色与表面色。

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

from bilibili_music.ui.icons import DARK  # noqa: E402
from bilibili_music.ui.theme import (  # noqa: E402
    SURFACES,
    apply_theme,
    build_palette,
    stylesheet,
)


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


class TestBuildPalette(unittest.TestCase):
    """深色应用级调色板。"""

    def test_maps_roles_to_the_dark_surfaces(self) -> None:
        """底色/文字/输入框底都必须落在深色取值上。

        **不能拿 ``QPalette()`` 当"默认浅色"来对比**:无参构造的 ``QPalette`` 取的是
        **当前应用调色板**(``QApplication.palette()``),在已经套过主题的进程里它本身
        就是深色,那样比等于自比自。
        """
        scheme = build_palette()
        # 比 QColor 而不是比 name():name() 会把十六进制转成小写,"#EAEAEA" 与
        # "#eaeaea" 本是同一个颜色,拿字符串比较会假失败
        self.assertEqual(
            scheme.color(QPalette.ColorRole.Window), QColor(SURFACES.window)
        )
        self.assertEqual(scheme.color(QPalette.ColorRole.WindowText), QColor(DARK.text))
        self.assertEqual(scheme.color(QPalette.ColorRole.Base), QColor(SURFACES.panel))
        # 底色要比正文色暗得多,否则"深色主题"就是自欺欺人
        window = QColor(SURFACES.window)
        self.assertLess(window.lightness(), QColor(DARK.text).lightness() / 2)

    def test_accent_comes_from_the_icon_palette(self) -> None:
        """强调色与图标调色板**同源**,避免"主题色在两处各写一遍,改一处忘一处"。"""
        scheme = build_palette()
        self.assertEqual(scheme.color(QPalette.ColorRole.Highlight), QColor(DARK.accent))
        self.assertEqual(
            scheme.color(QPalette.ColorRole.HighlightedText), QColor(DARK.on_accent)
        )

    def test_disabled_text_is_dimmed(self) -> None:
        """禁用态要单独给色,否则"禁用"和"可点"看起来一模一样。"""
        scheme = build_palette()
        disabled = scheme.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
        )
        self.assertEqual(disabled, QColor(DARK.disabled))
        self.assertNotEqual(disabled, scheme.color(QPalette.ColorRole.ButtonText))

    def test_every_call_returns_an_independent_palette(self) -> None:
        """每次构造都是新对象:调用方改它不该污染别处(``QPalette`` 是隐式共享的)。"""
        first = build_palette()
        first.setColor(QPalette.ColorRole.Window, QColor("#FF0000"))
        self.assertNotEqual(build_palette().color(QPalette.ColorRole.Window), QColor("#FF0000"))


class TestStylesheet(unittest.TestCase):
    """全局 QSS。"""

    @staticmethod
    def _rule(text: str, selector: str) -> str:
        """从样式表里抠出某个选择器的规则块(不确定样式表格式就无法断言字号)。

        只取从 ``<selector> {`` 到其后第一个 ``}`` 的那一段 —— 断言"这条规则写了多大字"
        比断言整份样式表里出现过某个字号更准。

        Args:
            text: 完整样式表。
            selector: 要取的选择器,如 ``"QLabel#PageRowTitle"``。

        Returns:
            规则块文本(含选择器行、不含结尾的 ``}``);找不到时返回空串。
        """
        start = text.find(f"{selector} {{")
        if start < 0:
            return ""
        end = text.find("}", start)
        return text[start:end] if end > start else ""

    def test_contains_accent_and_surface_colors(self) -> None:
        """强调色与表面色都要出现在样式表里 —— 否则设计稿的粉色主按钮就没了。"""
        text = stylesheet()
        self.assertIn(DARK.accent, text)
        self.assertIn(SURFACES.window, text)
        self.assertIn(SURFACES.card_active, text)

    def test_styles_named_widgets_only(self) -> None:
        """样式一律按 ``#objectName`` 选:按类型选会连弹窗里的按钮一起改掉。"""
        text = stylesheet()
        for name in (
            "#AppRoot",
            "#TitleBar",
            "#Sidebar",
            "#PlayerBar",
            "#TrackTable",
            "#PageSelector",
            "#PagePopup",
            "#PageRow",
            "#PageRowTitle",
        ):
            self.assertIn(name, text)

    def test_page_row_title_is_not_styled_as_the_page_heading(self) -> None:
        """分P菜单行的标题必须有自己的字号,不许套上内容区大标题的 20px。

        这两处曾经共用 ``#PageTitle``:QSS 按 objectName 选控件,菜单里的小字行因此被
        内容区大标题(20px 加粗)一起选中,一行只有 34px 高,字差点把行撑满
        (用户反馈"分P菜单里的字太大")。这条用例按选择器分别取规则块来钉住这个边界。
        """
        text = stylesheet()
        self.assertIn("font-size: 13px;", self._rule(text, "QLabel#PageRowTitle"))
        # 大标题那条必须还在:内容区的页面标题本来就该是 20px
        self.assertIn("font-size: 20px;", self._rule(text, "QLabel#PageTitle"))
        # 菜单行标题的规则不许再落到内容区大标题的 objectName 上
        self.assertNotIn("font-size: 13px;", self._rule(text, "QLabel#PageTitle"))

    def test_is_non_trivial(self) -> None:
        """空样式表说明生成逻辑坏了(比如 f-string 没插值),这里挡一道。"""
        self.assertGreater(len(stylesheet()), 2000)


class TestApplyTheme(unittest.TestCase):
    """把主题应用到应用实例。"""

    def setUp(self) -> None:
        """拿到应用实例;每个用例结束后重新套一遍,免得留下改过的状态。"""
        self.app = QApplication.instance()
        assert self.app is not None
        self.addCleanup(apply_theme, self.app)

    def test_returns_the_icon_palette(self) -> None:
        """返回值必须是图标调色板 —— 控件要用它重新染色 SVG。"""
        self.assertIs(apply_theme(self.app), DARK)

    def test_application_palette_and_stylesheet_are_applied(self) -> None:
        """应用级调色板与样式表都要真的落到 ``QApplication`` 上。"""
        apply_theme(self.app)
        self.assertEqual(
            self.app.palette().color(QPalette.ColorRole.Window).name(),
            SURFACES.window.lower(),
        )
        self.assertIn(DARK.accent, self.app.styleSheet())

    def test_applying_twice_is_stable(self) -> None:
        """重复套用不该改变结果(启动与主题变化都会调它)。"""
        apply_theme(self.app)
        first = self.app.palette().color(QPalette.ColorRole.Window).name()
        apply_theme(self.app)
        self.assertEqual(
            self.app.palette().color(QPalette.ColorRole.Window).name(), first
        )


if __name__ == "__main__":
    unittest.main()
