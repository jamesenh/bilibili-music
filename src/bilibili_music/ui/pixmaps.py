"""位图加工:圆角封面、封面占位图与标题栏图标。

为什么单独一个模块
------------------

界面里"封面"出现在播放条、搜索结果行、队列行三处,每一处都要**等比裁切 + 圆角**
两件事。三处各写一遍 ``QPainter`` 裁剪,迟早出现"某处忘了裁切、图片把行高撑破"
这类不一致 —— 与 :mod:`bilibili_music.ui.icons` 把 SVG 染色集中起来是同一个理由。

圆角是怎么画的:给 ``QPainter`` 设一条圆角矩形裁剪路径再画原图。用 ``QPainterPath``
而不是"四角贴遮罩位图",是因为尺寸是运行时才知道的几档小尺寸(40/48),路径裁剪
在高分屏下不会有遮罩位图被拉伸的毛边。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap

from .icons import DARK, render_pixmap
from .theme import SURFACES

__all__ = [
    "COVER_RADIUS",
    "cover_pixmap",
    "logo_pixmap",
]

#: 封面缩略图的圆角半径(像素)。尺寸越大越不该是直角,但也不该圆成头像。
COVER_RADIUS = 6


def cover_pixmap(source: QPixmap | None, size: int, *, radius: int = COVER_RADIUS) -> QPixmap:
    """把封面加工成 ``size×size`` 的圆角缩略图。

    传入 ``None``(或空图)时**画一张占位图**而不是返回空位图:列表行与播放条的封面
    槽位始终有内容,布局不会因为"这一首没封面"而跳一下,调用方也不必到处判空。

    裁切用 ``KeepAspectRatioByExpanding`` + 居中取块:B站的封面比例五花八门(有 16:9
    也有 1:1),直接拉伸会把人脸压扁。

    Args:
        source: 原始封面;``None`` 或空图表示没有封面。
        size: 输出边长(逻辑像素)。
        radius: 圆角半径。

    Returns:
        新的 ``size×size`` 位图;占位图是深灰底 + 弱化色的音符。

    Raises:
        RuntimeError: 进程里还没有 ``QGuiApplication``(由
            :func:`~bilibili_music.ui.icons.render_pixmap` 抛出)。
    """
    # 先渲染音符图标:它内部会拦"没有 QGuiApplication"的情况,必须在建 QPixmap 之前调用
    note = render_pixmap("music", DARK.muted, max(16, size // 2))

    target = QPixmap(size, size)
    target.fill(Qt.GlobalColor.transparent)

    painter = QPainter(target)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
        painter.setClipPath(path)

        if source is None or source.isNull():
            painter.fillRect(QRectF(0, 0, size, size), QColor(SURFACES.field))
            painter.drawPixmap(
                (size - note.width()) // 2, (size - note.height()) // 2, note
            )
        else:
            scaled = source.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            # 居中取块:等比放大后总有一边多出来,把多出来的两边各裁掉一半
            painter.drawPixmap(
                -(scaled.width() - size) // 2, -(scaled.height() - size) // 2, scaled
            )
    finally:
        painter.end()
    return target


def logo_pixmap(size: int) -> QPixmap:
    """画标题栏左上角的应用图标:强调色圆角方块 + 白色音符。

    不新增图片资源:方块与音符都能用现成的 ``music.svg`` 与强调色拼出来,而拼出来的
    图标天然跟随主题色(资源图片则会在改配色时忘记同步)。

    Args:
        size: 输出边长(逻辑像素)。

    Returns:
        新的 ``size×size`` 位图。

    Raises:
        RuntimeError: 进程里还没有 ``QGuiApplication``。
    """
    note = render_pixmap("music", DARK.on_accent, int(size * 0.6))

    target = QPixmap(size, size)
    target.fill(Qt.GlobalColor.transparent)

    painter = QPainter(target)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DARK.accent))
        # 圆角取边长的 30%:比"正方形"更像应用图标,又不至于圆成圆形按钮
        radius = size * 0.3
        painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
        painter.drawPixmap(
            (size - note.width()) // 2, (size - note.height()) // 2, note
        )
    finally:
        painter.end()
    return target
