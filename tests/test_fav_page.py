"""收藏夹页(`FavPage`)的单元测试:控件行为。

只测控件本身(填数据、过滤、空态、弱化行、信号转发、翻页按钮),不碰主窗口 ——
主窗口的接线在 ``tests/test_ui_wiring.py::TestAccountWiring`` 里验。

不触网、不出声:``FavPage`` 不发任何请求,数据全靠用例推下去。这里**没有**主窗口,
也就没有替身客户端可注入 —— 这正是这一页的设计目标(UI 只做展示与事件转发)。

离屏平台(``QT_QPA_PLATFORM=offscreen``)让 ``QWidget`` 在无显示器环境里也能构造。
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

from bilibili_music.api.bilibili import FavItem  # noqa: E402
from bilibili_music.ui.widgets import FavPage  # noqa: E402


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


def _item(
    bvid: str,
    title: str = "",
    *,
    author: str = "某UP",
    attr: int = 0,
    item_type: int = 2,
    page_count: int = 1,
    duration: int = 180,
) -> FavItem:
    """造一条收藏夹条目样本(``attr≠0`` 即失效,``type=2`` 是视频稿件)。"""
    return FavItem(
        bvid=bvid,
        title=title or f"收藏{bvid}",
        author=author,
        cover_url=f"https://i0.hdslb.com/{bvid}.jpg",
        duration=duration,
        page_count=page_count,
        attr=attr,
        item_type=item_type,
        fav_time=1700000000,
    )


class TestFavPageRender(unittest.TestCase):
    """列表渲染:行内容、弱化行、统计与空态。"""

    def _page(self) -> FavPage:
        """造一个空页面(不装封面加载器:封面与断言无关)。"""
        return FavPage()

    def test_rows_show_author_duration_and_page_count(self) -> None:
        """普通视频行:UP主、时长、多P时的分P数都要显示。"""
        page = self._page()
        page.set_folder("默认收藏夹", media_count=2)
        page.set_items([_item("BV1a"), _item("BV1p", page_count=100, duration=3600)])

        self.assertEqual(page.list.rowCount(), 2)
        self.assertEqual(page.list.title_at(0), "收藏BV1a")
        self.assertEqual(page.list.item(0, 2).text(), "某UP")
        self.assertEqual(page.list.item(0, 3).text(), "3:00")
        self.assertEqual(page.list.item(0, 4).text(), "")
        self.assertEqual(page.list.item(1, 4).text(), "P100")

    def test_dead_items_are_dimmed_and_labelled(self) -> None:
        """失效条目:画成弱化行,并在副标题里说明"已失效"。

        实测一个收藏夹一页 20 条里有 6 条失效 —— 它们必须留在列表里,但得一眼看出不能用。
        """
        page = self._page()
        page.set_items([_item("BV1ok"), _item("BV1dead", attr=9, duration=0)])

        self.assertFalse(page.list.is_row_dimmed(0))
        self.assertTrue(page.list.is_row_dimmed(1))
        self.assertEqual(page.list.subtitle_at(1), "已失效")
        # 失效条目常常拿不到时长,这时留空而不是显示 "0:00"(那看起来像"零秒的歌")
        self.assertEqual(page.list.item(1, 3).text(), "")

    def test_non_video_item_is_dimmed_with_its_own_reason(self) -> None:
        """音频 / 合集条目也要弱化,但原因与"失效"区分开。"""
        page = self._page()
        page.set_items([_item("", item_type=12, attr=0)])
        self.assertTrue(page.list.is_row_dimmed(0))
        self.assertIn("暂不支持", page.list.subtitle_at(0))

    def test_private_folder_marks_the_header(self) -> None:
        """私密收藏夹要在标题行标出来(侧栏太窄,记号放不下)。"""
        page = self._page()
        page.set_folder("私密夹", is_private=True, media_count=1)
        self.assertEqual(page.privacy_label.text(), "· 私密")
        page.set_folder("公开夹", is_private=False)
        self.assertEqual(page.privacy_label.text(), "")

    def test_summary_shows_loaded_over_total(self) -> None:
        """统计写"已加载 / 共多少":用户要能看出还没加载完。"""
        page = self._page()
        page.set_folder("夹子", media_count=128)
        page.set_items([_item("BV1a")])
        self.assertEqual(page.count_label.text(), "1 / 128 个内容")
        page.set_folder("空夹", media_count=0)
        page.set_items([])
        self.assertEqual(page.count_label.text(), "")

    def test_empty_states_differ_by_cause(self) -> None:
        """"还没选收藏夹""这个夹子是空的""过滤没匹配到"三种空态要分得清。"""
        page = self._page()
        self.assertIn("还没有选择收藏夹", page.empty_page.title_label.full_text())

        page.set_folder("空夹")
        page.set_items([])
        self.assertIn("这个收藏夹是空的", page.empty_page.title_label.full_text())

        page.set_items([_item("BV1a", "周杰伦 晴天")])
        page.filter_input.setText("不存在的关键字")
        self.assertIn("没有匹配", page.empty_page.title_label.full_text())

    def test_filter_matches_title_and_author(self) -> None:
        """过滤按标题或UP主,大小写不敏感;清空过滤框要恢复全部。"""
        page = self._page()
        page.set_items([_item("BV1a", "周杰伦 晴天", author="UP甲"), _item("BV1b", "夜曲", author="UP乙")])

        page.filter_input.setText("周杰伦")
        self.assertEqual([i.bvid for i in page.visible_entries()], ["BV1a"])
        page.filter_input.setText("up乙")
        self.assertEqual([i.bvid for i in page.visible_entries()], ["BV1b"])
        page.filter_input.setText("")
        self.assertEqual(len(page.visible_entries()), 2)


class TestFavPageActions(unittest.TestCase):
    """行操作:能播的发信号,不能播的发原因。"""

    def _page_with(self, *items: FavItem) -> FavPage:
        """造一个装了给定条目的页面。"""
        page = FavPage()
        page.set_folder("夹子", media_count=len(items))
        page.set_items(list(items))
        return page

    def test_playable_row_emits_activation(self) -> None:
        """能播的行:双击上报行号。"""
        page = self._page_with(_item("BV1ok"))
        got: list[int] = []
        page.row_activated.connect(got.append)
        page._on_activated(0)
        self.assertEqual(got, [0])

    def test_dead_row_emits_a_reason_instead_of_activation(self) -> None:
        """失效行:不发播放信号,而是发一条中文原因(界面直接显示它)。"""
        page = self._page_with(_item("BV1dead", "没了", attr=1))
        activated: list[int] = []
        reasons: list[str] = []
        page.row_activated.connect(activated.append)
        page.unplayable_requested.connect(reasons.append)

        page._on_activated(0)

        self.assertEqual(activated, [])
        self.assertEqual(len(reasons), 1)
        self.assertIn("失效", reasons[0])

    def test_dead_row_does_not_open_a_menu(self) -> None:
        """失效行连右键菜单都不弹:菜单里全是点不动的动作,不如直接说原因。"""
        from PySide6.QtCore import QPoint

        page = self._page_with(_item("BV1dead", attr=9))
        menus: list[tuple[int, QPoint]] = []
        reasons: list[str] = []
        page.row_menu_requested.connect(lambda row, pos: menus.append((row, pos)))
        page.unplayable_requested.connect(reasons.append)

        page._on_menu_requested(0, QPoint(1, 2))

        self.assertEqual(menus, [])
        self.assertEqual(len(reasons), 1)

    def test_non_video_row_is_rejected_with_its_own_wording(self) -> None:
        """音频条目被拒时,原因要说明"不是视频",而不是含糊的"暂不可用"。"""
        page = self._page_with(_item("", item_type=21))
        reasons: list[str] = []
        page.unplayable_requested.connect(reasons.append)
        page._on_add_requested(0)
        self.assertIn("不是视频稿件", reasons[0])

    def test_out_of_range_row_is_ignored(self) -> None:
        """越界行号不该抛异常(信号可能来自过滤前的旧列表)。"""
        page = self._page_with(_item("BV1ok"))
        activated: list[int] = []
        page.row_activated.connect(activated.append)
        page._on_activated(5)
        self.assertEqual(activated, [])


class TestFavPagePaging(unittest.TestCase):
    """"加载更多"的状态机。"""

    def test_load_more_button_visibility_follows_has_more(self) -> None:
        """没有下一页时按钮要隐藏;有下一页时可见。"""
        page = FavPage()
        page.set_folder("夹子", media_count=3)
        page.set_items([_item("BV1a")])

        page.set_has_more(False)
        self.assertTrue(page.load_more_button.isHidden())
        page.set_has_more(True)
        self.assertFalse(page.load_more_button.isHidden())

    def test_load_more_emits_only_once_while_loading(self) -> None:
        """请求在飞时重复点不该再发请求(否则一页会被拉好几次)。"""
        page = FavPage()
        page.set_folder("夹子", media_count=40)
        page.set_items([_item("BV1a")])
        page.set_has_more(True)

        got: list[bool] = []
        page.load_more_requested.connect(lambda: got.append(True))
        page.load_more_button.click()
        self.assertEqual(got, [True])

        page.set_loading(True)
        page.load_more_button.click()
        self.assertEqual(got, [True])
        self.assertEqual(page.load_more_button.text(), "加载中…")

        page.set_loading(False)
        page.load_more_button.click()
        self.assertEqual(got, [True, True])

    def test_append_keeps_existing_rows(self) -> None:
        """追加而不是替换:翻页不能把前一页的内容抹掉。"""
        page = FavPage()
        page.set_folder("夹子", media_count=3)
        page.set_items([_item("BV1a"), _item("BV1b")])
        page.append_items([_item("BV1c")])
        self.assertEqual(page.list.rowCount(), 3)
        self.assertEqual(page.entry_at(2).bvid, "BV1c")  # type: ignore[union-attr]

    def test_failure_message_shows_in_the_footer(self) -> None:
        """失败要说在页脚,而且列表内容保持不动(用户还能看已有的)。"""
        page = FavPage()
        page.set_folder("夹子", media_count=40)
        page.set_items([_item("BV1a")])
        page.set_has_more(True)
        page.set_failed("加载收藏夹失败:HTTP 412")
        self.assertIn("412", page.status_label.text())
        self.assertEqual(page.list.rowCount(), 1)

        # 下一次成功加载要把失败提示清掉,否则它会一直挂着(此时页脚改显示"还有 N 个")
        page.append_items([_item("BV1b")])
        self.assertNotIn("412", page.status_label.text())

    def test_playing_highlight_follows_bvid(self) -> None:
        """"正在播哪一条"按 ``bvid`` 找行(收藏夹条目是视频级的)。"""
        page = FavPage()
        page.set_items([_item("BV1a"), _item("BV1b")])
        page.set_playing("BV1b")
        self.assertEqual(page.list.highlighted_row(), 1)
        page.set_playing("")
        self.assertEqual(page.list.highlighted_row(), -1)

    def test_highlight_does_not_brighten_a_dead_row(self) -> None:
        """取消高亮后,失效行必须回到弱化色,不能跟着变回正常色。"""
        page = FavPage()
        page.set_items([_item("BV1a"), _item("BV1dead", attr=9)])
        page.set_playing("BV1a")
        page.set_playing("")
        self.assertTrue(page.list.is_row_dimmed(1))
        self.assertEqual(page.list._rows[1].title_label.styleSheet(), "color: #9A9A9A;")


if __name__ == "__main__":
    unittest.main(verbosity=2)
