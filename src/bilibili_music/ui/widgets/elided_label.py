"""会自动省略过长文字的 ``QLabel``。

**为什么需要它**:``QTableWidgetItem`` 的文字由 item delegate 绘制,delegate 自带
"放不下就加省略号"的行为;而放进单元格里的 ``QLabel``(封面 + 标题那类富文本行)
没有这个能力,B站标题又普遍很长 —— 结果就是文字被硬生生切掉,没有省略号,看起来像坏了。

省略在 ``resizeEvent`` 里做:控件宽度是布局算出来的,只有那一刻才知道能放多少字。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QWidget

__all__ = ["ElidedLabel"]


class ElidedLabel(QLabel):
    """显示时按控件宽度省略、但保留完整文本的标签。

    ``full_text()`` 始终返回完整内容,工具提示也挂的是完整内容 —— 界面上截断不等于
    信息丢失。

    Args:
        text: 完整文本。
        parent: Qt 父对象。
        mode: 省略方式,默认从右侧省略(标题的场景)。
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        """记录完整文本并先按原样显示(此时宽度还没定,先不省略)。"""
        super().__init__(parent)
        self._full_text = ""
        self._mode = mode
        self.setText(text)

    def full_text(self) -> str:
        """当前完整文本(未被省略的原文)。"""
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        """替换文本;内部会把**完整文本**记住,并按当前宽度重新省略。"""
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        super().setText(self._full_text)
        self._refresh()

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - QResizeEvent / Qt 命名
        """宽度变了就重新算一次省略位置。"""
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        """按当前宽度把显示文本替换成省略后的版本。"""
        width = self.width()
        if width <= 0:
            return  # 还没被布局过,等 resizeEvent
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full_text, self._mode, width))
