"""搜索结果与播放队列共用的曲目表格。

两者共用同一个控件是有意的:它们的差别只是列与数据来源,做成两个表格会立刻出现
"搜索列表能双击播放、队列列表忘了接"这类不一致,而这正是 MVP 单文件界面攒出来的
毛病。

控件只认**字符串单元格**与**行号**:它不 import ``Video`` / ``QueueItem``,
因为那些模型的展示规则(比如多P用分P标题)属于上层,不该让一个通用表格去认识。
调用方负责把模型转成行,控件只负责显示与把用户操作转成信号。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QWidget

__all__ = ["TrackList"]


class TrackList(QTableWidget):
    """一列曲目,支持双击激活与右键菜单。

    信号:
        row_activated(int): 双击(或回车)某一行,参数是行号(0 起)
        row_menu_requested(int, QPoint): 在某一行上请求右键菜单,参数是行号与全局坐标

    信号名带 ``row_`` 前缀是为了**避开基类已有的信号**:``QAbstractItemView``
    本身就有 ``activated(QModelIndex)``,同名覆盖会打断 Qt 内部转发。
    """

    row_activated = Signal(int)
    row_menu_requested = Signal(int, QPoint)

    def __init__(
        self,
        headers: Sequence[str],
        *,
        stretch_column: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        """建表并固定交互行为。

        Args:
            headers: 列标题,列数由它决定。
            stretch_column: 哪一列吃掉多余宽度;其余列按内容自适应。
            parent: Qt 父对象。
        """
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(list(headers))
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.setWordWrap(False)

        header = self.horizontalHeader()
        for column in range(len(headers)):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if column == stretch_column
                else QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(column, mode)

        self.doubleClicked.connect(self._on_double_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu_requested)

    # ------------------------------------------------------------ 填数据

    def set_rows(self, rows: Sequence[Sequence[str]]) -> None:
        """整体替换所有行(单元格不足的列留空,多余的列忽略)。

        Args:
            rows: 每行若干个单元格文本;行数可以变化。
        """
        self.setRowCount(0)
        for row in rows:
            index = self.rowCount()
            self.insertRow(index)
            for column, text in enumerate(row):
                if column >= self.columnCount():
                    break
                self.setItem(index, column, QTableWidgetItem(str(text)))

    def set_cell(self, row: int, column: int, text: str) -> None:
        """改写单个单元格(用于"分P数"这种稍后才补全的信息)。

        Args:
            row: 行号,越界时什么都不做。
            column: 列号,越界时什么都不做。
            text: 新的单元格文本。
        """
        if not 0 <= row < self.rowCount() or not 0 <= column < self.columnCount():
            return
        item = self.item(row, column)
        if item is None:
            self.setItem(row, column, QTableWidgetItem(text))
        else:
            item.setText(text)

    def set_highlight(self, row: int) -> None:
        """高亮某一行(当前播放项),并把视图滚到它上面。

        Args:
            row: 要突出的行号;传负数表示取消所有高亮。
        """
        for index in range(self.rowCount()):
            item = self.item(index, 0)
            if item is None:
                continue
            font = item.font()
            font.setBold(index == row)
            item.setFont(font)
        if 0 <= row < self.rowCount():
            self.scrollToItem(self.item(row, 0))

    def highlighted_row(self) -> int:
        """当前被加粗高亮的行号;没有则为 ``-1``。

        用于测试与界面自检 —— 否则"到底哪一行是当前播放"这个状态就只存在于字体里,
        外面无从断言。
        """
        for index in range(self.rowCount()):
            item = self.item(index, 0)
            if item is not None and item.font().bold():
                return index
        return -1

    # ------------------------------------------------------------ 内部槽

    def _on_double_clicked(self, index) -> None:  # noqa: ANN001 - QModelIndex
        """把 Qt 的 ``QModelIndex`` 折算成行号再发信号。"""
        self.row_activated.emit(index.row())

    def _on_menu_requested(self, position: QPoint) -> None:
        """把右键位置折算成"哪一行 + 全局坐标"。"""
        item = self.itemAt(position)
        if item is None:
            return
        self.row_menu_requested.emit(item.row(), self.viewport().mapToGlobal(position))
