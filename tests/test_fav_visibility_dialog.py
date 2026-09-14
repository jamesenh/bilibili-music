"""收藏夹「显示 / 隐藏」对话框(``FavVisibilityDialog``)的单元测试。

只测控件本身:它**不落盘、不发请求、不碰侧栏**(那是 ``MainWindow`` 的事),所以这里
既没有替身客户端也没有临时文件,只有几份手写的收藏夹条目。

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
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from bilibili_music.ui.widgets import FavVisibilityDialog, PlaylistEntry  # noqa: E402


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


def _entry(
    media_id: int,
    title: str = "",
    *,
    count: int = 0,
    private: bool = False,
) -> PlaylistEntry:
    """造一个收藏夹条目样本。"""
    return PlaylistEntry(
        media_id=media_id,
        title=title or f"夹子{media_id}",
        media_count=count,
        is_private=private,
    )


class TestFavVisibilityDialog(unittest.TestCase):
    """勾选列表的初始状态、结果与批量操作。"""

    def _dialog(self, *entries: PlaylistEntry, hidden: tuple[int, ...] = ()) -> FavVisibilityDialog:
        """按给定条目与被隐藏的 id 造一个对话框。"""
        return FavVisibilityDialog(list(entries), hidden)

    def test_checked_means_visible(self) -> None:
        """勾选态表示"显示":不在隐藏集合里的都要是勾上的。

        这是弹窗最容易写反的一处 —— 反了的话,用户一打开就看到与侧栏完全相反的画面,
        点"确定"会一次性把设置全部翻转。
        """
        dialog = self._dialog(_entry(1), _entry(2), _entry(3), hidden=(2,))
        self.assertTrue(dialog.boxes[1].isChecked())
        self.assertFalse(dialog.boxes[2].isChecked())
        self.assertTrue(dialog.boxes[3].isChecked())

    def test_hidden_ids_returns_the_unchecked_ones_in_order(self) -> None:
        """结果按条目顺序给出**未勾选**的 id(主窗口直接拿它写配置)。"""
        dialog = self._dialog(_entry(7), _entry(8), _entry(9))
        dialog.boxes[8].setChecked(False)
        dialog.boxes[9].setChecked(False)
        self.assertEqual(dialog.hidden_media_ids(), (8, 9))

        dialog.boxes[8].setChecked(True)
        self.assertEqual(dialog.hidden_media_ids(), (9,))

    def test_no_hidden_ids_when_everything_is_checked(self) -> None:
        """全勾时结果是空元组(而不是 None):主窗口要能直接落盘一个空列表。"""
        dialog = self._dialog(_entry(1), _entry(2))
        self.assertEqual(dialog.hidden_media_ids(), ())

    def test_labels_show_count_private_mark_and_untitled_placeholder(self) -> None:
        """每一行要能认出是哪个夹子:条数、私密记号,空标题要有占位文字。"""
        dialog = self._dialog(
            _entry(1, "默认收藏夹", count=128),
            _entry(2, "私密夹", private=True),
            _entry(3, title=" "),
        )
        self.assertIn("128", dialog.boxes[1].text())
        self.assertIn("私密", dialog.boxes[2].text())
        # 空标题在侧栏里还能靠条数认出来,这里光秃秃一行就完全不知道是哪一条
        self.assertIn("未命名", dialog.boxes[3].text())

    def test_summary_counts_the_hidden_ones(self) -> None:
        """"已隐藏几个"要跟着勾选实时变 —— 用户得知道这一下会藏掉多少。"""
        dialog = self._dialog(_entry(1), _entry(2), _entry(3))
        self.assertIn("全部显示", dialog.summary_label.text())

        dialog.boxes[2].setChecked(False)
        self.assertIn("已隐藏 1 个", dialog.summary_label.text())

        dialog.boxes[3].setChecked(False)
        self.assertIn("已隐藏 2 个", dialog.summary_label.text())

    def test_select_all_and_select_none(self) -> None:
        """「全选」「全不选」要一键改完所有勾选(收藏夹实测有十几个,逐个点太费事)。"""
        dialog = self._dialog(_entry(1), _entry(2), hidden=(1, 2))

        dialog.select_all_button.click()
        self.assertEqual(dialog.hidden_media_ids(), ())

        dialog.select_none_button.click()
        self.assertEqual(dialog.hidden_media_ids(), (1, 2))

    def test_empty_account_disables_the_actions(self) -> None:
        """一条收藏夹都没有时:给出说明,确定与批量按钮都不可点(点了也没意义)。"""
        dialog = self._dialog()
        self.assertTrue(dialog.empty_label.isVisibleTo(dialog))
        self.assertFalse(dialog.ok_button.isEnabled())
        self.assertFalse(dialog.select_all_button.isEnabled())
        self.assertFalse(dialog.select_none_button.isEnabled())
        self.assertEqual(dialog.summary_label.text(), "")

        # 取消仍然可用:否则用户会被一个关不掉的弹窗困住
        self.assertTrue(dialog.cancel_button.isEnabled())

    def test_empty_label_is_hidden_when_there_are_entries(self) -> None:
        """有条目时那句"还没有收藏夹"不该露出来。"""
        dialog = self._dialog(_entry(1))
        self.assertFalse(dialog.empty_label.isVisibleTo(dialog))

    def test_ok_button_accepts_and_cancel_button_rejects(self) -> None:
        """两个按钮要分别给出 Accepted / Rejected:主窗口只认前者。"""
        accepted = self._dialog(_entry(1))
        accepted.ok_button.click()
        self.assertEqual(accepted.result(), QDialog.DialogCode.Accepted)

        rejected = self._dialog(_entry(1))
        rejected.cancel_button.click()
        self.assertEqual(rejected.result(), QDialog.DialogCode.Rejected)

    def test_exec_returns_the_standard_accepted_code(self) -> None:
        """真跑一次模态循环:``exec()`` 的返回值要能直接与 ``QDialog.DialogCode.Accepted`` 比。

        主窗口的接线就是 ``if dialog.exec() != QDialog.DialogCode.Accepted: return``
        (见 ``MainWindow._open_fav_visibility_dialog``)。比不出来的话,"确定"会被当成
        "取消",用户的勾选静默丢掉 —— 所以这条要用真的 ``exec()`` 钉住,而不是靠替身。
        """
        dialog = self._dialog(_entry(1), hidden=(1,))
        # 用 0 延时的定时器自己点"确定":模态循环一起来就会处理它,用例不会挂住
        QTimer.singleShot(0, dialog.ok_button.click)
        self.assertEqual(dialog.exec(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.hidden_media_ids(), (1,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
