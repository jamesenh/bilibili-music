"""图标资源测试:验证每个 SVG 都能渲染、能被染色,并且缓存按维度命中。

这些测试**只用内存位图,不碰磁盘临时目录**,也不触网。

Qt 需要有应用实例才能建 ``QPixmap``,所以这里在导入被测模块前先强制离屏平台并创建
一个应用 —— 与 ``scripts/_helpers.py::ensure_app`` 的理由一致。

**用 ``QApplication`` 而不是 ``QGuiApplication``**:整个测试进程只能有一个应用实例,
而 ``tests/test_ui_wiring.py`` 要造 ``QWidget``,只建了 ``QGuiApplication`` 的话
"谁先跑谁说了算",widget 用例会在全量跑时突然起不来(``AGENTS.md`` 第 5 节同一条口径)。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在创建应用实例之前设置,否则无显示环境下起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 scripts/_helpers.py 的说明)
from PySide6.QtGui import QColor, QGuiApplication, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QSize  # noqa: E402

from bilibili_music.ui import icons as icons_mod  # noqa: E402
from bilibili_music.ui.icons import (  # noqa: E402
    DARK,
    app_icon_path,
    available_icons,
    clear_cache,
    get_icon,
    icon_path,
    render_pixmap,
)


def _app() -> QApplication:
    """确保存在 QApplication(幂等);已有实例时直接复用。"""
    return QApplication.instance() or QApplication(sys.argv)


class TestIconResources(unittest.TestCase):
    """图标文件与定位。"""

    def setUp(self) -> None:
        """图标定位不依赖 QApplication,但仍统一建一次,避免用例间行为不一致。"""
        _app()

    def test_icons_dir_is_found_relative_to_module(self) -> None:
        """ICONS_DIR 必须锚定到模块自身位置,否则换工作目录或打包后图标目录会解析错。"""
        self.assertTrue(icons_mod.ICONS_DIR.is_dir())

    def test_expected_icons_present(self) -> None:
        """界面会用到的图标必须都在,缺一个界面就会启动即报错。"""
        expected = {
            "play",
            "pause",
            "prev",
            "next",
            "stop",
            "search",
            "download",
            "music",
            "volume",
            "volume-mute",
            "repeat",
            "repeat-one",
            "shuffle",
            "list",
            "heart",
            "close",
            "refresh",
            # 自绘标题栏与新版式新增的
            "minimize",
            "maximize",
            "restore",
            "home",
            "plus",
            "more-vertical",
            "trash",
            "expand",
        }
        self.assertEqual(expected - set(available_icons()), set())

    def test_icon_path_rejects_unknown_name(self) -> None:
        """未知图标名必须立刻抛 ValueError,不能把拼错的名字当合法路径传给后续加载。"""
        with self.assertRaises(ValueError):
            icon_path("definitely-not-an-icon")

    def test_icon_path_rejects_traversal(self) -> None:
        """路径拼接必须挡掉 ../ 穿越。"""
        with self.assertRaises(ValueError):
            icon_path("../../main_window")

    def test_app_icon_exists(self) -> None:
        """应用图标文件必须真实存在,否则窗口标题栏与任务栏会没有图标。"""
        path = app_icon_path()
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file())


class TestIconRendering(unittest.TestCase):
    """栅格化与着色。"""

    def setUp(self) -> None:
        """每个用例前清空 lru_cache,否则上一条用例缓存的位图会掩盖本用例的渲染问题。"""
        _app()
        clear_cache()

    tearDown = setUp

    def test_every_icon_renders_with_visible_pixels(self) -> None:
        """逐个渲染,既验证 SVG 语法没写错,也防止画出个全透明的空图。"""
        for name in available_icons():
            with self.subTest(icon=name):
                image = render_pixmap(name, "#000000", 32).toImage()
                self.assertEqual(image.size(), QSize(32, 32))
                has_ink = any(
                    image.pixelColor(x, y).alpha() > 0
                    for x in range(32)
                    for y in range(32)
                )
                self.assertTrue(has_ink, f"{name} 渲染结果是全透明的")

    def test_color_is_applied(self) -> None:
        """染色真的生效:同一图标换颜色,输出像素必须跟着变。"""
        red = render_pixmap("play", "#FF0000", 24).toImage()
        blue = render_pixmap("play", "#0000FF", 24).toImage()
        self.assertNotEqual(red, blue)

        # 找一个不透明像素,确认就是目标色
        target = next(
            (x, y)
            for x in range(24)
            for y in range(24)
            if red.pixelColor(x, y).alpha() > 0
        )
        self.assertEqual(red.pixelColor(*target).name(), "#ff0000")
        self.assertEqual(blue.pixelColor(*target).name(), "#0000ff")

    def test_cache_returns_same_object_for_same_key(self) -> None:
        """同名同色同尺寸重复渲染必须命中 lru_cache,否则列表滚动时会反复栅格化 SVG。"""
        first = render_pixmap("music", "#123456", 24).toImage()
        second = render_pixmap("music", "#123456", 24).toImage()
        self.assertEqual(first, second)
        info = icons_mod._tinted_image.cache_info()
        self.assertEqual(info.misses, 1)
        self.assertEqual(info.hits, 1)

    def test_size_changes_are_distinct_cache_entries(self) -> None:
        """尺寸是缓存键的一部分,不同尺寸不能共用条目,否则按钮会拿到缩放错误的图标。"""
        render_pixmap("play", "#000000", 16)
        render_pixmap("play", "#000000", 24)
        self.assertEqual(icons_mod._tinted_image.cache_info().misses, 2)

    def test_render_before_app_raises_python_error(self) -> None:
        """没有 QGuiApplication 时要报 Python 异常,而不是直接段错误。"""
        original = QGuiApplication.instance
        icons_mod.QGuiApplication.instance = staticmethod(lambda: None)  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                render_pixmap("play", "#000000", 24)
        finally:
            icons_mod.QGuiApplication.instance = original  # type: ignore[method-assign]


class TestIconObject(unittest.TestCase):
    """QIcon 组装。"""

    def setUp(self) -> None:
        """每个用例前清空 lru_cache,保证测的是本次调用真正渲染出来的位图。"""
        _app()
        clear_cache()

    def test_get_icon_returns_non_null_with_requested_size(self) -> None:
        """get_icon 必须返回请求尺寸的非空 QIcon,否则按钮上会显示成空白占位。"""
        icon = get_icon("play", DARK.text, 20)
        self.assertFalse(icon.isNull())
        self.assertIn(QSize(20, 20), icon.availableSizes())

    def test_disabled_variant_uses_disabled_color(self) -> None:
        """Qt 不会自动灰化 SVG,禁用态必须显式注册,否则按钮禁用后图标不变色。"""
        icon = get_icon("play", DARK.text, 24, disabled_color=DARK.disabled)

        normal = icon.pixmap(QSize(24, 24), mode=QIcon.Mode.Normal).toImage()
        disabled = icon.pixmap(QSize(24, 24), mode=QIcon.Mode.Disabled).toImage()
        self.assertNotEqual(normal, disabled)

        opaque = next(
            (x, y)
            for x in range(24)
            for y in range(24)
            if disabled.pixelColor(x, y).alpha() > 0
        )
        # 预乘 alpha 的往返换算有 ±1 的舍入误差(实测 #5A5A5A 读回来是 #595959),
        # 所以按通道比、允许差 1,而不是拿十六进制字符串硬比
        actual = disabled.pixelColor(*opaque)
        expected = QColor(DARK.disabled)
        for channel in ("red", "green", "blue"):
            with self.subTest(channel=channel):
                self.assertLessEqual(
                    abs(getattr(actual, channel)() - getattr(expected, channel)()), 1
                )

    def test_no_disabled_variant_when_not_requested(self) -> None:
        """未显式传 disabled_color 时不该注册禁用态位图,让 QIcon 走 Qt 自带的降饱和处理。"""
        icon = get_icon("play", DARK.text, 24)
        normal = icon.pixmap(QSize(24, 24), mode=QIcon.Mode.Normal).toImage()
        disabled = icon.pixmap(QSize(24, 24), mode=QIcon.Mode.Disabled).toImage()
        # Qt 自行降饱和处理,但与正常态不同;这里只断言两种模式都能出图
        self.assertFalse(normal.isNull())
        self.assertFalse(disabled.isNull())


class TestPalette(unittest.TestCase):
    """调色板(应用是深色单主题,所以只有一套)。"""

    def test_colors_are_valid_hex(self) -> None:
        """每个颜色都必须是 ``#RRGGBB``,否则 Qt 会解析成无效颜色而画不出图标。"""
        for color in (DARK.text, DARK.muted, DARK.accent, DARK.disabled, DARK.on_accent):
            with self.subTest(color=color):
                self.assertTrue(color.startswith("#"))
                self.assertEqual(len(color), 7)

    def test_roles_are_distinct(self) -> None:
        """正文色与弱化色必须不同:一样的话"次要信息"就区分不出来了。"""
        self.assertNotEqual(DARK.text, DARK.muted)
        self.assertNotEqual(DARK.muted, DARK.accent)


if __name__ == "__main__":
    unittest.main()
