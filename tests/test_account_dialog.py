"""账号对话框(``AccountDialog``)的单元测试:输入校验与状态切换。

只测控件本身:它**不落盘、不发请求**(那是 ``MainWindow`` 的事),所以这里既没有替身
客户端也没有临时文件,只有一个用来显示的假路径。

不触网、不出声。离屏平台(``QT_QPA_PLATFORM=offscreen``)让 ``QWidget`` 能构造。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置,否则无显示环境下起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)
from PySide6.QtWidgets import QApplication  # noqa: E402

from bilibili_music.ui.widgets import AccountDialog  # noqa: E402

#: 假的凭据路径(只用于界面显示,本控件不读也不写它)。
_PATH = Path("C:/fake/BiliMusic/session.json")


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


class TestAccountDialog(unittest.TestCase):
    """账号对话框。"""

    def _dialog(self) -> AccountDialog:
        """造一个未登录状态的对话框。"""
        return AccountDialog(_PATH)

    def test_starts_in_the_login_state_and_shows_the_path(self) -> None:
        """初始是粘贴面板,并且**必须**明示凭据保存路径(AGENTS.md 第 5 节第 10 条)。"""
        dialog = self._dialog()
        self.assertTrue(dialog.login_panel.isVisibleTo(dialog))
        self.assertFalse(dialog.account_panel.isVisibleTo(dialog))
        self.assertIn(str(_PATH), dialog.path_label.text())

    def test_cookie_input_is_masked(self) -> None:
        """输入框要遮住取值:粘贴的是等价于密码的东西,不该摊在屏幕上。"""
        dialog = self._dialog()
        from PySide6.QtWidgets import QLineEdit

        self.assertEqual(dialog.cookie_input.echoMode(), QLineEdit.EchoMode.Password)

    def test_empty_input_reports_and_does_not_emit(self) -> None:
        """空输入:给提示,不发信号(省一次必然失败的请求)。"""
        dialog = self._dialog()
        got: list[str] = []
        dialog.login_requested.connect(got.append)
        dialog.login_button.click()
        self.assertEqual(got, [])
        self.assertIn("请先粘贴", dialog.error_label.text())

    def test_missing_sessdata_reports_and_does_not_emit(self) -> None:
        """只有 bili_jct 不算凭据:缺 SESSDATA 时当场说清楚,不发请求。"""
        dialog = self._dialog()
        got: list[str] = []
        dialog.login_requested.connect(got.append)
        dialog.cookie_input.setText("bili_jct=abc; DedeUserID=42")
        dialog.login_button.click()
        self.assertEqual(got, [])
        self.assertIn("SESSDATA", dialog.error_label.text())

    def test_valid_input_emits_raw_cookie_and_clears_the_field(self) -> None:
        """合法输入:原样上报 Cookie 头,并把输入框擦掉(少一份可被窥屏的副本)。"""
        dialog = self._dialog()
        got: list[str] = []
        dialog.login_requested.connect(got.append)
        dialog.cookie_input.setText("SESSDATA=a%2Cb; bili_jct=c")
        dialog.login_button.click()

        self.assertEqual(got, ["SESSDATA=a%2Cb; bili_jct=c"])
        self.assertEqual(dialog.cookie_text(), "")
        self.assertEqual(dialog.error_label.text(), "")

    def test_busy_disables_the_login_button(self) -> None:
        """校验期间按钮要禁用并换文案,免得用户连点出一串请求。"""
        dialog = self._dialog()
        dialog.set_busy(True)
        self.assertFalse(dialog.login_button.isEnabled())
        self.assertIn("正在登录", dialog.login_button.text())
        dialog.set_busy(False)
        self.assertTrue(dialog.login_button.isEnabled())

    def test_set_account_switches_panels(self) -> None:
        """登录后切到账号面板,并显示昵称与 mid。"""
        dialog = self._dialog()
        dialog.set_account("甲", 42)
        self.assertFalse(dialog.login_panel.isVisibleTo(dialog))
        self.assertTrue(dialog.account_panel.isVisibleTo(dialog))
        self.assertIn("甲", dialog.account_label.text())
        self.assertIn("42", dialog.account_label.text())

    def test_relogin_returns_to_the_login_panel(self) -> None:
        """"重新登录"要切回粘贴面板(并且把上一次的错误提示清掉)。"""
        dialog = self._dialog()
        dialog.set_account("甲", 42)
        dialog.error_label.setText("上一次的错误")
        dialog.relogin_button.click()
        self.assertTrue(dialog.login_panel.isVisibleTo(dialog))
        self.assertEqual(dialog.error_label.text(), "")

    def test_set_error_brings_back_the_login_panel(self) -> None:
        """校验失败时:即使当前在账号面板,也要切回粘贴面板并把原因写出来。"""
        dialog = self._dialog()
        dialog.set_account("甲", 42)
        dialog.set_error("这个凭据登录不了")
        self.assertTrue(dialog.login_panel.isVisibleTo(dialog))
        self.assertIn("登录不了", dialog.error_label.text())
        self.assertTrue(dialog.login_button.isEnabled())

    def test_logout_button_emits(self) -> None:
        """"登出"要有信号,不能是哑按钮。"""
        dialog = self._dialog()
        dialog.set_account("甲", 42)
        got: list[bool] = []
        dialog.logout_requested.connect(lambda: got.append(True))
        dialog.logout_button.click()
        self.assertEqual(got, [True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
