"""无边框窗口:自绘标题栏 + 8 个边缘缩放把手。

窗口去掉了系统边框(设计稿的标题栏是自绘的),于是"拖边框缩放"这件事也得自己补回来 ——
否则只剩最大化/还原能改变窗口大小,那是个很明显的残缺。

为什么用 8 个隐形把手,而不是 ``nativeEvent`` 里处理 ``WM_NCHITTEST``:
后者是 Windows 专有代码(还要挑 32/64 位与 DPI 的坑),而这里只需要在每个边缘/角上放
一块 6px 宽的透明控件,按下时调 ``QWindow.startSystemResize()`` —— 缩放的**实际行为**
仍然交给平台,所以拖动时的实时预览、最小尺寸约束、跨屏表现都与原生窗口一致。

把手做成"隐形"靠的是不给它设任何背景:没有样式表规则命中它、也没开
``autoFillBackground``,``QWidget`` 就不会画任何东西。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QMainWindow, QWidget

__all__ = ["FramelessWindow", "RESIZE_GRIP"]

#: 缩放把手的厚度(像素)。再宽会开始挡住内容,再窄就不好抓。
RESIZE_GRIP = 6

_LEFT = Qt.Edge.LeftEdge
_TOP = Qt.Edge.TopEdge
_RIGHT = Qt.Edge.RightEdge
_BOTTOM = Qt.Edge.BottomEdge

#: 8 个把手的规格 ``(边缘标志, 光标, 水平位置, 垂直位置)``。
#:
#: 位置取值是 ``left``/``center``/``right`` 与 ``top``/``center``/``bottom``;
#: 光标方向按"拖这一角能往哪拉"给,左上/右下用 ``SizeFDiagCursor``,右上/左下用
#: ``SizeBDiagCursor``。
_GRIP_SPECS: tuple[tuple[Qt.Edges, Qt.CursorShape, str, str], ...] = (
    (_LEFT | _TOP, Qt.CursorShape.SizeFDiagCursor, "left", "top"),
    (_TOP, Qt.CursorShape.SizeVerCursor, "center", "top"),
    (_RIGHT | _TOP, Qt.CursorShape.SizeBDiagCursor, "right", "top"),
    (_RIGHT, Qt.CursorShape.SizeHorCursor, "right", "center"),
    (_RIGHT | _BOTTOM, Qt.CursorShape.SizeFDiagCursor, "right", "bottom"),
    (_BOTTOM, Qt.CursorShape.SizeVerCursor, "center", "bottom"),
    (_LEFT | _BOTTOM, Qt.CursorShape.SizeBDiagCursor, "left", "bottom"),
    (_LEFT, Qt.CursorShape.SizeHorCursor, "left", "center"),
)


class _ResizeGrip(QWidget):
    """窗口边缘上一块透明的缩放把手。

    Args:
        edges: 这块把手覆盖的边缘组合(角上是两条边相或)。
        cursor: 悬停时的光标形状。
        parent: 宿主窗口。
    """

    def __init__(self, edges: Qt.Edges, cursor: Qt.CursorShape, parent: QWidget) -> None:
        """记录边缘组合并设置光标(不画任何东西)。"""
        super().__init__(parent)
        self._edges = edges
        self.setCursor(cursor)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802 - QMouseEvent / Qt 命名
        """按下左键时请平台开始缩放;平台不接手就放行给下面的控件。

        放行(``ignore``)很重要:离屏平台拿不到窗口句柄,若这里硬吃掉事件,鼠标事件
        就永远到不了它该去的地方。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemResize(self._edges):
                event.accept()
                return
        event.ignore()


class FramelessWindow(QMainWindow):
    """无系统边框、靠自绘标题栏与边缘把手操作的主窗口。

    子类负责填内容;缩放把手的定位与"最大化时隐藏"由本类处理。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """去掉系统边框并建好 8 个缩放把手。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._grips = [
            _ResizeGrip(edges, cursor, self) for edges, cursor, _, _ in _GRIP_SPECS
        ]
        self._layout_grips()

    # ------------------------------------------------------------ 把手布局

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - QResizeEvent / Qt 命名
        """窗口尺寸变了就重新贴边摆放把手。"""
        super().resizeEvent(event)
        self._layout_grips()

    def changeEvent(self, event) -> None:  # noqa: ANN001, N802 - QEvent / Qt 命名
        """最大化/全屏时收起把手:那时候窗口尺寸不归用户拖。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_grips()

    def _layout_grips(self) -> None:
        """把 8 个把手摆到各自的边缘/角上,并抬到最上层。"""
        width, height = self.width(), self.height()
        for (_, _, horizontal, vertical), grip in zip(_GRIP_SPECS, self._grips):
            x = _place(horizontal, width, "left", "right")
            y = _place(vertical, height, "top", "bottom")
            grip.setGeometry(x, y, RESIZE_GRIP, RESIZE_GRIP)
            grip.raise_()
        self._sync_grips()

    def _sync_grips(self) -> None:
        """按当前窗口状态显示/隐藏把手。"""
        visible = not (self.isMaximized() or self.isFullScreen())
        for grip in self._grips:
            grip.setVisible(visible)


def _place(position: str, total: int, low: str, high: str) -> int:
    """把一个把手在某一维上的坐标算出来。

    Args:
        position: ``low`` / ``high`` / 其它(居中)。
        total: 窗口在该维上的尺寸。
        low: "贴起始边"的取值名(水平是 ``left``,垂直是 ``top``)。
        high: "贴结束边"的取值名。

    Returns:
        该维上的起始坐标(保证不会算出负数)。
    """
    if position == low:
        return 0
    if position == high:
        return max(0, total - RESIZE_GRIP)
    return max(0, (total - RESIZE_GRIP) // 2)
