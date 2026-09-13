"""新控件与纯展示逻辑的单元测试。

覆盖这一轮改版新增/改写的部件:

* ``split_title_prefix`` —— 标题前缀拆分(纯函数)
* ``ElidedLabel`` —— 按宽度省略但保留完整文本
* ``TrackList`` —— 富信息行、封面、操作列、高亮与"特殊列不可被覆盖"
* ``PageSelector`` —— 播放条上的分P选择器:禁用规则、标签省略、弹出菜单的内容与位置
* ``Sidebar`` —— 导航信号、页面/歌单互斥、队列开关同步
* ``TitleBar`` —— 搜索与窗口按钮信号、最大化按钮外观
* ``PlaceholderPage`` —— 占位内容可替换
* ``FramelessWindow`` —— 去掉系统边框并备好 8 个缩放把手
* ``pixmaps`` —— 圆角封面与标题栏图标确实画出了指定尺寸

不触网、不写盘;离屏平台 + 进程内共享的 ``QApplication``(与 ``test_icons`` /
``test_ui_wiring`` 同一套约定:必须用 ``QApplication``,否则 widget 用例在全量跑时
会突然起不来)。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)
from PySide6.QtGui import QFontMetrics, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402

from bilibili_music.core.models import Page  # noqa: E402
from bilibili_music.ui.pixmaps import cover_pixmap, logo_pixmap  # noqa: E402
from bilibili_music.ui.widgets import (  # noqa: E402
    ElidedLabel,
    FramelessWindow,
    PageSelector,
    PlaceholderPage,
    PlayerBar,
    Sidebar,
    TitleBar,
    TrackList,
    TrackRow,
)
from bilibili_music.ui.widgets.page_selector import (  # noqa: E402
    EMPTY_LABEL,
    POPUP_BORDER,
    POPUP_MAX_HEIGHT,
    POPUP_MARGIN,
    POPUP_ROW_HEIGHT,
    POPUP_SPACING,
)
from bilibili_music.ui.widgets.sidebar import NAV_ITEMS  # noqa: E402
from bilibili_music.ui.widgets.track_list import (  # noqa: E402
    INDEX_COLUMN,
    RICH_COLUMN,
    _INDEX_PADDING,
    split_title_prefix,
)
from bilibili_music.ui.widgets.window_frame import RESIZE_GRIP, _ResizeGrip  # noqa: E402


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


# ====================================================================== 替身


class _FakeCovers:
    """封面加载器替身:只记录"请求了谁、清了几次"。

    真正的取图与贴图链路在 ``test_cover_loader`` 与 ``test_ui_wiring`` 里验;
    这里只关心列表控件有没有按约定调它。
    """

    def __init__(self) -> None:
        """建一个空的请求记录。"""
        self.requested: list[str] = []
        self.clear_count = 0

    def load(self, url: str) -> None:
        """记录一次封面请求。"""
        self.requested.append(url)

    def clear(self) -> None:
        """记录一次清空。"""
        self.clear_count += 1


def _rows() -> list[TrackRow]:
    """造三行样本数据(含一行没有封面的)。"""
    return [
        TrackRow(
            title="晴天【官方 MV】",
            prefix="周杰伦 - ",
            subtitle="480.0万 播放",
            cover_url="https://i0.hdslb.com/a.jpg",
            columns=("杰威尔音乐官方频道", "4:29", "?"),
        ),
        TrackRow(
            title="七里香",
            subtitle="320.0万 播放",
            cover_url="https://i0.hdslb.com/b.jpg",
            columns=("杰威尔音乐官方频道", "4:59", "?"),
        ),
        TrackRow(title="没有封面的歌", columns=("某UP", "3:01", "?")),
    ]


_COLUMNS = ("#", "视频信息", "UP主", "时长", "分P", "操作")
_ACTION_COLUMN = 5


# ====================================================================== 纯逻辑


class TestSplitTitlePrefix(unittest.TestCase):
    """标题前缀拆分。"""

    def test_splits_on_the_first_spaced_hyphen(self) -> None:
        """"歌手 - 歌名"要拆成弱化前缀与主标题。"""
        self.assertEqual(
            split_title_prefix("周杰伦 - 晴天【官方 MV】"),
            ("周杰伦 - ", "晴天【官方 MV】"),
        )

    def test_prefix_and_title_rebuild_the_original(self) -> None:
        """拆出来的两段拼回去必须与原文一字不差(界面显示的是这两段之和)。"""
        title = "周杰伦 - 晴天【官方 MV】"
        prefix, rest = split_title_prefix(title)
        self.assertEqual(prefix + rest, title)

    def test_title_without_a_spaced_hyphen_is_left_alone(self) -> None:
        """没有 ``" - "`` 的标题保持原样(单个连字符太常见,不能当分隔符)。"""
        for title in ("【4K】某首歌", "MV-01 试听", "A - ", " - B", ""):
            with self.subTest(title=title):
                self.assertEqual(split_title_prefix(title), ("", title))


class TestElidedLabel(unittest.TestCase):
    """按宽度省略的标签。"""

    def test_full_text_is_kept(self) -> None:
        """``text()`` 可能被省略,但 ``full_text()`` 必须始终是完整内容。

        要先 ``show()`` 再 ``resize()``:Qt 对**隐藏**控件不派发 resize 事件,而省略
        正是靠 ``resizeEvent`` 触发的(真实界面里控件总是可见的)。
        """
        label = ElidedLabel("一个很长很长的标题" * 5)
        label.show()
        self.addCleanup(label.close)
        label.resize(40, 20)
        self.assertEqual(label.full_text(), "一个很长很长的标题" * 5)
        self.assertLess(len(label.text()), len(label.full_text()))
        self.assertIn("…", label.text())

    def test_tooltip_carries_the_full_text(self) -> None:
        """省略之后要靠工具提示把完整标题读出来。"""
        label = ElidedLabel("完整标题")
        self.assertEqual(label.toolTip(), "完整标题")

    def test_short_text_is_not_touched(self) -> None:
        """放得下就不该加省略号。"""
        label = ElidedLabel("短")
        label.resize(200, 20)
        self.assertEqual(label.text(), "短")


# ====================================================================== 列表


class TestTrackList(unittest.TestCase):
    """曲目列表:富信息行、封面、操作列与高亮。"""

    def _list(self, **kwargs) -> TrackList:  # noqa: ANN003 - 透传给构造函数
        """造一个列表并填上样本数据。"""
        widget = TrackList(_COLUMNS, action_column=_ACTION_COLUMN, **kwargs)
        widget.set_tracks(_rows())
        return widget

    def test_requires_at_least_two_columns(self) -> None:
        """只有一列时富信息没地方放,必须在构造时就报错而不是画出个半成品。"""
        with self.assertRaises(ValueError):
            TrackList(("#",))

    def test_rows_expose_title_subtitle_and_index(self) -> None:
        """富信息列的内容要能被读出来(它是单元格控件,不是 QTableWidgetItem)。"""
        widget = self._list()
        self.assertEqual(widget.rowCount(), 3)
        self.assertEqual(widget.title_at(0), "晴天【官方 MV】")
        self.assertEqual(widget.subtitle_at(0), "480.0万 播放")
        self.assertEqual(widget.item(0, INDEX_COLUMN).text(), "1")
        self.assertEqual(widget.item(0, 2).text(), "杰威尔音乐官方频道")

    def test_cover_requests_follow_the_rows(self) -> None:
        """填数据时要把有封面的地址交给加载器,没有封面的行不请求。"""
        covers = _FakeCovers()
        widget = self._list(covers=covers)  # type: ignore[arg-type]
        self.assertEqual(
            covers.requested,
            ["https://i0.hdslb.com/a.jpg", "https://i0.hdslb.com/b.jpg"],
        )

    def test_set_tracks_clears_stale_cover_requests(self) -> None:
        """列表整体换内容时要清掉旧封面请求:否则旧图还占着限速窗口慢慢取。"""
        covers = _FakeCovers()
        widget = self._list(covers=covers)  # type: ignore[arg-type]
        widget.set_tracks([])
        self.assertGreaterEqual(covers.clear_count, 2)

    def test_append_tracks_keeps_existing_rows_and_covers(self) -> None:
        """翻页追加重建已有行是错的:行不能动、已贴的封面不能丢、封面队列不能被清。

        清掉封面队列意味着"把前 30 行的封面请求全部作废再重下一遍",既慢又白挨限速。
        """
        covers = _FakeCovers()
        widget = self._list(covers=covers)  # type: ignore[arg-type]
        widget.set_cover("https://i0.hdslb.com/a.jpg", QPixmap(8, 8))
        covers.requested.clear()
        clears_before = covers.clear_count

        widget.append_tracks(
            [TrackRow(title="新歌", cover_url="https://i0.hdslb.com/c.jpg")]
        )

        self.assertEqual(widget.rowCount(), 4)
        self.assertEqual(widget.item(3, INDEX_COLUMN).text(), "4")  # 序号接着往下排
        self.assertEqual(widget.title_at(0), "晴天【官方 MV】")  # 旧行原样保留
        self.assertTrue(widget.has_cover_at(0))  # 已经贴上的封面没被丢掉
        self.assertEqual(covers.clear_count, clears_before)  # 没有清封面队列
        self.assertEqual(covers.requested, ["https://i0.hdslb.com/c.jpg"])

    def test_append_tracks_with_nothing_changes_nothing(self) -> None:
        """空批次(接口回了 0 条)不该动列表。"""
        widget = self._list()
        widget.append_tracks([])
        self.assertEqual(widget.rowCount(), 3)

    def test_index_column_fits_three_digits(self) -> None:
        """翻页后行号会过百,序号列必须放得下三位数。

        列宽是**按字体算**的(不写死像素):同一份代码在真实字体与离屏环境的 fallback
        字体下宽度并不相同,所以断言也必须跟着字体走 —— 列宽里有一段被 QSS 的
        ``padding: 0 8px`` 吃掉,那部分放不下字。
        """
        widget = self._list()
        available = widget.columnWidth(INDEX_COLUMN) - _INDEX_PADDING
        bold = widget.font()
        bold.setBold(True)  # 当前播放行的序号是加粗的
        for text in ("100", "150"):
            with self.subTest(text=text):
                self.assertGreaterEqual(
                    available, QFontMetrics(widget.font()).horizontalAdvance(text)
                )
                self.assertGreaterEqual(
                    available, QFontMetrics(bold).horizontalAdvance(text)
                )

    def test_scrolling_near_the_bottom_asks_for_more(self) -> None:
        """滚到快到底要报"再要一批";在顶部时不该报。"""
        host = QWidget()
        self.addCleanup(host.close)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        widget = TrackList(_COLUMNS, action_column=_ACTION_COLUMN)
        widget.set_tracks([TrackRow(title=f"第{i}首") for i in range(30)])
        layout.addWidget(widget)
        host.resize(400, 200)
        host.show()
        QApplication.processEvents()

        bar = widget.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0, "前提:内容确实比视口高")

        got: list[bool] = []
        widget.load_more_requested.connect(lambda: got.append(True))
        bar.setValue(bar.maximum())  # 滚到底
        self.assertEqual(len(got), 1)
        bar.setValue(0)  # 又滚回顶部
        self.assertEqual(len(got), 1, "回到顶部不该再要下一批")

    def test_set_cover_marks_only_the_matching_rows(self) -> None:
        """封面按 URL 找到行:贴错行比不贴更糟。"""
        widget = self._list()
        self.assertFalse(widget.has_cover_at(0))
        widget.set_cover("https://i0.hdslb.com/b.jpg", QPixmap(8, 8))
        self.assertFalse(widget.has_cover_at(0))
        self.assertTrue(widget.has_cover_at(1))

    def test_unknown_cover_url_changes_nothing(self) -> None:
        """没人要的封面不该让列表出错(加载器可能回来得比换列表晚)。"""
        widget = self._list()
        widget.set_cover("https://i0.hdslb.com/zzz.jpg", QPixmap(8, 8))
        self.assertFalse(widget.has_cover_at(0))

    def test_set_cell_updates_plain_columns(self) -> None:
        """详情补全后要能把"分P"列改成真实数量。"""
        widget = self._list()
        widget.set_cell(0, 4, "12")
        self.assertEqual(widget.item(0, 4).text(), "12")

    def test_set_cell_refuses_special_columns(self) -> None:
        """序号列、富信息列与操作列由控件自己维护,外部改不了。"""
        widget = self._list()
        before = widget.item(0, INDEX_COLUMN).text()
        widget.set_cell(0, INDEX_COLUMN, "99")
        widget.set_cell(0, RICH_COLUMN, "覆盖标题")
        widget.set_cell(0, _ACTION_COLUMN, "覆盖按钮")
        self.assertEqual(widget.item(0, INDEX_COLUMN).text(), before)
        self.assertEqual(widget.title_at(0), "晴天【官方 MV】")

    def test_set_cell_ignores_out_of_range_rows(self) -> None:
        """越界行号什么都不做,不能抛异常打断调用方。"""
        widget = self._list()
        widget.set_cell(99, 4, "x")
        self.assertEqual(widget.rowCount(), 3)

    def test_highlight_marks_and_clears(self) -> None:
        """高亮要能打上也能撤掉,并且能从外面读出来。"""
        widget = self._list()
        widget.set_highlight(1)
        self.assertEqual(widget.highlighted_row(), 1)
        widget.set_highlight(-1)
        self.assertEqual(widget.highlighted_row(), -1)

    def test_highlight_selects_the_row(self) -> None:
        """当前行同时是选中行:底色的表达交给 QSS 的 ``::item:selected``。"""
        widget = self._list()
        widget.set_highlight(2)
        self.assertEqual(widget.selectionModel().selectedRows()[0].row(), 2)

    def test_add_button_emits_the_row(self) -> None:
        """操作列的"+"要带着行号发信号,否则会加错歌。"""
        widget = self._list()
        got: list[int] = []
        widget.add_requested.connect(got.append)
        buttons = widget.cellWidget(1, _ACTION_COLUMN).findChildren(QPushButton)
        buttons[0].click()  # 第一个是"+"
        self.assertEqual(got, [1])

    def test_action_buttons_have_tooltips(self) -> None:
        """操作列只有图标,没有提示就没人知道 "+" 是干什么的。"""
        widget = self._list()
        buttons = widget.cellWidget(0, _ACTION_COLUMN).findChildren(QPushButton)
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(button.toolTip() for button in buttons))

    def test_rich_cell_is_transparent_for_mouse(self) -> None:
        """富信息行必须让鼠标事件穿透,否则双击标题区域不会触发播放。"""
        widget = self._list()
        cell = widget.cellWidget(0, RICH_COLUMN)
        self.assertTrue(cell.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_centered_columns_are_honoured(self) -> None:
        """时长/分P 之类的短列要居中(设计稿里它们不在左边线上)。"""
        widget = TrackList(_COLUMNS, action_column=_ACTION_COLUMN, centered=(3, 4))
        widget.set_tracks(_rows())
        centered = widget.item(0, 3).textAlignment()
        self.assertTrue(centered & Qt.AlignmentFlag.AlignHCenter)


# ====================================================================== 分P选择器


def _pages(count: int) -> list[Page]:
    """造 ``count`` 个分P样本;标题与时长逐P不同,便于断言取到的是哪一P。"""
    return [
        Page(index=i + 1, cid=1000 + i, title=f"第{i + 1}首", duration=180 + i)
        for i in range(count)
    ]


class TestPageSelector(unittest.TestCase):
    """播放条上的分P选择器:显示规则、禁用规则与弹出菜单。"""

    def _selector(self, *, count: int = 3, current: int = 2) -> PageSelector:
        """造一个填好分P数据的选择器,并在用例结束时关掉菜单。"""
        selector = PageSelector()
        selector.set_pages(_pages(count), current)
        self.addCleanup(selector.close_popup)
        return selector

    def test_shows_the_current_page(self) -> None:
        """标签要写成"分P:P2 第2首"这种形式 —— 光有编号用户看不出是哪首歌。"""
        selector = self._selector(count=3, current=2)
        self.assertEqual(selector.full_label_text(), "分P:P2 第2首")
        self.assertTrue(selector.text().endswith("▾"))  # 下拉箭头是"能点"的唯一提示

    def test_wider_label_than_the_widget_is_elided_but_kept_in_the_tooltip(self) -> None:
        """标题过长只省略**显示**,完整文本必须在工具提示里读得到。"""
        long_title = "一个非常非常长的分P标题" * 4
        selector = self._selector(count=1, current=1)
        selector.set_pages([Page(index=1, cid=1, title=long_title, duration=180)], 1)
        self.assertIn("…", selector.text())
        self.assertTrue(selector.text().endswith("▾"))  # 箭头不许跟着一起被省略
        self.assertEqual(selector.full_label_text(), f"分P:P1 {long_title}")
        self.assertEqual(selector.toolTip(), f"分P:P1 {long_title}")

    def test_multipart_is_clickable(self) -> None:
        """多P合集要能点开(禁用状态在 Qt 里点不出菜单,这条同时验证了两件事)。"""
        selector = self._selector(count=3, current=1)
        self.assertTrue(selector.isEnabled())
        selector.click()
        self.assertTrue(selector.is_popup_open)

    def test_single_page_keeps_the_position_but_is_disabled(self) -> None:
        """单P视频也照样显示当前分P,只是点不开 —— 清空文本会让控件宽度/位置跳动。"""
        selector = self._selector(count=1, current=1)
        self.assertFalse(selector.isEnabled())
        self.assertEqual(selector.full_label_text(), "分P:P1 第1首")

    def test_without_a_video_it_is_disabled_with_a_placeholder(self) -> None:
        """没有当前视频:显示占位文本并禁用。"""
        selector = PageSelector()
        self.assertFalse(selector.isEnabled())
        self.assertEqual(selector.full_label_text(), EMPTY_LABEL)
        selector.click()
        self.assertFalse(selector.is_popup_open)  # 禁用时点不出菜单

    def test_unknown_detail_shows_only_the_page_number(self) -> None:
        """详情还没补全时只知道序号:先只报序号,别显示上一首的分P标题。"""
        selector = PageSelector()
        selector.set_pages((), 3)
        self.assertFalse(selector.isEnabled())
        self.assertEqual(selector.full_label_text(), "分P:P3")

    def test_popup_lists_every_page_with_its_own_title_and_duration(self) -> None:
        """菜单每一行要有编号、该分P自己的标题与自己的时长(领域铁律)。"""
        selector = self._selector(count=3, current=2)
        selector.click()
        rows = selector.popup_rows()
        self.assertEqual([row.page_index for row in rows], [1, 2, 3])
        self.assertEqual(rows[1].index_label.text(), "P2")
        self.assertEqual(rows[1].title_label.full_text(), "第2首")
        self.assertEqual(rows[1].duration_label.text(), "3:01")  # 第2P自己的时长

    def test_long_titles_in_the_menu_are_elided_and_kept_in_the_tooltip(self) -> None:
        """菜单行同样放不下长标题:省略显示,完整标题留在工具提示里。"""
        long_title = "一个非常非常长的分P标题" * 4
        selector = self._selector(count=2, current=1)
        selector.set_pages(
            [
                Page(index=1, cid=1, title=long_title, duration=180),
                Page(index=2, cid=2, title="短标题", duration=180),
            ],
            1,
        )
        selector.click()
        title_label = selector.popup_rows()[0].title_label
        self.assertIn("…", title_label.text())
        self.assertEqual(title_label.full_text(), long_title)
        self.assertEqual(title_label.toolTip(), long_title)

    def test_row_title_keeps_its_own_object_name(self) -> None:
        """菜单行标题要用独立的 objectName,不许和内容区大标题 ``#PageTitle`` 撞名。

        撞名的后果不是"名字不好看":``theme.py`` 里 ``QLabel#PageTitle`` 是 20px 加粗的
        内容区大标题,菜单一行只有 34px 高,套上它字会撑满整行(用户反馈的"字太大")。
        """
        selector = self._selector(count=2, current=1)
        selector.click()
        title_label = selector.popup_rows()[0].title_label
        self.assertEqual(title_label.objectName(), "PageRowTitle")
        self.assertNotEqual(title_label.objectName(), "PageTitle")

    def test_only_the_current_page_is_marked(self) -> None:
        """菜单里只有一行是选中态,而且圆点是实心的。"""
        selector = self._selector(count=3, current=2)
        selector.click()
        rows = selector.popup_rows()
        self.assertEqual([row.is_current for row in rows], [False, True, False])
        self.assertEqual([row.dot_label.text() for row in rows], ["○", "●", "○"])

    def test_choosing_a_row_reports_it_and_closes_the_menu(self) -> None:
        """点某一行:把分P序号发上去并关掉菜单(单选,选完即收起)。"""
        selector = self._selector(count=3, current=1)
        got: list[int] = []
        selector.page_selected.connect(got.append)
        selector.click()
        selector.popup_rows()[2].click()
        self.assertEqual(got, [3])
        self.assertFalse(selector.is_popup_open)

    def test_second_click_closes_without_changing_the_choice(self) -> None:
        """再点一次选择器是收起菜单,不该顺手换一P。"""
        selector = self._selector(count=3, current=2)
        got: list[int] = []
        selector.page_selected.connect(got.append)
        selector.click()
        selector.click()
        self.assertFalse(selector.is_popup_open)
        self.assertEqual(got, [])

    def test_new_pages_close_a_stale_menu(self) -> None:
        """切歌/详情补全后再刷新时,菜单里那些行已经属于上一首了,必须收起来。"""
        selector = self._selector(count=3, current=1)
        selector.click()
        self.assertTrue(selector.is_popup_open)
        selector.set_pages(_pages(2), 1)
        self.assertFalse(selector.is_popup_open)

    def test_popup_grows_with_the_page_count_but_stops_at_the_cap(self) -> None:
        """高度随分P数量增长,超过上限后在菜单内部滚动。"""
        selector = self._selector(count=3, current=1)
        selector.click()
        assert selector._popup is not None  # noqa: SLF001 - 就是要验菜单尺寸
        self.assertEqual(
            selector._popup.height(),  # noqa: SLF001
            2 * (POPUP_MARGIN + POPUP_BORDER)
            + 3 * POPUP_ROW_HEIGHT
            + 2 * POPUP_SPACING,
        )

        selector.set_pages(_pages(40), 1)
        selector.click()
        self.assertEqual(len(selector.popup_rows()), 40)
        self.assertEqual(selector._popup.height(), POPUP_MAX_HEIGHT)  # noqa: SLF001

    def test_popup_shows_no_scrollbar_while_every_row_fits(self) -> None:
        """行数没超过上限时不该出现滚动条:高度漏算描边就会多出一条无意义的滚动条。"""
        selector = self._selector(count=6, current=1)
        selector.click()
        assert selector._popup is not None  # noqa: SLF001
        scroll_bar = selector._popup.scroll.verticalScrollBar()  # noqa: SLF001
        self.assertFalse(scroll_bar.isVisible())
        self.assertEqual(scroll_bar.maximum(), 0)

    def test_popup_is_anchored_above_and_centered_on_the_selector(self) -> None:
        """菜单底边紧贴选择器上边缘、水平居中对齐 —— 否则会被窗口底部裁掉。"""
        selector, host = self._hosted_selector()
        selector.click()
        assert selector._popup is not None  # noqa: SLF001 - 几何就是要验的东西
        popup_rect = selector._popup.geometry()  # noqa: SLF001
        selector_rect = QRect(selector.mapToGlobal(QPoint(0, 0)), selector.size())
        self.assertEqual(popup_rect.bottom() + 1, selector_rect.top())
        # 居中允许 1px 的取整误差(宽度差是奇数时除法会向下取整)
        self.assertLessEqual(
            abs(popup_rect.center().x() - selector_rect.center().x()), 1
        )

    def test_popup_stays_inside_the_window(self) -> None:
        """选择器贴着窗口右边时,菜单要向左收,不能被窗口裁掉一半。"""
        selector, host = self._hosted_selector(right_aligned=True)
        selector.click()
        assert selector._popup is not None  # noqa: SLF001
        popup_rect = selector._popup.geometry()  # noqa: SLF001
        host_right = host.mapToGlobal(QPoint(host.width(), 0)).x()
        self.assertLessEqual(popup_rect.right(), host_right)

    def test_popup_avoids_the_widget_it_is_told_to_avoid(self) -> None:
        """菜单不许压住被指定的控件(实际用法是右侧播放队列面板)。"""
        selector, host = self._hosted_selector()
        drawer = QWidget(host)
        drawer.setGeometry(host.width() - 300, 0, 300, host.height())
        drawer.show()
        selector.set_avoid_widget(drawer)
        selector.click()
        assert selector._popup is not None  # noqa: SLF001
        popup_rect = selector._popup.geometry()  # noqa: SLF001
        drawer_left = drawer.mapToGlobal(QPoint(0, 0)).x()
        self.assertLessEqual(popup_rect.right(), drawer_left)

    def _hosted_selector(self, *, right_aligned: bool = False) -> tuple[PageSelector, QWidget]:
        """把选择器放进一个显示出来的窗口里(几何必须相对真窗口才有意义)。

        Args:
            right_aligned: 是否把选择器贴到窗口右边(验"贴边时向内收"那条)。

        Returns:
            ``(选择器, 承载它的窗口)``。
        """
        host = QWidget()
        self.addCleanup(host.close)
        host.resize(1000, 600)
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        selector = PageSelector()
        layout.addWidget(selector)
        if not right_aligned:
            # 留出右边距,让"居中"这一条不被窗口边界挤掉(那是另一条用例)
            layout.addSpacing(400)
        host.show()
        QApplication.processEvents()
        selector.set_pages(_pages(3), 1)
        self.addCleanup(selector.close_popup)
        return selector, host


class TestPlayerBarLayout(unittest.TestCase):
    """播放条的版式:分P选择器要落在"播放控制按钮之后、音质下拉之前"。"""

    def _bar(self) -> tuple[PlayerBar, QWidget]:
        """造一个显示出来的播放条(版式只有被布局过之后才有意义)。"""
        host = QWidget()
        self.addCleanup(host.close)
        bar = PlayerBar()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(bar)
        host.resize(1200, 120)
        host.show()
        bar.set_pages(_pages(3), 2)
        QApplication.processEvents()
        return bar, host

    def test_selector_sits_between_the_controls_and_the_quality_combo(self) -> None:
        """左起:播放控制按钮 → 分P选择器 → 音质下拉(设计稿的顺序)。"""
        bar, host = self._bar()
        x_of = lambda widget: widget.mapTo(host, QPoint(0, 0)).x()  # noqa: E731 - 断言辅助
        self.assertLess(x_of(bar.queue_button), x_of(bar.page_selector))
        self.assertLess(x_of(bar.page_selector), x_of(bar.quality_combo))

    def test_selector_is_vertically_centered_with_the_quality_combo(self) -> None:
        """两个控件高度不同(36 / 30),垂直中心要对齐。"""
        bar, _ = self._bar()
        self.assertLessEqual(
            abs(bar.page_selector.geometry().center().y()
                - bar.quality_combo.geometry().center().y()), 1
        )

    def test_selector_keeps_its_size_when_nothing_is_playing(self) -> None:
        """清空播放后控件尺寸不变:否则整行布局会跟着抖一下。"""
        bar, _ = self._bar()
        before = bar.page_selector.size()
        bar.set_pages((), 0)
        self.assertEqual(bar.page_selector.size(), before)

    def test_bar_refuses_to_open_the_menu_without_pages(self) -> None:
        """没有当前视频时点选择器不该弹菜单(禁用按钮本身也点不动)。"""
        bar, _ = self._bar()
        bar.set_pages((), 0)
        bar.page_selector.click()
        self.assertFalse(bar.page_selector.is_popup_open)


# ====================================================================== 侧栏


class TestSidebar(unittest.TestCase):
    """左侧导航。"""

    def _sidebar(self) -> Sidebar:
        """造一个侧栏(不需要父对象)。"""
        return Sidebar()

    def test_nav_entries_raise_their_key(self) -> None:
        """每个入口都要把 key 发出来,主窗口靠它决定切哪一页。"""
        sidebar = self._sidebar()
        got: list[str] = []
        sidebar.nav_selected.connect(got.append)
        sidebar.nav_buttons["discover"].click()
        sidebar.nav_buttons["cache"].click()
        self.assertEqual(got, ["discover", "cache"])

    def test_queue_entry_is_gone(self) -> None:
        """"播放队列"的开关只在播放条上,侧栏不该再留一个入口。

        侧栏入口一律是"切一页"的语义,队列面板是常驻开关 —— 混在一起会让"点侧栏
        到底是换页还是收起面板"说不清。
        """
        sidebar = self._sidebar()
        self.assertNotIn("queue", sidebar.nav_buttons)
        self.assertEqual(
            [item.key for item in NAV_ITEMS], ["discover", "results", "cache"]
        )

    def test_playlist_click_clears_page_selection(self) -> None:
        """选歌单要清掉页面入口的选中态,侧栏不能同时亮两个。"""
        sidebar = self._sidebar()
        sidebar.nav_buttons["results"].click()
        sidebar.playlist_buttons["周杰伦"].click()
        self.assertFalse(sidebar.nav_buttons["results"].isChecked())
        self.assertTrue(sidebar.playlist_buttons["周杰伦"].isChecked())

    def test_page_click_clears_playlist_selection(self) -> None:
        """反过来也一样:切回页面时歌单要熄灭。"""
        sidebar = self._sidebar()
        sidebar.playlist_buttons["周杰伦"].click()
        sidebar.nav_buttons["results"].click()
        self.assertFalse(sidebar.playlist_buttons["周杰伦"].isChecked())

    def test_playlist_selection_raises_the_name(self) -> None:
        """歌单名要原样发出去(占位页的标题就是它)。"""
        sidebar = self._sidebar()
        got: list[str] = []
        sidebar.playlist_selected.connect(got.append)
        sidebar.playlist_buttons["华语经典"].click()
        self.assertEqual(got, ["华语经典"])

    def test_create_button_raises_its_signal(self) -> None:
        """"+"按钮要有信号,不能是哑的。"""
        sidebar = self._sidebar()
        got: list[bool] = []
        sidebar.create_playlist_requested.connect(lambda: got.append(True))
        sidebar.create_button.click()
        self.assertEqual(got, [True])

    def test_set_active_page_does_not_emit(self) -> None:
        """上层同步选中态是"告诉界面",不是"用户点了",不该再触发一次导航。"""
        sidebar = self._sidebar()
        got: list[str] = []
        sidebar.nav_selected.connect(got.append)
        sidebar.set_active_page("results")
        self.assertTrue(sidebar.nav_buttons["results"].isChecked())
        self.assertEqual(got, [])


# ====================================================================== 标题栏


class TestTitleBar(unittest.TestCase):
    """自绘标题栏。"""

    def test_search_signals(self) -> None:
        """按钮与回车都要发同一个信号(两个入口,一条路径)。"""
        bar = TitleBar()
        got: list[bool] = []
        bar.search_requested.connect(lambda: got.append(True))
        bar.search_button.click()
        bar.search_input.returnPressed.emit()
        self.assertEqual(got, [True, True])

    def test_window_button_signals(self) -> None:
        """三个窗口按钮各发各的信号。"""
        bar = TitleBar()
        hits: list[str] = []
        bar.minimize_requested.connect(lambda: hits.append("min"))
        bar.maximize_requested.connect(lambda: hits.append("max"))
        bar.close_requested.connect(lambda: hits.append("close"))
        bar.minimize_button.click()
        bar.maximize_button.click()
        bar.close_button.click()
        self.assertEqual(hits, ["min", "max", "close"])

    def test_close_button_carries_the_closing_property(self) -> None:
        """关闭键要有 ``closing`` 属性:样式表靠它让悬停变红。"""
        bar = TitleBar()
        self.assertTrue(bar.close_button.property("closing"))

    def test_set_maximized_swaps_icon_and_tooltip(self) -> None:
        """最大化之后按钮要变成"还原",否则用户不知道还能点。"""
        bar = TitleBar()
        bar.set_maximized(True)
        self.assertEqual(bar.maximize_button.toolTip(), "向下还原")
        bar.set_maximized(False)
        self.assertEqual(bar.maximize_button.toolTip(), "最大化")

    def test_drag_does_not_raise_without_a_window(self) -> None:
        """离屏/未显示时拿不到窗口句柄,拖动请求要安静地失败而不是崩。"""
        bar = TitleBar()
        self.assertFalse(bar._start_system_move())  # noqa: SLF001 - 就是要验这个边界


# ====================================================================== 占位页


class TestPlaceholderPage(unittest.TestCase):
    """占位页。"""

    def test_set_content_replaces_title_and_hint(self) -> None:
        """同一个实例要能反复当不同页面的占位(整个应用只留一个)。"""
        page = PlaceholderPage()
        page.set_content("发现", "排行榜还没实现")
        self.assertEqual(page.title_label.full_text(), "发现")
        self.assertEqual(page.hint_label.full_text(), "排行榜还没实现")
        page.set_content("本地缓存", "")
        self.assertEqual(page.title_label.full_text(), "本地缓存")
        self.assertTrue(page.hint_label.isHidden())

    def test_hint_comes_back_when_given_again(self) -> None:
        """说明文字清掉之后还能再显示回来(隐藏状态不能被粘住)。"""
        page = PlaceholderPage()
        page.set_content("A", "")
        page.set_content("A", "有说明了")
        self.assertFalse(page.hint_label.isHidden())


# ====================================================================== 无边框窗口


class TestFramelessWindow(unittest.TestCase):
    """无边框窗口与缩放把手。"""

    def test_window_is_frameless(self) -> None:
        """去掉系统边框是自绘标题栏的前提,否则会出现两条标题栏。"""
        window = FramelessWindow()
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)

    def test_has_eight_resize_grips(self) -> None:
        """四条边 + 四个角,一个都不能少 —— 少了哪边就拖不动哪边。"""
        window = FramelessWindow()
        grips = window.findChildren(_ResizeGrip)
        self.assertEqual(len(grips), 8)

    def test_grips_sit_on_the_edges(self) -> None:
        """把手要贴着窗口边缘放,否则鼠标永远碰不到它们。

        与 ``ElidedLabel`` 那条同一个原因:隐藏的窗口收不到 resize 事件,所以要先
        ``show()`` 再改尺寸(真实运行时窗口总是可见的)。
        """
        window = FramelessWindow()
        self.addCleanup(window.close)
        window.show()
        window.resize(400, 300)
        edges = sorted(
            (grip.geometry().x(), grip.geometry().y())
            for grip in window.findChildren(_ResizeGrip)
        )
        self.assertIn((0, 0), edges)  # 左上角
        self.assertIn((400 - RESIZE_GRIP, 300 - RESIZE_GRIP), edges)  # 右下角


# ====================================================================== 位图


class TestPixmaps(unittest.TestCase):
    """圆角封面与标题栏图标。"""

    def test_placeholder_cover_has_the_requested_size(self) -> None:
        """没有封面时也要给出指定尺寸的占位图,布局才不会因为缺图而跳。"""
        pixmap = cover_pixmap(None, 40)
        self.assertEqual((pixmap.width(), pixmap.height()), (40, 40))
        self.assertFalse(pixmap.isNull())

    def test_cover_is_cropped_to_a_square(self) -> None:
        """宽高比不对的封面要被裁成正方形,而不是被拉扁。"""
        source = QPixmap(120, 40)
        source.fill(Qt.GlobalColor.red)
        pixmap = cover_pixmap(source, 40)
        self.assertEqual((pixmap.width(), pixmap.height()), (40, 40))

    def test_empty_source_falls_back_to_placeholder(self) -> None:
        """空位图按"没有封面"处理(CDN 偶尔会返回 0×0 的图)。"""
        pixmap = cover_pixmap(QPixmap(), 40)
        self.assertEqual((pixmap.width(), pixmap.height()), (40, 40))

    def test_logo_is_square_and_not_empty(self) -> None:
        """标题栏图标必须是正方形且真的画了东西(全透明等于没图标)。"""
        pixmap = logo_pixmap(28)
        self.assertEqual((pixmap.width(), pixmap.height()), (28, 28))
        image = pixmap.toImage()
        self.assertTrue(
            any(
                image.pixelColor(x, y).alpha() > 0
                for x in range(28)
                for y in range(28)
            )
        )


if __name__ == "__main__":
    unittest.main()
