"""从 ``app.svg`` 生成 ``app.ico`` / ``app.png`` / ``app-macos.png``。

为什么要手写 ICO 结构:Qt 的 ICO writer 只保存**最后一个** pixmap,指望它输出
多尺寸图标会得到只有一个 256×256 的文件 —— Windows 把它缩到标题栏的 16×16
就会发虚。所以这里按 ICO 规范自己拼目录 + PNG 数据(Windows Vista 起支持
ICO 内嵌 PNG,16/24/32 等小尺寸建议仍用 BMP,但现代 Windows 对 PNG 同样接受)。

为什么 macOS 要单独渲染一份 PNG:macOS 不吃 ``.ico``,而且它的原生图标网格要求
圆角方块本体只占画布约 80%("四周留白":``MACOS_ARTWORK_RATIO``),而 Windows 的
任务栏/资源管理器按满幅图标设计。同一份位图满足不了两边,所以从同一个 SVG 渲染
出两份资产,由 ``ui/icons.py::app_icon_path`` 按平台挑(那份文件里有选择逻辑)。
脚本本身**不判断平台**:两份资产一次都生成,免得换个系统再生成时漏掉其中一份。

用法::

    uv run python scripts/make_app_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# 导入顺序:先 QtGui 再 QtCore(与 scripts/_helpers.py 的说明一致)
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

#: 仓库根目录。脚本要能独立运行,所以用 ``__file__`` 反推而不是依赖当前工作目录。
ROOT = Path(__file__).resolve().parent.parent
#: 图标资源的落盘目录(与 ``ui/icons.py`` 的 ``APP_ICON_DIR`` 指向同一处)。
APP_DIR = ROOT / "src" / "bilibili_music" / "ui" / "resources" / "app"
#: 矢量源文件。它才是唯一需要手工维护的图标源,ico/png 都是生成物。
SRC_SVG = APP_DIR / "app.svg"

#: 写进 .ico 的尺寸档位。覆盖标题栏(16)、任务栏(32/48)、资源管理器大图标(256)。
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: 满幅位图的文件名(Windows 及 macOS 之外的平台用),它同时是 .ico 里最大那档。
FULL_BLEED_PNG_NAME = "app.png"
#: macOS 专用位图的文件名。macOS 不吃 ``.ico``,而且需要四周留白,无法与上面共用。
MACOS_PNG_NAME = "app-macos.png"

#: macOS 位图的边长。Dock 最大 128pt、Retina 下要 256px,取 1024 是为了和 Apple
#: 自己 icns 里最大的 ``512@2x`` 对齐(「关于」窗口、Spotlight 等还会更大),
#: 这份图形很简单,再大只是白占体积。
MACOS_PNG_SIZE = 1024

#: macOS 图标网格:圆角方块本体只占画布的这一比例,四周留白是给系统投影用的。
#: ``824/1024`` 取自 Apple 的图标模板;本机实测系统自带的 Music.app / Finder.app
#: 图标实心区域正好是画布的 80.5%,而 ``app.svg`` 是满幅 100% —— 直接拿来用会比
#: 系统图标大出约 24%(线性尺寸),在 Dock 里一眼就能看出偏大。
MACOS_ARTWORK_RATIO = 824 / 1024


def render_png(
    renderer: QSvgRenderer, size: int, artwork_ratio: float = 1.0
) -> bytes:
    """把 SVG 渲染成 ``size×size`` 的 PNG 字节。

    Args:
        renderer: 已加载 ``app.svg`` 的渲染器(复用同一个,避免每档尺寸重解析)。
        size: 目标边长(像素),正方形。
        artwork_ratio: 画面本体占画布边长的比例,``1.0`` 表示满幅。小于 1 时
            四周各留一圈透明边(用于 :data:`MACOS_ARTWORK_RATIO`)。

    Returns:
        可直接写进 ``.ico`` 的 PNG 编码字节。

    Raises:
        RuntimeError: Qt 的 PNG 编码器失败(实践中只会在内存耗尽时出现)。
    """
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        if artwork_ratio >= 1.0:
            renderer.render(painter)
        else:
            # 留白按分数算,所以可能落在半像素上(1024 画布下正好是 100,但换个
            # 尺寸就不一定)。用 QRectF 而不是整数 QRect:两侧各自取整会让左右
            # 留白差 1px,图标在 Dock 里会有肉眼可见的偏移。
            inset = size * (1.0 - artwork_ratio) / 2.0
            renderer.render(
                painter, QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
            )
    finally:
        painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"{size}x{size} PNG 编码失败")
    return bytes(buffer.data())


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """按 ICO 规范拼装:6 字节文件头 + 每图 16 字节目录项 + 图像数据。

    Args:
        images: ``(边长, PNG 字节)`` 列表,顺序即目录项顺序。

    Returns:
        完整的 ``.ico`` 文件字节(ICONDIR + ICONDIRENTRY[] + PNG 数据区)。
    """
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)

    directory = bytearray()
    payload = bytearray()
    for size, data in images:
        # 目录项里 0 表示 256(该字段只有 1 字节)
        dim = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset
        )
        payload += data
        offset += len(data)

    return header + bytes(directory) + bytes(payload)


def main() -> int:
    """渲染所有尺寸并写出 ``app.ico`` / ``app.png`` / ``app-macos.png``。

    Returns:
        进程退出码:``0`` 成功;``1`` 表示源 SVG 缺失或无法解析。
    """
    QGuiApplication(sys.argv)

    if not SRC_SVG.is_file():
        print(f"[FAIL] 找不到源文件 {SRC_SVG}")
        return 1

    renderer = QSvgRenderer(QByteArray(SRC_SVG.read_bytes()))
    if not renderer.isValid():
        print(f"[FAIL] {SRC_SVG} 不是有效的 SVG")
        return 1

    images = [(s, render_png(renderer, s)) for s in SIZES]

    ico = APP_DIR / "app.ico"
    ico.write_bytes(build_ico(images))
    print(f"已写入 {ico} ({ico.stat().st_size} 字节, {len(images)} 档尺寸)")

    largest_size, largest_png = max(images, key=lambda item: item[0])
    png = APP_DIR / FULL_BLEED_PNG_NAME
    png.write_bytes(largest_png)
    print(
        f"已写入 {png} ({png.stat().st_size} 字节, "
        f"{largest_size}x{largest_size})"
    )

    # macOS 专用:同一份 SVG,但按图标网格缩到 80.5% 并留出透明边
    macos = APP_DIR / MACOS_PNG_NAME
    macos.write_bytes(render_png(renderer, MACOS_PNG_SIZE, MACOS_ARTWORK_RATIO))
    print(
        f"已写入 {macos} ({macos.stat().st_size} 字节, "
        f"{MACOS_PNG_SIZE}x{MACOS_PNG_SIZE}, 画面占 {MACOS_ARTWORK_RATIO:.1%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
