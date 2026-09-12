"""从 ``app.svg`` 生成多尺寸 ``app.ico`` / ``app.png``。

为什么要手写 ICO 结构:Qt 的 ICO writer 只保存**最后一个** pixmap,指望它输出
多尺寸图标会得到只有一个 256×256 的文件 —— Windows 把它缩到标题栏的 16×16
就会发虚。所以这里按 ICO 规范自己拼目录 + PNG 数据(Windows Vista 起支持
ICO 内嵌 PNG,16/24/32 等小尺寸建议仍用 BMP,但现代 Windows 对 PNG 同样接受)。

用法::

    uv run python scripts/make_app_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# 导入顺序:先 QtGui 再 QtCore(与 scripts/_helpers.py 的说明一致)
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
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


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    """把 SVG 渲染成 ``size×size`` 的 PNG 字节。

    Args:
        renderer: 已加载 ``app.svg`` 的渲染器(复用同一个,避免每档尺寸重解析)。
        size: 目标边长(像素),正方形。

    Returns:
        可直接写进 ``.ico`` 的 PNG 编码字节。

    Raises:
        RuntimeError: Qt 的 PNG 编码器失败(实践中只会在内存耗尽时出现)。
    """
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter)
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
    """渲染所有尺寸并写出 ``app.ico`` / ``app.png``。

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
    png = APP_DIR / "app.png"
    png.write_bytes(largest_png)
    print(
        f"已写入 {png} ({png.stat().st_size} 字节, "
        f"{largest_size}x{largest_size})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
