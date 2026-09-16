"""搜索历史下拉框(``SearchSuggest``)的单元测试:展开、过滤触发、点击与收起。

只测**控件自己**的行为:历史词由替身回调给(它记录"被按什么输入问过"),真实过滤规则
在 ``core/search_history.py`` 里另有用例。这样这里失败就说明是交互接线坏了,而不是过滤
规则变了。

离屏平台 + 进程内共享的 ``QApplication``(与 ``test_ui_widgets`` 同一套约定:必须用
``QApplication``,否则 widget 用例在全量跑时会突然起不来)。

四件事必须钉住(它们正是需求里的四条行为):

1. 点搜索框 / 获得焦点 → 展开,并**贴在搜索框正下方**;
2. 输入时按当前内容重新过滤,一条都没有时**收起**(不留空浮层);
3. 点一条历史词 → 收起并发出 ``term_activated``;
4. 点搜索框与下拉框之外 → 收起 + **清掉输入框焦点**(光标不再闪);焦点还在输入框里时
   再点搜索框要能重新展开(这一条不能只靠 ``FocusIn``)。
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
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget  # noqa: E402
from PySide6.QtGui import QFocusEvent  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from bilibili_music.ui.widgets.search_suggest import (  # noqa: E402
    SUGGEST_BORDER,
    SUGGEST_GAP,
    SUGGEST_MARGIN,
    SUGGEST_MIN_WIDTH,
    SUGGEST_ROW_HEIGHT,
    SUGGEST_SPACING,
    SearchSuggest,
)


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


class TestSearchSuggest(unittest.TestCase):
    """下拉框的交互行为。"""

    def setUp(self) -> None:
        """搭一个"宿主窗口 + 搜索框 + 下拉框"的最小现场并显示出来。

        必须 ``show()``:焦点相关的行为(``hasFocus``)只在可见窗口上有意义 —— 与
        ``test_ui_widgets`` 里测 ``ElidedLabel`` 省略时必须先 show 是同一个原因。
        """
        self.terms: list[str] = ["周杰伦 MV", "周杰伦 演唱会"]
        self.queries: list[str] = []

        self.host = QWidget()
        self.host.resize(600, 400)
        # 布局留成属性:测"焦点走到别的控件上"时要往里加一个控件(加进布局的控件才会
        # 被显示,而看不见的控件根本拿不到焦点,``setFocus()`` 会静默失败)
        self.layout = QVBoxLayout(self.host)
        self.line_edit = QLineEdit()
        self.layout.addWidget(self.line_edit)
        self.suggest = SearchSuggest(self.host)
        self.suggest.set_source(self._source)
        self.suggest.attach(self.line_edit)
        self.addCleanup(self.host.close)
        self.host.show()
        # 转一圈事件循环让窗口真正成为**活动窗口**:离屏平台下 ``show()`` 之后窗口还是
        # 非活动的,而焦点只对活动窗口有意义(``setFocus()`` 会静默地什么都不做,
        # 表现为所有焦点用例莫名其妙地失败)
        QApplication.processEvents()
        # 之前用例的窗口被关掉之后,离屏平台不会把新窗口自动设为活动窗口,得显式请求一次
        self.host.activateWindow()
        QApplication.processEvents()

    def _source(self, query: str) -> list[str]:
        """替身数据源:记下被问的输入,并返回用例摆好的那一份历史词。"""
        self.queries.append(query)
        return list(self.terms)

    # ------------------------------------------------------------ 展开

    def test_refresh_lists_terms_under_the_search_box(self) -> None:
        """展开后要贴着搜索框正下方、宽度与它对齐 —— 位置错了会挡住别的东西或飘到别处。"""
        self.suggest.refresh()
        self.assertTrue(self.suggest.is_open)
        self.assertEqual(self.suggest.terms(), ("周杰伦 MV", "周杰伦 演唱会"))
        self.assertEqual(self.suggest.x(), self.line_edit.x())
        self.assertLessEqual(
            self.suggest.x() + self.suggest.width(), self.host.width()
        )
        self.assertEqual(
            self.suggest.y(), self.line_edit.y() + self.line_edit.height() + SUGGEST_GAP
        )
        # 宽度跟搜索框走(只有一个最小宽度兜底)
        self.assertEqual(self.suggest.width(), max(SUGGEST_MIN_WIDTH, self.line_edit.width()))
        # 高度按行数算准:漏掉描边或间距,行会从框底溢出去
        expected = (
            2 * (SUGGEST_MARGIN + SUGGEST_BORDER)
            + 2 * SUGGEST_ROW_HEIGHT
            + SUGGEST_SPACING
        )
        self.assertEqual(self.suggest.height(), expected)

    def test_focus_in_opens_the_list(self) -> None:
        """用 Tab 进搜索框(没有鼠标点击)也要展开。"""
        other = QLineEdit()
        self.layout.addWidget(other)
        # 后加进布局的控件要等布局被激活才真正可见,而看不见的控件拿不到焦点
        other.show()
        QApplication.processEvents()
        other.setFocus()
        self.assertFalse(self.suggest.is_open)
        self.line_edit.setFocus(Qt.FocusReason.TabFocusReason)
        self.assertTrue(self.line_edit.hasFocus())
        self.assertTrue(self.suggest.is_open)

    def test_window_activation_focus_does_not_open_the_list(self) -> None:
        """窗口重新变成活动窗口时的 ``FocusIn`` **不算**用户要看历史。

        这一条是防"自己闪"的:切出去再切回来 Qt 会把焦点补回搜索框,那个 ``FocusIn`` 与
        "失焦就收起"凑起来就是"展开 → 收起 → 展开"的来回。
        """
        self.suggest.refresh()
        self.suggest.close_suggest()
        before = len(self.queries)
        event = QFocusEvent(
            QEvent.Type.FocusIn, Qt.FocusReason.ActiveWindowFocusReason
        )
        self.suggest.eventFilter(self.line_edit, event)
        self.assertFalse(self.suggest.is_open)
        self.assertEqual(len(self.queries), before)

    def test_clicking_the_search_box_reopens_after_it_was_closed(self) -> None:
        """焦点还在输入框里时再点它要能重新展开(FocusIn 不会再发,只能靠鼠标事件)。"""
        self.suggest.refresh()
        self.suggest.close_suggest()
        self.assertTrue(self.line_edit.hasFocus())
        QTest.mouseClick(self.line_edit, Qt.MouseButton.LeftButton)
        self.assertTrue(self.suggest.is_open)

    def test_empty_result_keeps_the_list_closed(self) -> None:
        """没有可显示的历史词时**不留空浮层**:用户还没搜过东西时不该弹一个空框。"""
        self.terms = []
        self.suggest.refresh()
        self.assertFalse(self.suggest.is_open)

    # ------------------------------------------------------------ 过滤触发

    def test_typing_asks_the_source_with_the_current_text(self) -> None:
        """用户每敲一个字,都要按输入框的当前内容重新问一次数据源。"""
        self.line_edit.setText("杰伦")
        self.line_edit.textEdited.emit("杰伦")
        self.assertEqual(self.queries[-1], "杰伦")
        self.assertTrue(self.suggest.is_open)

    def test_programmatic_set_text_does_not_re_filter(self) -> None:
        """用 ``setText`` 批量填词(点历史词走的就是这条路)不该触发重新过滤。

        否则:点一条历史词 → 填回输入框 → 立刻又按新文本展开一次,浮层刚关又冒出来。
        """
        self.suggest.refresh()
        before = len(self.queries)
        self.line_edit.setText("周杰伦 MV")
        self.assertEqual(len(self.queries), before)

    def test_unchanged_terms_are_not_rebuilt(self) -> None:
        """内容没变时不重建行:触发源里有成串到来的,重建一次就是一次可见的闪烁。"""
        self.suggest.refresh()
        first_row = self.suggest.rows[0]
        self.suggest.refresh()
        self.assertIs(self.suggest.rows[0], first_row)

    def test_long_term_does_not_widen_the_list(self) -> None:
        """超长历史词只能被省略,不能把下拉框撑得比搜索框还宽。"""
        self.terms = ["很长的历史搜索词" * 20]
        self.suggest.refresh()
        self.assertEqual(
            self.suggest.width(), max(SUGGEST_MIN_WIDTH, self.line_edit.width())
        )

    def test_closed_list_does_not_follow_host_resize(self) -> None:
        """关着的时候不动任何控件:跟尺寸/位置无关的窗口缩放不该引来一次布局折腾。"""
        before = self.suggest.size()
        self.host.resize(520, 400)
        QApplication.processEvents()
        self.assertEqual(self.suggest.size(), before)

    def test_empty_text_falls_back_to_the_full_list(self) -> None:
        """清空输入框后要重新给出全部历史(数据源那一侧按空串处理)。"""
        self.suggest.refresh()
        self.line_edit.setText("")
        self.line_edit.textEdited.emit("")
        self.assertEqual(self.queries[-1], "")
        self.assertTrue(self.suggest.is_open)

    # ------------------------------------------------------------ 点选与收起

    def test_clicking_a_term_emits_it_and_closes(self) -> None:
        """点一条历史词:收起浮层,并把**原文**(不是省略后的显示文本)送出去。"""
        self.suggest.refresh()
        activated: list[str] = []
        self.suggest.term_activated.connect(activated.append)
        self.suggest.rows[0].click()
        self.assertEqual(activated, ["周杰伦 MV"])
        self.assertFalse(self.suggest.is_open)

    def test_clicking_outside_closes_and_drops_focus(self) -> None:
        """点搜索框与下拉框之外:收起 + 取消输入框焦点(需求里的第 5 条)。"""
        self.suggest.refresh()
        self.line_edit.setFocus()
        self.assertTrue(self.line_edit.hasFocus())
        QTest.mouseClick(self.host, Qt.MouseButton.LeftButton, pos=QPoint(300, 300))
        self.assertFalse(self.suggest.is_open)
        self.assertFalse(self.line_edit.hasFocus())

    def test_clicking_inside_the_list_does_not_drop_focus(self) -> None:
        """点下拉框自己不算"点到外面":不能把输入框焦点清掉(否则紧接着的输入就丢了)。"""
        self.suggest.refresh()
        self.line_edit.setFocus()
        QTest.mouseClick(
            self.suggest.rows[1], Qt.MouseButton.LeftButton, pos=QPoint(10, 10)
        )
        self.assertTrue(self.line_edit.hasFocus())

    def test_focus_out_closes_the_list(self) -> None:
        """焦点自己走了(Tab 到别的控件)也要收起,不能留一个悬空的浮层。"""
        other = QLineEdit()
        self.layout.addWidget(other)
        # 后加进布局的控件要等布局被激活才真正可见,而看不见的控件拿不到焦点
        other.show()
        QApplication.processEvents()
        self.suggest.refresh()
        self.line_edit.setFocus()
        self.assertTrue(self.suggest.is_open)
        other.setFocus()
        self.assertFalse(self.line_edit.hasFocus())
        self.assertFalse(self.suggest.is_open)

    def test_resize_moves_the_list_to_follow_the_search_box(self) -> None:
        """宿主窗口变宽后下拉框要跟着锚点重新贴一次,不能留在旧位置与旧宽度。"""
        self.suggest.refresh()
        before = self.line_edit.width()
        self.host.resize(900, 400)
        # 布局请求是投递事件:不转一圈事件循环,搜索框还是旧宽度,断言就测不到东西
        QApplication.processEvents()
        self.assertGreater(self.line_edit.width(), before)
        self.assertEqual(
            self.suggest.y(), self.line_edit.y() + self.line_edit.height() + SUGGEST_GAP
        )
        self.assertEqual(
            self.suggest.width(), max(SUGGEST_MIN_WIDTH, self.line_edit.width())
        )


if __name__ == "__main__":
    unittest.main()
