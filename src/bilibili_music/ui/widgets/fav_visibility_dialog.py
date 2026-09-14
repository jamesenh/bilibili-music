"""收藏夹「显示 / 隐藏」对话框:勾选哪些收藏夹出现在侧栏。

这一页要回答的是"我这一堆收藏夹里,哪几个真想天天看到"。它**不发请求、不写配置**:
拿一份收藏夹条目 + 当前被隐藏的 id,把用户改完的隐藏 id 交回去 —— 落盘与重画侧栏都是
``MainWindow`` 的事(``AGENTS.md`` 第 4 节:UI 只做展示与事件转发)。

设计取舍
--------

* **勾选态表示"显示"**:用户想的是"我要看到哪些",而不是"我要藏哪些"。默认全勾,
  取消勾选即隐藏 —— 这样弹窗打开时的画面与侧栏当前的样子是一致的,不用在脑子里做
  一次取反。
* **隐藏只影响本机侧栏**,B站 上的收藏夹一个都不会动。这件事必须写在弹窗里:用户的
  收藏夹是他真实的数据,看到"隐藏"两个字很自然会怀疑会不会删掉东西。
* **确定 / 取消两态,不做实时预览**:边勾边重建侧栏会让"取消"变得没有意义,而且用户
  正在浏览的收藏夹页会被反复触动。一次确定、一次重画,语义最清楚。
* **不做"隐藏全部"的拦截**:全隐藏是合法状态(用户可能只想先清空侧栏再逐个放开),
  侧栏那边会给一句"收藏夹都被隐藏了"的说明与恢复入口(见 ``Sidebar.set_playlists``
  的 ``empty_hint``)。
* **列表可滚动**:收藏夹实测有 16 个,再多就会把对话框撑出屏幕。限高之后一律在列表里
  滚动,弹窗高度因此是稳定的。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .sidebar import PlaylistEntry

__all__ = ["FavVisibilityDialog"]

#: 复选框列表的最高高度(像素)。超过就滚动,免得收藏夹一多,对话框比屏幕还高。
_LIST_MAX_HEIGHT = 320

#: 标题为空的收藏夹在弹窗里的占位文字。
#:
#: 实测账号里真的有空标题的夹子(见 ``sidebar.PlaylistEntry``):侧栏里它能靠内容条数
#: 认出来,这里光秃秃一个复选框就完全不知道是哪一条了。只有空白字符的标题按同一个口径
#: 处理 —— 那种标题在界面上与空的没有区别。
UNTITLED_FOLDER = "(未命名收藏夹)"


class FavVisibilityDialog(QDialog):
    """自定义「我的歌单」里显示哪些收藏夹。

    用法是"打开 → 用户改勾选 → 确定/取消",所以结果通过 :meth:`hidden_media_ids` 取,
    而不是发信号:取值只在 :meth:`QDialog.exec` 返回"确定"之后才有意义。

    Args:
        entries: 收藏夹条目,顺序与侧栏一致(接口返回的顺序)。
        hidden_ids: 当前被隐藏的 ``media_id``;不在里面的都算显示。
        parent: Qt 父对象。
    """

    def __init__(
        self,
        entries: Sequence[PlaylistEntry],
        hidden_ids: Collection[int],
        parent: QWidget | None = None,
    ) -> None:
        """按当前条目与被隐藏的 id 建好复选框列表。

        Args:
            entries: 收藏夹条目。
            hidden_ids: 当前被隐藏的 ``media_id``。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setWindowTitle("显示 / 隐藏收藏夹")
        self.setModal(True)
        self.setMinimumWidth(420)

        #: ``media_id`` -> 复选框。公开给上层与测试按 id 回查(与
        #: ``Sidebar.playlist_buttons`` 同一个口径:键由 id 说了算,名字可以重复)
        self.boxes: dict[int, QCheckBox] = {}
        self._entries = list(entries)
        hidden = {int(media_id) for media_id in hidden_ids}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("显示 / 隐藏收藏夹")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        hint = QLabel(
            "取消勾选的收藏夹不再出现在侧栏「我的歌单」里。"
            "这里只改本机的显示,B站 上的收藏夹不会被删除或修改。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addLayout(self._build_bulk_row())

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("FavVisibilityScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(_LIST_MAX_HEIGHT)
        self.scroll.setWidget(self._build_list(hidden))
        layout.addWidget(self.scroll, 1)

        self.empty_label = QLabel("这个账号下还没有收藏夹,先去B站收藏几个吧。")
        self.empty_label.setObjectName("MutedLabel")
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(not self._entries)
        layout.addWidget(self.empty_label)

        layout.addLayout(self._build_buttons())
        self._update_summary()

    # ------------------------------------------------------------ 构建界面

    def _build_bulk_row(self) -> QHBoxLayout:
        """建"全选 / 全不选"这一行 —— 收藏夹多的时候一个一个点太费事。"""
        row = QHBoxLayout()
        row.setSpacing(8)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedLabel")

        self.select_all_button = QPushButton("全选")
        self.select_all_button.setObjectName("GhostTextButton")
        self.select_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_button.clicked.connect(lambda: self._set_all(True))

        self.select_none_button = QPushButton("全不选")
        self.select_none_button.setObjectName("GhostTextButton")
        self.select_none_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_none_button.clicked.connect(lambda: self._set_all(False))

        # 条目为空时"全选 / 全不选"没有意义,按钮禁掉而不是留在那里点了没反应
        enabled = bool(self._entries)
        self.select_all_button.setEnabled(enabled)
        self.select_none_button.setEnabled(enabled)

        row.addWidget(self.summary_label, 1)
        row.addWidget(self.select_all_button)
        row.addWidget(self.select_none_button)
        return row

    def _build_list(self, hidden: set[int]) -> QWidget:
        """建可滚动的复选框列表(一个收藏夹一行)。

        Args:
            hidden: 当前被隐藏的 ``media_id``。

        Returns:
            放进 :class:`QScrollArea` 的内容控件。
        """
        panel = QWidget(self)
        panel.setObjectName("FavVisibilityViewport")
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        for entry in self._entries:
            check = QCheckBox(self._label_of(entry), panel)
            check.setObjectName("FavVisibilityCheck")
            check.setCursor(Qt.CursorShape.PointingHandCursor)
            check.setChecked(entry.media_id not in hidden)
            check.toggled.connect(self._update_summary)
            self.boxes[entry.media_id] = check
            box.addWidget(check)
        box.addStretch(1)
        return panel

    def _build_buttons(self) -> QHBoxLayout:
        """建"取消 / 确定"这一行。"""
        row = QHBoxLayout()
        row.addStretch(1)

        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.setObjectName("GhostTextButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)

        self.ok_button = QPushButton("确定", self)
        self.ok_button.setObjectName("PrimaryButton")
        self.ok_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ok_button.setDefault(True)
        self.ok_button.setEnabled(bool(self._entries))
        self.ok_button.clicked.connect(self.accept)

        row.addWidget(self.cancel_button)
        row.addWidget(self.ok_button)
        return row

    @staticmethod
    def _label_of(entry: PlaylistEntry) -> str:
        """拼一个复选框的文字:名字 + 内容条数 + 私密记号。

        记号在这里**要**写出来:同一个收藏夹在侧栏与收藏夹页都标了"私密",唯独挑
        "要不要显示它"的这一步不标,用户就没法按敏感程度做决定。

        Args:
            entry: 收藏夹条目。

        Returns:
            面向用户的一行文字。
        """
        parts = [entry.title.strip() or UNTITLED_FOLDER]
        if entry.media_count:
            parts.append(f"({entry.media_count} 个内容)")
        if entry.is_private:
            parts.append("· 私密")
        return " ".join(parts)

    # ------------------------------------------------------------ 结果

    def hidden_media_ids(self) -> tuple[int, ...]:
        """用户改完之后**被隐藏**的收藏夹 id(未勾选的那些)。

        Returns:
            按条目顺序排列的 ``media_id``;全部勾选时是空元组。
        """
        return tuple(
            entry.media_id
            for entry in self._entries
            if not self.boxes[entry.media_id].isChecked()
        )

    # ------------------------------------------------------------ 内部槽

    def _set_all(self, checked: bool) -> None:
        """把全部复选框设成同一个状态。

        Args:
            checked: ``True`` 全选(全部显示),``False`` 全不选(全部隐藏)。
        """
        for check in self.boxes.values():
            check.setChecked(checked)

    def _update_summary(self, *_args: object) -> None:
        """刷新"已隐藏几个"的说明(每次勾选都会走到这里)。

        参数用不到:状态在复选框自己身上。``toggled`` 会带一个 bool 过来,所以签名要
        能吃下它。
        """
        total = len(self.boxes)
        hidden = len(self.hidden_media_ids())
        if not total:
            self.summary_label.setText("")
        elif hidden:
            self.summary_label.setText(f"共 {total} 个,已隐藏 {hidden} 个")
        else:
            self.summary_label.setText(f"共 {total} 个,全部显示")
