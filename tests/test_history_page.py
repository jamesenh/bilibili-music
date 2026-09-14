"""最近播放页的单元测试:控件行为 + 主窗口接线。

分两半:

* :class:`TestHistoryPageWidget` 只测控件本身(填数据、过滤、空态、高亮、信号转发),
  用离屏平台构造,不碰主窗口;
* :class:`TestHistoryPageWiring` 把**真的** ``PlayHistory``(落在沙箱库里)接进主窗口,
  验证"点侧栏能看到记录、双击能播、删历史不会顺手把缓存删掉、播放时真的写了一条"。

第二条里的解析器是**真的** :class:`~bilibili_music.audio.resolver.AudioResolver`,
而客户端替身被设计成"一被调用就让用例失败"。于是"已缓存的歌从历史页点播零网络请求"
是被证明的,而不是靠断言请求次数为 0 推断的。

不触网、不出声;模态弹窗(``warning`` / ``question``)全部换成记录器,否则离屏测试会挂住。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置,否则无显示环境下起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402

from bilibili_music.api.bilibili import SearchResult  # noqa: E402
from bilibili_music.audio.playback import PlaybackController  # noqa: E402
from bilibili_music.audio.resolver import AudioResolver, ResolvedAudio  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.config import AppConfig, ConfigStore  # noqa: E402
from bilibili_music.core.cover_cache import CoverCache  # noqa: E402
from bilibili_music.core.history import HistoryEntry, PlayHistory, history_entry_for  # noqa: E402
from bilibili_music.core.library_db import LibraryDb  # noqa: E402
from bilibili_music.core.models import AudioTrack, Page, Video  # noqa: E402
from bilibili_music.core.session import SessionStore  # noqa: E402
from bilibili_music.ui.main_window import MainWindow  # noqa: E402
from bilibili_music.ui.widgets import HistoryPage  # noqa: E402

#: 落盘类用例的临时目录根(与 test_cache_page 同一套做法:不用 tempfile)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``(进程里只允许有一个)。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


# ====================================================================== 替身


class _FakeCovers:
    """封面加载器替身:只记录请求了谁(取图链路另有测试)。"""

    def __init__(self) -> None:
        """建一个空的请求记录。"""
        self.requested: list[str] = []

    def load(self, url: str) -> None:
        """记录一次封面请求。"""
        self.requested.append(url)

    def clear(self) -> None:
        """列表整体换内容时会清掉排队中的请求(本替身只提供接口)。"""


class _NoRequestClient:
    """接口客户端替身:**任何**请求都让用例失败(证明离线点播真的没发请求)。"""

    def fetch_video(self, bvid: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """已缓存的歌不该补详情。"""
        raise AssertionError("离线点播不该请求详情接口")

    def fetch_audio_tracks(  # noqa: ANN001 - 替身签名
        self, bvid: str, cid: int, *, on_success, on_error
    ) -> None:
        """已缓存的歌不该请求 playurl。"""
        raise AssertionError("离线点播不该请求 playurl 接口")

    def fetch_cover(self, url: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """封面请求记下来即可(每一行都会取封面)。"""
        return None

    def search_video(  # noqa: ANN001 - 替身签名
        self, keyword: str, *, page: int = 1, on_success, on_error
    ) -> None:
        """搜索不是这一页的事,直接回调空结果。"""
        on_success(SearchResult())

    def close(self) -> None:
        """关闭是空操作。"""


class _FakePlayer(QObject):
    """播放器替身:提供编排层与界面用到的那四个信号。"""

    state_changed = Signal(bool)
    position_changed = Signal(int, int)
    track_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self) -> None:
        """建一个"什么都没加载"的播放器替身。"""
        super().__init__()
        self.loaded: list[Path] = []
        self._is_playing = False

    @property
    def is_playing(self) -> bool:
        """是否处于播放状态。"""
        return self._is_playing

    @property
    def position_ms(self) -> int:
        """当前播放位置(本文件不关心,给个 0)。"""
        return 0

    def load(self, path: Path, *, autoplay: bool = True) -> None:
        """记录一次加载。"""
        self.loaded.append(Path(path))
        self._is_playing = autoplay

    def play(self) -> None:
        """记录一次继续播放。"""
        self._is_playing = True

    def pause(self) -> None:
        """记录一次暂停。"""
        self._is_playing = False

    def toggle(self) -> None:
        """切换播放状态。"""
        self._is_playing = not self._is_playing

    def stop(self) -> None:
        """停止播放。"""
        self._is_playing = False

    def seek(self, position_ms: int) -> None:
        """跳转(本文件不关心)。"""

    def set_volume(self, volume: float) -> None:
        """设置音量(本文件不关心)。"""


# ====================================================================== 控件用例


def _entry(
    *,
    bvid: str = "BV1",
    cid: int = 100,
    title: str = "晴天",
    author: str = "周杰伦",
    page_index: int = 1,
    multipart: bool = False,
    duration: int = 269,
    played_at: float = 0.0,
) -> HistoryEntry:
    """造一条播放记录(展示相关字段都可以调)。"""
    return HistoryEntry(
        bvid=bvid,
        cid=cid,
        page_index=page_index,
        title=title,
        author=author,
        page_title=title,
        multipart=multipart,
        duration=duration,
        cover_url="https://i0.hdslb.com/a.jpg",
        played_at=played_at or time.time(),
    )


class TestHistoryPageWidget(unittest.TestCase):
    """最近播放页控件本身:填数据、过滤、空态、高亮与信号转发。"""

    def _page(self, entries: list[HistoryEntry] | None = None) -> HistoryPage:
        """造一个填好数据的页面。"""
        page = HistoryPage()
        page.set_entries(entries if entries is not None else [_entry()])
        return page

    # ---------------------------------------------------------- 空态与统计

    def test_empty_state_when_nothing_was_played(self) -> None:
        """一条记录都没有时给空态页,而不是一张空表格。"""
        page = self._page([])
        self.assertIs(page.stack.currentWidget(), page.empty_page)
        self.assertEqual(page.empty_page.title_label.full_text(), "还没有播放记录")
        self.assertEqual(page.list.rowCount(), 0)
        self.assertFalse(page.clear_button.isEnabled())
        self.assertEqual(page.count_label.text(), "")

    def test_summary_shows_the_count(self) -> None:
        """有记录时显示条数并让"清空历史"可用。"""
        page = self._page([_entry(), _entry(bvid="BV2", cid=200)])
        self.assertIs(page.stack.currentWidget(), page.list)
        self.assertEqual(page.count_label.text(), "2 首")
        self.assertTrue(page.clear_button.isEnabled())

    # ---------------------------------------------------------- 行内容

    def test_rows_show_duration_and_relative_time(self) -> None:
        """每行要有时长与"多久以前" —— 最近播放页的信息就靠这两列。"""
        page = self._page([_entry(duration=269)])
        self.assertEqual(page.list.rowCount(), 1)
        self.assertEqual(page.list.title_at(0), "晴天")
        self.assertEqual(page.list.item(0, 2).text(), "4:29")
        self.assertEqual(page.list.item(0, 3).text(), "刚刚")

    def test_multipart_row_marks_the_page_number(self) -> None:
        """多P合集的记录要标出是第几P(同一视频里每P都是一首独立的歌)。"""
        page = self._page([_entry(page_index=3, multipart=True)])
        self.assertEqual(page.list.subtitle_at(0), "周杰伦 · P3")

    def test_single_page_row_has_no_page_number(self) -> None:
        """单P视频不显示 ``P1``(与缓存页、播放层同一口径)。"""
        page = self._page([_entry(multipart=False)])
        self.assertEqual(page.list.subtitle_at(0), "周杰伦")

    def test_rows_request_their_cover(self) -> None:
        """有封面的行要交给加载器取图(本页用自己的加载器,不与缓存页抢)。"""
        covers = _FakeCovers()
        page = HistoryPage(covers)  # type: ignore[arg-type]
        page.set_entries([_entry()])
        self.assertEqual(covers.requested, ["https://i0.hdslb.com/a.jpg"])

    # ---------------------------------------------------------- 过滤

    def test_filter_matches_title_and_author_case_insensitively(self) -> None:
        """过滤按曲名与UP主匹配,且不区分大小写。"""
        page = self._page(
            [
                _entry(bvid="BV1", title="晴天"),
                _entry(bvid="BV2", cid=2, title="七里香"),
                _entry(bvid="BV3", cid=3, title="Other", author="Jay"),
            ]
        )
        page.filter_input.setText("七里")
        self.assertEqual([e.bvid for e in page.visible_entries()], ["BV2"])
        page.filter_input.setText("jay")
        self.assertEqual([e.bvid for e in page.visible_entries()], ["BV3"])
        self.assertEqual(page.list.rowCount(), 1)

    def test_filtered_view_reports_its_own_row_numbers(self) -> None:
        """过滤之后行号以可见列表为准 —— 信号里传的行号必须与 ``entry_at`` 同一套。"""
        page = self._page([_entry(bvid="BV1"), _entry(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        self.assertEqual(page.entry_at(0).bvid, "BV2")  # type: ignore[union-attr]
        self.assertIsNone(page.entry_at(1))

    def test_summary_counts_within_the_filter(self) -> None:
        """过滤中要显示"可见 / 总数",否则用户不知道是不是记录丢了。"""
        page = self._page([_entry(bvid="BV1"), _entry(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        self.assertEqual(page.count_label.text(), "1 / 2 首")

    def test_no_match_shows_a_filter_specific_empty_state(self) -> None:
        """过滤到一条不剩时,文案要指向"过滤条件"而不是"还没有播放记录"。"""
        page = self._page([_entry()])
        page.filter_input.setText("不存在的东西")
        self.assertIs(page.stack.currentWidget(), page.empty_page)
        self.assertEqual(page.empty_page.title_label.full_text(), "没有匹配的播放记录")

    def test_clearing_the_filter_restores_every_row(self) -> None:
        """清空过滤框要恢复全部行(过滤不该动原始数据)。"""
        page = self._page([_entry(bvid="BV1"), _entry(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        page.filter_input.setText("")
        self.assertEqual(len(page.visible_entries()), 2)

    def test_new_data_keeps_the_filter(self) -> None:
        """上层刷新数据(删掉一条、刚播完一首)时不该把用户打的关键字清掉。"""
        page = self._page([_entry(bvid="BV1"), _entry(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        page.set_entries([_entry(bvid="BV2", cid=2, title="七里香")])
        self.assertEqual(page.filter_input.text(), "七里")
        self.assertEqual(len(page.visible_entries()), 1)

    # ---------------------------------------------------------- 高亮与信号

    def test_set_playing_marks_the_matching_row(self) -> None:
        """正在播放的那一首要标出来:否则用户看不出播放条上那首歌在哪一行。"""
        page = self._page([_entry(bvid="BV1"), _entry(bvid="BV2", cid=2)])
        page.set_playing("BV2", 2)
        self.assertEqual(page.list.highlighted_row(), 1)

    def test_set_playing_with_empty_bvid_clears_the_mark(self) -> None:
        """没有在播的歌时把标记撤掉,不能留着上一首的高亮。"""
        page = self._page([_entry(bvid="BV1")])
        page.set_playing("BV1", 100)
        page.set_playing("", 0)
        self.assertEqual(page.list.highlighted_row(), -1)

    def test_playing_mark_follows_the_filter(self) -> None:
        """过滤之后行号会变,高亮必须按新行号重算。"""
        page = self._page([_entry(bvid="BV1"), _entry(bvid="BV2", cid=2, title="七里香")])
        page.set_playing("BV2", 2)
        page.filter_input.setText("七里")
        self.assertEqual(page.list.highlighted_row(), 0)

    def test_list_signals_are_forwarded(self) -> None:
        """列表的入口(双击 / 加队列)都要转发出去,不能接在空处。"""
        page = self._page()
        got: list[tuple[str, int]] = []
        page.row_activated.connect(lambda row: got.append(("play", row)))
        page.add_requested.connect(lambda row: got.append(("add", row)))
        page.list.row_activated.emit(0)
        page.list.add_requested.emit(0)
        self.assertEqual(got, [("play", 0), ("add", 0)])

    def test_clear_button_emits(self) -> None:
        """清空按钮要有信号(真正的删除由主窗口确认后执行)。"""
        page = self._page()
        got: list[bool] = []
        page.clear_requested.connect(lambda: got.append(True))
        page.clear_button.click()
        self.assertEqual(got, [True])

    def test_style_hooks_are_in_place(self) -> None:
        """样式按 ``#objectName`` 选,控件必须挂上约定好的名字(与缓存页同一套)。"""
        page = self._page()
        self.assertEqual(page.objectName(), "CenterPanel")
        self.assertEqual(page.filter_input.objectName(), "FilterInput")
        self.assertEqual(page.list.objectName(), "TrackTable")
        self.assertEqual(page.clear_button.objectName(), "GhostTextButton")


# ====================================================================== 接线用例


class _WiringCase(unittest.TestCase):
    """把真的 ``PlayHistory`` 接进主窗口的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建沙箱目录、替身与主窗口,并拦掉模态弹窗。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.db = LibraryDb(self.tmp / "library.db")
        self.cache = AudioCache(self.tmp / "cache", db=self.db)
        self.history = PlayHistory(self.db)
        self.player = _FakePlayer()
        # 真的解析器 + "一被调用就失败"的客户端:离线点播但凡发一个请求,用例就红
        self.playback = PlaybackController(
            AudioResolver(_NoRequestClient(), self.cache), self.player
        )

        self.warnings: list[str] = []
        self.questions: list[str] = []
        self.question_answer = QMessageBox.StandardButton.Yes
        self._patch(QMessageBox, "warning", self._record_warning)
        self._patch(QMessageBox, "question", self._record_question)

        self.config_store = ConfigStore(self.tmp / "config.json")
        self.window = self._make_window()

    def _make_window(self, config: AppConfig | None = None) -> MainWindow:
        """按给定配置建一个主窗口(默认配置就是"没动过设置")。"""
        if config is not None:
            self.config_store.save(config)
        window = MainWindow(
            client=_NoRequestClient(),  # type: ignore[arg-type]
            cache=self.cache,
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=self.config_store,
            playback=self.playback,
            # 会话存储同样要落进沙箱:默认路径是用户真实的配置目录,不注入就会把
            # 用户真实的 session.json 当成"上次登录"读进来(见 main_window 模块 docstring)
            session_store=SessionStore(self.tmp / "session.json"),
        )
        self.addCleanup(window.close)
        return window

    def _patch(self, target, name: str, replacement: Callable) -> None:
        """打一个补丁并在用例结束时撤销。"""
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record_warning(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """记录一次 ``QMessageBox.warning``(第三个参数是正文)。"""
        self.warnings.append(args[2])

    def _record_question(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        """记录一次 ``QMessageBox.question`` 并按预设回答。"""
        self.questions.append(args[2])
        return self.question_answer

    def _seed_history(
        self,
        *,
        bvid: str = "BV1",
        cid: int = 100,
        title: str = "晴天",
        author: str = "周杰伦",
        page_index: int = 1,
        multipart: bool = False,
        played_at: float = 0.0,
        with_audio: bool = False,
    ) -> HistoryEntry:
        """往历史里放一条记录;``with_audio`` 连磁盘上的音频与索引也一起准备好。

        Args:
            bvid: 视频 BV 号。
            cid: 分P的 cid。
            title: 曲名。
            author: UP主。
            page_index: 分P序号。
            multipart: 是否多P合集。
            played_at: 播放时间戳;``0`` 表示记录时的当前时间。
            with_audio: 是否把音频文件与缓存索引也建起来(离线点播用例需要)。

        Returns:
            写进库里的那条记录。
        """
        entry = _entry(
            bvid=bvid,
            cid=cid,
            title=title,
            author=author,
            page_index=page_index,
            multipart=multipart,
            played_at=played_at,
        )
        self.history.record(entry)
        if with_audio:
            track = AudioTrack(
                quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url=""
            )
            path = self.cache.path_for(bvid, cid, 30280, "mp4a.40.2")
            path.write_bytes(b"x" * 32)
            video = Video(
                bvid=bvid,
                title=f"视频{bvid}",
                author=author,
                cid=cid,
                pages=[Page(index=page_index, cid=cid, title=title, duration=269)],
            )
            page = video.page(page_index)
            assert page is not None
            self.cache.remember(video, page, track, path)
        return entry

    def _open_history_page(self) -> None:
        """点侧栏的"最近播放"入口(顺带触发一次重新读库)。"""
        self.window.sidebar.nav_buttons["history"].click()


class TestHistoryPageWiring(_WiringCase):
    """主窗口与最近播放页的接线。"""

    def test_nav_entry_opens_the_real_page(self) -> None:
        """"最近播放"是**真的页面**,不是占位页。"""
        self._open_history_page()
        self.assertIs(self.window.pages.currentWidget(), self.window.history_page)
        self.assertTrue(self.window.sidebar.nav_buttons["history"].isChecked())

    def test_page_lists_what_the_history_holds_newest_first(self) -> None:
        """播放记录要真的出现在页面上,且最近播的在最前面。"""
        self._seed_history(bvid="BV1", cid=100, title="晴天", played_at=100.0)
        self._seed_history(bvid="BV2", cid=200, title="七里香", played_at=200.0)
        self._open_history_page()
        self.assertEqual(self.window.history_page.list.rowCount(), 2)
        self.assertEqual(self.window.history_page.list.title_at(0), "七里香")
        self.assertEqual(self.window.history_page.count_label.text(), "2 首")

    def test_page_shows_nothing_when_the_history_is_empty(self) -> None:
        """一条记录都没有时给空态页,而不是一张空表格。"""
        self._open_history_page()
        self.assertEqual(self.window.history_page.list.rowCount(), 0)
        self.assertIs(
            self.window.history_page.stack.currentWidget(),
            self.window.history_page.empty_page,
        )

    def test_double_click_plays_a_cached_song_with_zero_requests(self) -> None:
        """双击历史里的一首:已缓存的话按本地文件开播,一个网络请求都不发。

        解析器是真的,客户端替身一被调用就会断言失败 —— 所以这条用例证明的是
        "从历史里点播也能离线播",而不是"大概没发请求"。
        """
        self._seed_history(title="晴天", with_audio=True)
        self._open_history_page()
        self.window.history_page.row_activated.emit(0)
        self.assertEqual(len(self.player.loaded), 1)
        self.assertEqual(self.playback.current.video.bvid, "BV1")  # type: ignore[union-attr]

    def test_play_from_history_keeps_the_page_number(self) -> None:
        """记录里是第几P,入队就是第几P —— 丢了它多P合集会串到第 1P 上。"""
        self._seed_history(cid=300, page_index=3, multipart=True, with_audio=True)
        self._open_history_page()
        self.window.history_page.row_activated.emit(0)
        item = self.playback.current
        assert item is not None
        self.assertEqual(item.page_index, 3)
        page = item.video.page(3)
        assert page is not None
        self.assertEqual(page.cid, 300)

    def test_double_click_turns_the_visible_list_into_the_queue(self) -> None:
        """双击 = 眼前这张列表整列成为队列,从那一行开始(与缓存页同一套行为)。"""
        self._seed_history(bvid="BV1", cid=100, played_at=100.0, with_audio=True)
        self._seed_history(bvid="BV2", cid=200, played_at=200.0, with_audio=True)
        self._seed_history(bvid="BV3", cid=300, played_at=300.0, with_audio=True)
        self._open_history_page()
        self.window.history_page.row_activated.emit(1)
        self.assertEqual(len(self.playback.queue), 3)
        self.assertEqual(self.playback.current.video.bvid, "BV2")  # type: ignore[union-attr]

    def test_add_button_enqueues_without_playing(self) -> None:
        """行内"+"把这一首加到队列末尾:第一首开播,第二首只排队、不打断它。"""
        self._seed_history(bvid="BV1", cid=100, played_at=100.0, with_audio=True)
        self._seed_history(bvid="BV2", cid=200, played_at=200.0, with_audio=True)
        self._open_history_page()
        self.window.history_page.add_requested.emit(0)
        self.window.history_page.add_requested.emit(1)
        self.assertEqual(
            [item.video.bvid for item in self.playback.queue.items], ["BV2", "BV1"]
        )
        self.assertEqual(len(self.player.loaded), 1)  # 只有第一首真的开播

    def test_playing_track_is_marked_on_the_page(self) -> None:
        """正在播的那一首在历史页上要被标出来。"""
        self._seed_history(bvid="BV1", cid=100, played_at=100.0, with_audio=True)
        self._seed_history(bvid="BV2", cid=200, played_at=200.0, with_audio=True)
        self._open_history_page()
        self.window.history_page.row_activated.emit(0)
        self.assertEqual(self.window.history_page.list.highlighted_row(), 0)

    # ---------------------------------------------------------- 写入时机(A1)

    def test_audio_ready_records_one_entry(self) -> None:
        """音频真正就绪时才写记录 —— 这是"什么算听过"的唯一判据(A1)。"""
        video = Video(
            bvid="BVNEW",
            title="新歌",
            author="UP",
            cid=999,
            pages=[Page(1, 999, "新歌", 180)],
        )
        page = video.first_page()
        assert page is not None
        track = AudioTrack(quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url="")
        self.playback.audio_ready.emit(
            ResolvedAudio(video=video, page=page, track=track, path=Path("/tmp/x.m4a"))
        )
        entries = self.history.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].bvid, "BVNEW")
        self.assertEqual(entries[0].cid, 999)
        self.assertEqual(entries[0].title, "新歌")

    def test_audio_ready_shows_the_new_entry_when_the_page_is_visible(self) -> None:
        """正在历史页上播一首时,播完要立刻出现在列表里并高亮它。"""
        self._open_history_page()
        video = Video(
            bvid="BVNEW",
            title="新歌",
            author="UP",
            cid=999,
            pages=[Page(1, 999, "新歌", 180)],
        )
        page = video.first_page()
        assert page is not None
        track = AudioTrack(quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url="")
        self.playback.audio_ready.emit(
            ResolvedAudio(video=video, page=page, track=track, path=Path("/tmp/x.m4a"))
        )
        self.assertEqual(self.window.history_page.list.rowCount(), 1)
        self.assertEqual(self.window.history_page.list.highlighted_row(), 0)

    def test_replaying_the_same_track_does_not_grow_the_list(self) -> None:
        """同一首再播一次仍然只有一行(去重发生在写入侧,不是界面侧)。"""
        video = Video(
            bvid="BV1", title="晴天", author="UP", cid=100, pages=[Page(1, 100, "晴天", 269)]
        )
        page = video.first_page()
        assert page is not None
        track = AudioTrack(quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url="")
        for _ in range(2):
            self.playback.audio_ready.emit(
                ResolvedAudio(video=video, page=page, track=track, path=Path("/tmp/x.m4a"))
            )
        self.assertEqual(len(self.history.entries()), 1)

    def test_history_limit_from_config_is_applied(self) -> None:
        """上限取自 ``config.json``(没有界面入口),超出的旧记录会被裁掉。"""
        self.window = self._make_window(AppConfig(history_limit=2))
        for index in range(3):
            video = Video(
                bvid=f"BV{index}",
                title=f"第{index}首",
                author="UP",
                cid=100 + index,
                pages=[Page(1, 100 + index, f"第{index}首", 180)],
            )
            page = video.first_page()
            assert page is not None
            track = AudioTrack(
                quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url=""
            )
            self.playback.audio_ready.emit(
                ResolvedAudio(video=video, page=page, track=track, path=Path("/tmp/x.m4a"))
            )
        entries = self.history.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual([e.bvid for e in entries], ["BV2", "BV1"])

    # ---------------------------------------------------------- 删除与清空

    def test_remove_deletes_only_the_record_after_confirmation(self) -> None:
        """确认后删记录:历史少一条,**缓存文件与索引都不动**。"""
        self._seed_history(with_audio=True)
        path = self.cache.path_for("BV1", 100, 30280, "mp4a.40.2")
        self._open_history_page()
        self.window.history_page.remove_requested.emit(0)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("不会被删除", self.questions[0])
        self.assertEqual(self.history.entries(), ())
        self.assertEqual(self.window.history_page.list.rowCount(), 0)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.cache.index.entries()), 1)

    def test_remove_asks_before_deleting(self) -> None:
        """删记录也要先问一句;默认按钮是"否"。"""
        self._seed_history()
        self._open_history_page()
        self.question_answer = QMessageBox.StandardButton.No
        self.window.history_page.remove_requested.emit(0)
        self.assertEqual(len(self.history.entries()), 1)

    def test_remove_on_an_empty_row_does_nothing(self) -> None:
        """越界行号不该抛异常(过滤之后行号会变,信号可能指到空处)。"""
        self._open_history_page()
        self.window.history_page.remove_requested.emit(5)
        self.assertEqual(self.questions, [])

    def test_clear_deletes_the_history_and_keeps_the_cache(self) -> None:
        """清空历史:历史清光,缓存文件与索引一条不少。"""
        self._seed_history(bvid="BV1", cid=100, with_audio=True)
        self._seed_history(bvid="BV2", cid=200, played_at=200.0, with_audio=True)
        before = self.cache.size_bytes()
        self._open_history_page()
        self.window.history_page.clear_requested.emit()
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.history.entries(), ())
        self.assertEqual(self.window.history_page.list.rowCount(), 0)
        self.assertEqual(len(self.cache.index.entries()), 2)
        self.assertEqual(self.cache.size_bytes(), before)

    def test_clear_when_cancelled_keeps_everything(self) -> None:
        """在确认框上选"否"就什么都不该发生。"""
        self._seed_history()
        self._open_history_page()
        self.question_answer = QMessageBox.StandardButton.No
        self.window.history_page.clear_requested.emit()
        self.assertEqual(len(self.history.entries()), 1)

    def test_clear_with_an_empty_history_does_not_ask(self) -> None:
        """本来就是空的时不该弹确认框(问了也没得删)。"""
        self._open_history_page()
        self.window.history_page.clear_requested.emit()
        self.assertEqual(self.questions, [])

    def test_clearing_the_cache_does_not_touch_the_history(self) -> None:
        """清空缓存**不影响**最近播放:两者同库不同表,这一条是那层隔离的防线。"""
        self._seed_history(with_audio=True)
        self.window.cache_page.clear_requested.emit()
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.cache.index.entries(), ())
        self.assertEqual(len(self.history.entries()), 1)
        self._open_history_page()
        self.assertEqual(self.window.history_page.list.rowCount(), 1)


class TestBrokenLibraryWiring(unittest.TestCase):
    """本地库开不起来时,应用照常能用(只是没有缓存索引与历史)。

    这是 ``LibraryDb`` “对外不抛异常”那条纪律的界面侧验收:磁盘满、目录不可写、
    库文件被别的程序锁住时,用户看到的应当是一个空列表,而不是一个起不来的应用或
    一个弹到脸前的错误框。
    """

    def setUp(self) -> None:
        """把库指向一个“被文件挡住”的路径(开库必然失败)。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        self.db = LibraryDb(blocker / "sub" / "library.db")

    def test_window_still_opens_and_pages_are_empty(self) -> None:
        """窗口能建起来,两页显示空态,且写历史不报错。"""
        cache = AudioCache(self.tmp / "cache", db=self.db)
        window = MainWindow(
            client=_NoRequestClient(),  # type: ignore[arg-type]
            cache=cache,
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=ConfigStore(self.tmp / "config.json"),
            library=self.db,
            playback=PlaybackController(
                AudioResolver(_NoRequestClient(), cache), _FakePlayer()
            ),
            session_store=SessionStore(self.tmp / "session.json"),
        )
        self.addCleanup(window.close)
        self.assertFalse(self.db.available)

        window.sidebar.nav_buttons["history"].click()
        self.assertEqual(window.history_page.list.rowCount(), 0)
        self.assertIs(
            window.history_page.stack.currentWidget(), window.history_page.empty_page
        )
        window.sidebar.nav_buttons["cache"].click()
        self.assertEqual(window.cache_page.list.rowCount(), 0)

    def test_audio_ready_does_not_raise(self) -> None:
        """库写不进去也不能影响播放:``audio_ready`` 走完不报错,只是没留下记录。"""
        cache = AudioCache(self.tmp / "cache", db=self.db)
        playback = PlaybackController(
            AudioResolver(_NoRequestClient(), cache), _FakePlayer()
        )
        history = PlayHistory(self.db)
        window = MainWindow(
            client=_NoRequestClient(),  # type: ignore[arg-type]
            cache=cache,
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=ConfigStore(self.tmp / "config.json"),
            library=self.db,
            playback=playback,
            session_store=SessionStore(self.tmp / "session.json"),
        )
        self.addCleanup(window.close)
        video = Video(
            bvid="BV1", title="晴天", author="UP", cid=100, pages=[Page(1, 100, "晴天", 269)]
        )
        page = video.first_page()
        assert page is not None
        track = AudioTrack(quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url="")
        playback.audio_ready.emit(
            ResolvedAudio(video=video, page=page, track=track, path=Path("/tmp/x.m4a"))
        )
        self.assertEqual(history.entries(), ())


if __name__ == "__main__":
    unittest.main()
