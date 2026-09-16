"""图标资源与运行时着色。

设计取舍(为什么不是 ``.qrc``)
----------------------------

图标以**独立 SVG 源文件**存放在 ``resources/icons/`` 下,由本模块在运行时读盘、
栅格化并按需染色,**没有编译步骤,也没有生成物**。

不采用 ``pyside6-rcc`` 把资源编成 ``rc_icons.py`` 的原因:

1. **换色是硬伤**。``.qrc`` 里的 SVG 填色写死在源文件中,深色主题就得为同一图标
   再维护一份;本模块方案下同一份源文件可以染任意颜色,深浅主题共用。
2. **多一个构建步骤**。改一个图标要重跑 ``pyside6-rcc``,且生成的 ``.py`` 要不要
   入库会反复纠结。
3. **它要解决的问题此处已不存在**。``.qrc`` 的核心价值是把资源打进二进制不丢,
   而本项目的打包边界(``pyproject.toml`` 的 ``packages``)已经覆盖包内数据文件。

SVG 约定
--------

- ``viewBox="0 0 24 24"``,内容尽量填满 24×24。
- 所有可见图元统一 ``fill="#000000"``,即把 SVG 当作**单色蒙版**看。
  不要用 ``currentColor`` —— Qt 的 SVG 引擎不认它(实测会退回纯黑),
  真正的染色由本模块的 :func:`render_pixmap` 在栅格化之后完成。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

#: 图标源文件目录。用 ``__file__`` 定位而不是当前工作目录 —— 否则从别的目录
#: 启动应用(例如桌面快捷方式)就会找不到图标。
ICONS_DIR = Path(__file__).resolve().parent / "resources" / "icons"

#: 应用图标(窗口/任务栏)用的位图目录。SVG 做不了 Windows 的 ``.ico``。
APP_ICON_DIR = Path(__file__).resolve().parent / "resources" / "app"

#: 其他平台(主要是 Windows)的应用图标候选文件,按优先级排列。
#: ``.ico`` 内含 16~256 共 7 档尺寸,任务栏/标题栏要按 DPI 自己挑档。
_APP_ICON_NAMES = ("app.ico", "app.png", "app.svg")

#: macOS 的应用图标候选文件。macOS 的原生图标网格要求圆角方块只占画布约 80%
#: (四周留白给系统投影),而 Windows 按满幅图标设计 —— 同一份位图两边通用不了,
#: 所以 ``resources/app/`` 下各存一份(生成方式见 ``scripts/make_app_icon.py``)。
_APP_ICON_NAMES_MACOS = ("app-macos.png", "app.png", "app.svg")


# ================================================================ 调色板


@dataclass(frozen=True, slots=True)
class Palette:
    """一套界面配色。图标只从这里取色,换主题就是换一套 :class:`Palette`。"""

    text: str
    """主要文字/图标颜色。"""

    muted: str
    """次要文字、状态提示、非活跃图标。"""

    accent: str
    """强调色(选中、当前播放项)。"""

    disabled: str
    """禁用态图标。Qt 在 ``QIcon.Mode.Disabled`` 下不会自动灰化 SVG,
    所以禁用色要显式生成一份。"""

    on_accent: str
    """叠在强调色底上的图标颜色。"""


#: 应用调色板。
#:
#: **只有一套**。界面在设计上就是深色单主题(见 ``ui/theme.py`` 的说明),所以这里
#: 不再维护"深浅两套同色键"的映射表 —— 那种表只在能切换主题时才有意义,单主题下
#: 它只会退化成"一个键对一份值"的绕路。
DARK = Palette(
    text="#EAEAEA",
    muted="#9A9A9A",
    accent="#FB7299",
    disabled="#5A5A5A",
    on_accent="#FFFFFF",
)


# ================================================================ 资源定位


@lru_cache(maxsize=1)
def available_icons() -> tuple[str, ...]:
    """列出所有可用的图标名(不含扩展名,已排序)。"""
    return tuple(sorted(p.stem for p in ICONS_DIR.glob("*.svg")))


def icon_path(name: str) -> Path:
    """把图标名解析成磁盘路径。

    用 ``is_file()`` 而不是字符串拼接,顺带挡掉 ``../`` 这类路径穿越。

    Raises:
        ValueError: 图标不存在。
    """
    path = ICONS_DIR / f"{name}.svg"
    if not path.is_file():
        known = "、".join(available_icons()) or "(目录为空)"
        raise ValueError(f"没有叫 {name!r} 的图标。可用:{known}")
    return path


def app_icon_path(platform: str | None = None) -> Path | None:
    """定位当前平台该用的应用图标位图。

    Windows 用满幅的 ``app.ico``;macOS 用带留白的 ``app-macos.png`` —— macOS
    的图标网格把圆角方块限制在画布约 80%,满幅位图在 Dock 里会比系统图标大出
    一圈(见 ``scripts/make_app_icon.py`` 的 ``MACOS_ARTWORK_RATIO``)。

    Args:
        platform: ``sys.platform`` 的取值。``None`` 表示取当前平台;显式传入只是
            为了两个分支都能被单测覆盖(不能真去改测试进程的 ``sys.platform``)。

    Returns:
        该平台图标文件的路径。整条候选链都不存在时返回 ``None``,由调用方跳过
        设置图标 —— 资源缺失不该把应用拦在启动阶段。
    """
    current = sys.platform if platform is None else platform
    names = _APP_ICON_NAMES_MACOS if current == "darwin" else _APP_ICON_NAMES
    for name in names:
        candidate = APP_ICON_DIR / name
        if candidate.is_file():
            return candidate
    return None


# ================================================================ 栅格化与着色


def _require_gui_app() -> None:
    """栅格化必须有 ``QGuiApplication``;没有就给出人话提示。

    ``QPixmap`` 在 ``QGuiApplication`` 之前创建会直接段错误(不是异常),
    所以在入口处显式拦截。

    Raises:
        RuntimeError: 当前进程还没有 ``QApplication`` / ``QGuiApplication`` 实例。
    """
    if QGuiApplication.instance() is None:
        raise RuntimeError(
            "图标栅格化需要先创建 QApplication/QGuiApplication。"
            "请把 get_icon()/render_pixmap() 的调用放到应用启动之后。"
        )


@lru_cache(maxsize=512)
def _tinted_image(name: str, color: str, size: int) -> QImage:
    """把 SVG 渲染成 ``size×size`` 的图像并染成 ``color``。

    着色手法:先原样渲染(单色黑),再用 ``CompositionMode_SourceIn`` 铺一层目标色。
    ``SourceIn`` 只保留目标色的 alpha 通道,等于"把整张图当蒙版刷漆",
    所以 SVG 里本来画的是什么颜色并不重要,只要它有 alpha。

    用 ``lru_cache`` 缓存到 ``QImage`` 层(而不是 ``QPixmap``):``QImage`` 是
    与平台无关的图像数据,重复调用同一组参数就不会重复解析 SVG(实测解析
    一次约 1ms,而按钮每次改状态都要重取图标)。

    Raises:
        ValueError: 图标名不存在,或对应 SVG 无法解析。
        OSError: 图标文件读不出来(权限、被占用等)。
    """
    renderer = QSvgRenderer(QByteArray(icon_path(name).read_bytes()))
    if not renderer.isValid():
        raise ValueError(f"图标 {name!r} 的 SVG 无法解析:{icon_path(name)}")

    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    try:
        renderer.render(painter)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(image.rect(), QColor(color))
    finally:
        painter.end()
    return image


def render_pixmap(
    name: str,
    color: str,
    size: int = 24,
    *,
    device_pixel_ratio: float = 1.0,
) -> QPixmap:
    """渲染出染色后的 :class:`QPixmap`。

    Args:
        name: 图标名(``resources/icons/`` 下的文件名,不含扩展名)。
        color: 目标颜色,如 ``"#666666"``。
        size: 逻辑尺寸(像素)。
        device_pixel_ratio: 高分屏缩放比。传屏幕的 DPR 可得到原生分辨率的位图,
            避免 Qt 把 24px 的图放大后发虚。

    Raises:
        ValueError: 图标不存在或 SVG 无法解析。
        RuntimeError: 还没有 ``QGuiApplication``。
    """
    _require_gui_app()
    image = _tinted_image(name, color, size)
    pixmap = QPixmap.fromImage(image)
    if device_pixel_ratio != 1.0:
        pixmap.setDevicePixelRatio(device_pixel_ratio)
    return pixmap


def get_icon(
    name: str,
    color: str,
    size: int = 24,
    *,
    disabled_color: str | None = None,
    device_pixel_ratio: float | None = None,
) -> QIcon:
    """构造一个可直接交给 ``setIcon()`` 的 :class:`QIcon`。

    Args:
        name: 图标名。
        color: 正常态颜色。
        size: 逻辑尺寸。
        disabled_color: 禁用态颜色。给了就注册一份 ``QIcon.Mode.Disabled``
            变体 —— Qt 不会自动灰化 SVG,不给的话按钮禁用后图标仍会保持原色。
        device_pixel_ratio: 高分屏缩放比,``None`` 表示取主屏幕的实际值。

    Returns:
        每次调用返回**新的** ``QIcon``(Qt 会接管其生命周期),底层的染色位图
        则由 :func:`_tinted_image` 缓存,所以重复调用不会重复解析 SVG。
    """
    if device_pixel_ratio is None:
        screen = QGuiApplication.primaryScreen()
        device_pixel_ratio = screen.devicePixelRatio() if screen is not None else 1.0

    icon = QIcon(
        render_pixmap(name, color, size, device_pixel_ratio=device_pixel_ratio)
    )
    if disabled_color is not None:
        icon.addPixmap(
            render_pixmap(
                name, disabled_color, size, device_pixel_ratio=device_pixel_ratio
            ),
            QIcon.Mode.Disabled,
        )
    return icon


def clear_cache() -> None:
    """清空内部缓存。换主题批量重取图标,或测试之间需要隔离时用。"""
    _tinted_image.cache_clear()
    available_icons.cache_clear()


__all__ = [
    "DARK",
    "ICONS_DIR",
    "Palette",
    "available_icons",
    "clear_cache",
    "get_icon",
    "icon_path",
    "render_pixmap",
]
