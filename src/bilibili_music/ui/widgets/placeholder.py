"""未实现功能的占位页。

侧栏里"发现 / 本地缓存 / 我的歌单"这些入口在设计稿上有位置,但对应功能分别属于
路线图的 M3 / M2 / M5,**当前一律没有实现**(``AGENTS.md`` 第 1.4 节:不在路线图内的
不自行扩范围)。做成"点了没反应"或者干脆不画,用户只会以为程序坏了;这里给一个明确的
占位页:说清楚这一页叫什么、为什么还是空的。

页面内容可复用(:meth:`PlaceholderPage.set_content`),所以整个应用只需要一个实例,
切换入口时换标题与说明即可。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..icons import DARK, render_pixmap
from .elided_label import ElidedLabel

__all__ = ["PlaceholderPage"]


class PlaceholderPage(QWidget):
    """居中的"功能开发中"页面:图标 + 标题 + 一行说明。

    信号: 无。它不接受任何输入,只是个说明页。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """建好图标、标题与说明三行,标题内容先留空。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("CenterPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addStretch(1)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 占位图标用弱化色:它是背景装饰,不该比标题更抢眼
        self.icon_label.setPixmap(render_pixmap("music", DARK.disabled, 56))

        self.title_label = ElidedLabel("功能开发中")
        self.title_label.setObjectName("PageTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.hint_label = ElidedLabel("")
        self.hint_label.setObjectName("MutedLabel")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)
        layout.addStretch(2)

    def set_content(self, title: str, hint: str = "") -> None:
        """换这一页的标题与说明。

        Args:
            title: 页面标题,同时作为窗口里的定位文案(如 ``"发现"``)。
            hint: 补充说明这一页为什么是空的;空串表示不显示说明行。
        """
        self.title_label.setText(title)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))
