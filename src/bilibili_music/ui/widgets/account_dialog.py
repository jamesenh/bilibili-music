"""账号对话框:粘贴 Cookie 登录、查看当前账号、登出。

为什么这一版是"粘贴 Cookie"而不是扫码
--------------------------------------

扫码登录必须在界面上画二维码,而 PySide6 6.8.3 **没有**二维码编码能力(实测:
``QtGui`` 里没有任何 QR 相关类,项目环境里也没有 ``qrcode`` / ``segno`` / ``PIL``)。
要做得么新增第三方依赖、要么手写一个 QR 编码器 —— 按 ``AGENTS.md`` 第 3 节
("新增依赖前先问"),这一版先做零依赖的 Cookie 粘贴,扫码留给 M5 S2。

凭据纪律(``AGENTS.md`` 第 5 节第 10 条)
----------------------------------------

* 输入框用 :attr:`QLineEdit.EchoMode.Password` 遮住取值:粘贴的是等价于密码的东西,
  不该直接摊在屏幕上。
* **必须明示落盘路径** —— 用户选了明文保存,就得让他知道凭据存在哪个文件里。
* 对话框自己不落盘、不发请求、不判断"这个 Cookie 能不能用":它只负责**收集与展示**,
  校验登录态、写文件、注入网络层全是 ``MainWindow`` 的事(第 4 节:UI 不做业务逻辑)。

信号:
    login_requested(str): 用户点了"登录",携带粘贴的 Cookie 头**原文**
    logout_requested(): 用户点了"登出"
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.session import parse_cookie_header

__all__ = ["AccountDialog"]

#: 说明文字:告诉用户去哪里复制那一行 Cookie。
_HOWTO = (
    "在浏览器里登录B站后,按 F12 打开开发者工具 → Network → 随便点一个 bilibili.com 的请求 →"
    "在请求头里找到 Cookie 这一行,整行复制过来。"
)


class AccountDialog(QDialog):
    """账号对话框(未登录时是粘贴框,已登录时是账号信息 + 登出)。

    信号:
        login_requested(str): 点了"登录",携带 Cookie 头原文
        logout_requested(): 点了"登出"

    Args:
        path: 凭据文件的保存路径(只用于**显示**,本对话框不读写文件)。
        parent: Qt 父对象。
    """

    login_requested = Signal(str)
    logout_requested = Signal()

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        """建好两个状态的面板(同一时间只显示一个)。

        Args:
            path: 凭据文件路径,用于界面明示。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setWindowTitle("B站账号")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        self.title_label = QLabel("登录B站账号")
        self.title_label.setObjectName("PageTitle")
        layout.addWidget(self.title_label)

        #: 凭据保存路径。**必须显示**:用户选了明文保存,就该知道文件在哪
        self.path_label = QLabel(f"凭据将明文保存在:{path}")
        self.path_label.setObjectName("MutedLabel")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.path_label)

        layout.addWidget(self._build_login_panel())
        layout.addWidget(self._build_account_panel())
        layout.addStretch(1)

        self.set_account("", 0)

    # ------------------------------------------------------------ 构建界面

    def _build_login_panel(self) -> QWidget:
        """建"说明 + Cookie 输入框 + 错误提示 + 按钮"这一面板。"""
        panel = QWidget(self)
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        howto = QLabel(_HOWTO)
        howto.setObjectName("MutedLabel")
        howto.setWordWrap(True)
        box.addWidget(howto)

        self.cookie_input = QLineEdit(panel)
        self.cookie_input.setObjectName("FilterInput")
        self.cookie_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie_input.setPlaceholderText("SESSDATA=...; bili_jct=...; DedeUserID=...")
        self.cookie_input.returnPressed.connect(self._on_login_clicked)
        box.addWidget(self.cookie_input)

        self.error_label = QLabel("")
        self.error_label.setObjectName("StatusLabel")
        self.error_label.setWordWrap(True)
        box.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消", panel)
        self.cancel_button.setObjectName("GhostTextButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)
        self.login_button = QPushButton("登录", panel)
        self.login_button.setObjectName("PrimaryButton")
        self.login_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.login_button.setDefault(True)
        self.login_button.clicked.connect(self._on_login_clicked)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.login_button)
        box.addLayout(buttons)

        self.login_panel = panel
        return panel

    def _build_account_panel(self) -> QWidget:
        """建"当前账号 + 登出 / 重新登录"这一面板。"""
        panel = QWidget(self)
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self.account_label = QLabel("")
        self.account_label.setObjectName("PageQuery")
        self.account_label.setWordWrap(True)
        box.addWidget(self.account_label)

        self.notice_label = QLabel("登出会删除本机保存的凭据文件。")
        self.notice_label.setObjectName("MutedLabel")
        self.notice_label.setWordWrap(True)
        box.addWidget(self.notice_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.relogin_button = QPushButton("重新登录", panel)
        self.relogin_button.setObjectName("GhostTextButton")
        self.relogin_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.relogin_button.clicked.connect(self._show_login_panel)
        self.logout_button = QPushButton("登出", panel)
        self.logout_button.setObjectName("PrimaryButton")
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_button.clicked.connect(self._on_logout_clicked)
        buttons.addWidget(self.relogin_button)
        buttons.addWidget(self.logout_button)
        box.addLayout(buttons)

        self.account_panel = panel
        return panel

    # ------------------------------------------------------------ 上层推状态

    def set_account(self, uname: str, mid: int) -> None:
        """切换成"已登录 / 未登录"两种状态之一。

        Args:
            uname: 账号昵称;空串表示未登录(切到粘贴面板)。
            mid: 账号 mid,仅用于显示。
        """
        logged_in = bool(uname.strip())
        self.login_panel.setVisible(not logged_in)
        self.account_panel.setVisible(logged_in)
        self.title_label.setText("B站账号" if logged_in else "登录B站账号")
        if logged_in:
            self.account_label.setText(f"当前账号:{uname}(mid {mid})")
            self.error_label.setText("")
        else:
            self.error_label.setText("")
            self.cookie_input.clear()

    def set_error(self, message: str) -> None:
        """在登录面板里显示一条错误(例如"这个凭据登录不了")。

        Args:
            message: 中文提示;空串表示清除。
        """
        self._show_login_panel()
        self.error_label.setText(message)
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        """设置"正在校验凭据",期间禁用登录按钮。

        Args:
            busy: 是否正在校验。
        """
        self.login_button.setEnabled(not busy)
        self.login_button.setText("正在登录…" if busy else "登录")

    def cookie_text(self) -> str:
        """当前输入框里的原文(测试与 ``MainWindow`` 都通过信号拿它,这里只为断言方便)。"""
        return self.cookie_input.text()

    # ------------------------------------------------------------ 内部槽

    def _show_login_panel(self) -> None:
        """切回粘贴面板(点"重新登录"时用)。"""
        self.set_account("", 0)

    def _on_login_clicked(self) -> None:
        """校验输入里**有没有**凭据,然后上报。

        这里只做"有没有"这一层判断(纯字符串):缺 ``SESSDATA`` 时连请求都不用发,
        直接告诉用户粘错了。至于"这个凭据在服务端还有效吗"必须由 ``MainWindow`` 发请求
        去问 —— 收藏夹接口在未登录时也返回 ``code=0``,拿它判断会得到假阳性
        (见 README「账号链路」)。
        """
        raw = self.cookie_input.text().strip()
        if not raw:
            self.error_label.setText("请先粘贴 Cookie")
            return
        if not parse_cookie_header(raw).get("SESSDATA"):
            self.error_label.setText(
                "这行 Cookie 里没有 SESSDATA —— 请复制请求头里完整的那一行"
            )
            return
        self.error_label.setText("")
        # 先把输入框擦掉再上报:凭据已经在信号参数里了,留在控件上只会多一份可被窥屏的副本
        self.cookie_input.clear()
        self.set_busy(True)
        self.login_requested.emit(raw)

    def _on_logout_clicked(self) -> None:
        """点了"登出"。"""
        self.logout_requested.emit()
