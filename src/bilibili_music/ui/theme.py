"""主题(浅色 / 深色)的应用。

**只换图标颜色是不够的**。控件自己的底色、文字色来自应用级 ``QPalette``,不一起换
就会出现"深色图标配浅色表格"的花屏。所以主题分两部分:

1. 应用级 ``QPalette``(本模块负责)—— 决定控件底/字/选中色
2. 图标调色板(:mod:`bilibili_music.ui.icons` 的 ``Palette``)—— 决定 SVG 染成什么色

两者共用同一份颜色取值:``accent`` / ``muted`` / ``disabled`` 一律从 ``icons`` 的
调色板取,避免"主题色在两处各写一遍,改一处忘一处"。

为什么切到 ``Fusion`` 样式:Windows 的原生样式对 ``QPalette`` 响应不完整,
深色主题会部分失效(表格还是白的);``Fusion`` 在两个平台上都老老实实遵循
``QPalette``。代价是控件外观不再是系统原生 —— 对自研界面的项目可以接受。
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ..core.config import THEMES
from .icons import Palette, palette as icon_palette

__all__ = ["apply_theme", "build_palette"]

#: 深色主题里几个不在图标调色板里的表面色。
#:
#: 取值思路是"比纯黑浅一点、层次靠亮度差":纯黑底配纯白字对比过强,长时间看很累。
_DARK_WINDOW = "#2B2B2B"
_DARK_BASE = "#232323"
_DARK_BUTTON = "#353535"


def build_palette(name: str) -> QPalette:
    """按主题名构造应用级 ``QPalette``。

    浅色主题返回**默认构造**的 ``QPalette``:在 Qt 里空 ``QPalette`` 的含义是
    "用样式自己的默认配色",比手写一套浅色更贴近平台观感。

    Args:
        name: ``"light"`` 或 ``"dark"``,取值见 :data:`bilibili_music.core.config.THEMES`。

    Returns:
        可直接交给 ``QApplication.setPalette`` 的调色板。

    Raises:
        ValueError: 主题名未知;消息里会列出所有可用主题。
    """
    if name not in THEMES:
        known = "、".join(THEMES)
        raise ValueError(f"未知主题 {name!r},可用:{known}")

    if name == "light":
        return QPalette()

    colors = icon_palette("dark")
    result = QPalette()
    #: 角色到颜色的映射;逐一 setColor 而不是用 setBrush,便于读
    mapping = {
        QPalette.ColorRole.Window: _DARK_WINDOW,
        QPalette.ColorRole.WindowText: colors.text,
        QPalette.ColorRole.Base: _DARK_BASE,
        QPalette.ColorRole.AlternateBase: _DARK_WINDOW,
        QPalette.ColorRole.Text: colors.text,
        QPalette.ColorRole.Button: _DARK_BUTTON,
        QPalette.ColorRole.ButtonText: colors.text,
        QPalette.ColorRole.Highlight: colors.accent,
        QPalette.ColorRole.HighlightedText: colors.on_accent,
        QPalette.ColorRole.ToolTipBase: _DARK_WINDOW,
        QPalette.ColorRole.ToolTipText: colors.text,
        QPalette.ColorRole.PlaceholderText: colors.muted,
    }
    for role, value in mapping.items():
        result.setColor(role, QColor(value))
    # 禁用态要单独给:否则深色下禁用文字与正常文字几乎一样,按钮"看起来能点"
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
    ):
        result.setColor(
            QPalette.ColorGroup.Disabled, role, QColor(colors.disabled)
        )
    return result


def apply_theme(app: QApplication, name: str) -> Palette:
    """把主题应用到整个应用,并返回该主题的图标调色板。

    主题是**应用级**的,不是窗口级的:同一个进程里所有窗口一起换,避免出现
    "主窗口深色、弹窗浅色"。

    Args:
        app: 应用实例。
        name: ``"light"`` 或 ``"dark"``。

    Returns:
        该主题的图标调色板;调用方应把它交给各控件(:meth:`PlayerBar.apply_palette`
        等)用于重绘图标与文字颜色。

    Raises:
        ValueError: 主题名未知。
    """
    colors = build_palette(name)  # 先校验主题名,未知名字不该动到应用状态
    app.setStyle("Fusion")
    app.setPalette(colors)
    return icon_palette(name)
