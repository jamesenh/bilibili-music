"""本地缓存页的单元测试:控件行为 + 主窗口接线。

分两半:

* :class:`TestCachePageWidget` 只测控件本身(填数据、过滤、空态、高亮、信号转发),
  用离屏平台构造,不碰主窗口;
* :class:`TestCachePageWiring` 把**真的** ``AudioCache``(指向沙箱目录)接进主窗口,
  验证"点侧栏能看到缓存、双击能离线播、删除与清空真的动了文件"。

第二条特别值得一提:那里的解析器用的是**真的** :class:`~bilibili_music.audio.resolver.AudioResolver`,
而客户端替身被设计成"一被调用就让用例失败"。于是"本地缓存页点播零网络请求"这件事
是被证明的,而不是靠断言请求次数为 0 推断的。

不触网、不出声;模态弹窗(``warning`` / ``question``)全部换成记录器,否则离屏测试会挂住。
"""

from __future__ import annotations

import os
import shutil
import sys
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
from bilibili_music.core.cache_index import CachedTrack  # noqa: E402
from bilibili_music.core.config import ConfigStore  # noqa: E402
from bilibili_music.core.cover_cache import CoverCache  # noqa: E402
from bilibili_music.core.library_db import LibraryDb  # noqa: E402
from bilibili_music.core.models import AudioTrack, Page, Video  # noqa: E402
from bilibili_music.core.session import SessionStore  # noqa: E402
from bilibili_music.ui.main_window import MainWindow  # noqa: E402
from bilibili_music.ui.widgets import CachePage  # noqa: E402

#: 落盘类用例的临时目录根(与 test_cache_index / test_config 同一套做法:不用 tempfile)。
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
        self.cleared = 0

    def load(self, url: str) -> None:
        """记录一次封面请求。"""
        self.requested.append(url)

    def clear(self) -> None:
        """记录一次清空。"""
        self.cleared += 1


class _NoRequestClient:
    """接口客户端替身:**任何**请求都让用例失败(证明离线点播真的没发请求)。"""

    def fetch_video(self, bvid: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """本地缓存命中时不该补详情。"""
        raise AssertionError("离线点播不该请求详情接口")

    def fetch_audio_tracks(  # noqa: ANN001 - 替身签名
        self, bvid: str, cid: int, *, on_success, on_error
    ) -> None:
        """本地缓存命中时不该请求 playurl。"""
        raise AssertionError("离线点播不该请求 playurl 接口")

    def fetch_cover(self, url: str, *, on_success, on_error) -> None:  # noqa: ANN001
        """封面请求记下来即可(缓存页会给每行取封面)。"""
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
        """当前播放位置(离线点播用例不关心,给个 0)。"""
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


def _track(
    *,
    bvid: str = "BV1",
    cid: int = 100,
    title: str = "晴天",
    author: str = "周杰伦",
    page_index: int = 1,
    multipart: bool = True,
    quality_id: int = 30280,
    bandwidth: int = 191_900,
    duration: int = 215,
    size_bytes: int = 3 * 1024 * 1024,
) -> CachedTrack:
    """造一条缓存记录(展示相关字段都可以调)。"""
    return CachedTrack(
        file_name=f"{bvid}.m4a",
        bvid=bvid,
        cid=cid,
        quality_id=quality_id,
        codec="mp4a.40.2",
        bandwidth=bandwidth,
        title=title,
        author=author,
        page_index=page_index,
        page_title=title,
        multipart=multipart,
        duration=duration,
        cover_url="https://i0.hdslb.com/a.jpg",
        size_bytes=size_bytes,
        cached_at=1000.0,
    )


class TestCachePageWidget(unittest.TestCase):
    """本地缓存页控件本身:填数据、过滤、空态、高亮与信号转发。"""

    def _page(self, entries: list[CachedTrack] | None = None) -> CachePage:
        """造一个填好数据的页面。"""
        page = CachePage()
        page.set_tracks(entries if entries is not None else [_track()], 4 * 1024 * 1024)
        return page

    # ---------------------------------------------------------- 空态与统计

    def test_empty_state_when_nothing_is_cached(self) -> None:
        """一首都没缓存时给空态页,而不是一张空表格。"""
        page = self._page([])
        self.assertIs(page.stack.currentWidget(), page.empty_page)
        self.assertEqual(page.empty_page.title_label.full_text(), "还没有缓存任何歌曲")
        self.assertEqual(page.list.rowCount(), 0)
        self.assertFalse(page.clear_button.isEnabled())
        self.assertEqual(page.count_label.text(), "")
        self.assertEqual(page.size_label.text(), "")

    def test_summary_shows_count_and_disk_usage(self) -> None:
        """有缓存时要显示"占用多少 / 多少首"(这一页存在的理由之一)。"""
        page = self._page([_track(), _track(bvid="BV2", cid=200)])
        self.assertIs(page.stack.currentWidget(), page.list)
        self.assertEqual(page.count_label.text(), "2 首")
        self.assertEqual(page.size_label.text(), "占用 4.0 MB")
        self.assertTrue(page.clear_button.isEnabled())

    # ---------------------------------------------------------- 行内容

    def test_rows_show_quality_duration_and_size(self) -> None:
        """每行要有音质、时长与体积 —— 本地缓存页的信息就靠这三列。"""
        page = self._page()
        self.assertEqual(page.list.rowCount(), 1)
        self.assertEqual(page.list.title_at(0), "晴天")
        self.assertEqual(page.list.item(0, 2).text(), "192K")
        self.assertEqual(page.list.item(0, 3).text(), "3:35")
        self.assertEqual(page.list.item(0, 4).text(), "3.0 MB")

    def test_multipart_row_marks_the_page_number(self) -> None:
        """多P合集的记录要标出是第几P(同一视频里每P都是一首独立的歌)。"""
        page = self._page([_track(page_index=3, multipart=True)])
        self.assertEqual(page.list.subtitle_at(0), "周杰伦 · P3")

    def test_single_page_row_has_no_page_number(self) -> None:
        """单P视频不显示 ``P1``(每行都挂个 P1 只是噪声,与播放层同一口径)。"""
        page = self._page([_track(page_index=1, multipart=False)])
        self.assertEqual(page.list.subtitle_at(0), "周杰伦")

    def test_rows_request_their_cover(self) -> None:
        """有封面的行要交给加载器取图(本页用的是自己的加载器,不与搜索列表抢)。"""
        covers = _FakeCovers()
        page = CachePage(covers)  # type: ignore[arg-type]
        page.set_tracks([_track()], 0)
        self.assertEqual(covers.requested, ["https://i0.hdslb.com/a.jpg"])

    # ---------------------------------------------------------- 过滤

    def test_filter_matches_title_and_author_case_insensitively(self) -> None:
        """过滤按曲名与UP主匹配,且不区分大小写。"""
        page = self._page(
            [
                _track(bvid="BV1", title="晴天"),
                _track(bvid="BV2", cid=2, title="七里香"),
                _track(bvid="BV3", cid=3, title="Other", author="Jay"),
            ]
        )
        page.filter_input.setText("七里")
        self.assertEqual([e.bvid for e in page.visible_entries()], ["BV2"])
        page.filter_input.setText("jay")
        self.assertEqual([e.bvid for e in page.visible_entries()], ["BV3"])
        self.assertEqual(page.list.rowCount(), 1)

    def test_filtered_view_reports_its_own_row_numbers(self) -> None:
        """过滤之后行号以可见列表为准 —— 信号里传的行号必须与 ``entry_at`` 同一套。"""
        page = self._page([_track(bvid="BV1"), _track(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        self.assertEqual(page.entry_at(0).bvid, "BV2")  # type: ignore[union-attr]
        self.assertIsNone(page.entry_at(1))

    def test_summary_counts_within_the_filter(self) -> None:
        """过滤中要显示"可见 / 总数",否则用户不知道是不是缓存丢了。"""
        page = self._page([_track(bvid="BV1"), _track(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        self.assertEqual(page.count_label.text(), "1 / 2 首")

    def test_no_match_shows_a_filter_specific_empty_state(self) -> None:
        """过滤到一条不剩时,文案要指向"过滤条件"而不是"还没有缓存"。"""
        page = self._page([_track()])
        page.filter_input.setText("不存在的东西")
        self.assertIs(page.stack.currentWidget(), page.empty_page)
        self.assertEqual(page.empty_page.title_label.full_text(), "没有匹配的缓存")

    def test_clearing_the_filter_restores_every_row(self) -> None:
        """清空过滤框要恢复全部行(过滤不该动原始数据)。"""
        page = self._page([_track(bvid="BV1"), _track(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        page.filter_input.setText("")
        self.assertEqual(len(page.visible_entries()), 2)

    def test_new_data_keeps_the_filter(self) -> None:
        """上层刷新数据(删掉一首、刚缓存一首)时不该把用户打的关键字清掉。"""
        page = self._page([_track(bvid="BV1"), _track(bvid="BV2", cid=2, title="七里香")])
        page.filter_input.setText("七里")
        page.set_tracks([_track(bvid="BV2", cid=2, title="七里香")], 1024)
        self.assertEqual(page.filter_input.text(), "七里")
        self.assertEqual(len(page.visible_entries()), 1)

    # ---------------------------------------------------------- 高亮与信号

    def test_set_playing_marks_the_matching_row(self) -> None:
        """正在播放的那一首要标出来:否则用户看不出播放条上那首歌在哪一行。"""
        page = self._page([_track(bvid="BV1"), _track(bvid="BV2", cid=2)])
        page.set_playing("BV2", 2)
        self.assertEqual(page.list.highlighted_row(), 1)

    def test_set_playing_with_empty_bvid_clears_the_mark(self) -> None:
        """没有在播的歌时把标记撤掉,不能留着上一首的高亮。"""
        page = self._page([_track(bvid="BV1")])
        page.set_playing("BV1", 100)
        page.set_playing("", 0)
        self.assertEqual(page.list.highlighted_row(), -1)

    def test_playing_mark_follows_the_filter(self) -> None:
        """过滤之后行号会变,高亮必须按新行号重算。"""
        page = self._page([_track(bvid="BV1"), _track(bvid="BV2", cid=2, title="七里香")])
        page.set_playing("BV2", 2)
        page.filter_input.setText("七里")
        self.assertEqual(page.list.highlighted_row(), 0)

    def test_list_signals_are_forwarded(self) -> None:
        """列表的四个入口(双击 / 右键 / 加队列)都要转发出去,不能接在空处。"""
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

    def test_download_task_button_emits(self) -> None:
        """"下载任务"按钮要把请求转给主窗口(对话框是主窗口的,控件不自己开窗)。"""
        page = self._page()
        got: list[bool] = []
        page.tasks_requested.connect(lambda: got.append(True))
        page.tasks_button.click()
        self.assertEqual(got, [True])

    def test_open_dir_button_emits(self) -> None:
        """"打开缓存目录"按钮同上:控件只发信号,不开目录也不读盘。"""
        page = self._page()
        got: list[bool] = []
        page.open_dir_requested.connect(lambda: got.append(True))
        page.open_dir_button.click()
        self.assertEqual(got, [True])

    def test_download_task_button_shows_the_unfinished_count(self) -> None:
        """未完成任务数写在按钮上;都干完了就把数字去掉,回到一个安静的入口。"""
        page = self._page()
        self.assertEqual(page.tasks_button.text(), "下载任务")
        page.set_task_summary(3)
        self.assertEqual(page.tasks_button.text(), "下载任务 (3)")
        self.assertIn("3 个任务未完成", page.tasks_button.toolTip())
        page.set_task_summary(0)
        self.assertEqual(page.tasks_button.text(), "下载任务")
        self.assertNotIn("0 个", page.tasks_button.toolTip())

    def test_style_hooks_are_in_place(self) -> None:
        """样式按 ``#objectName`` 选,控件必须挂上约定好的名字(过滤框是新的一个)。"""
        page = self._page()
        self.assertEqual(page.objectName(), "CenterPanel")
        self.assertEqual(page.filter_input.objectName(), "FilterInput")
        self.assertEqual(page.list.objectName(), "TrackTable")
        self.assertEqual(page.clear_button.objectName(), "GhostTextButton")
        self.assertEqual(page.tasks_button.objectName(), "GhostTextButton")
        self.assertEqual(page.open_dir_button.objectName(), "GhostTextButton")


# ====================================================================== 接线用例


class _WiringCase(unittest.TestCase):
    """把真的 ``AudioCache`` 接进主窗口的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建沙箱缓存目录、替身与主窗口,并拦掉模态弹窗。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.cache = AudioCache(self.tmp / "cache", db=LibraryDb(self.tmp / "library.db"))
        self.player = _FakePlayer()
        # 真的解析器 + "一被调用就失败"的客户端:离线点播但凡发一个请求,用例就红
        self.playback = PlaybackController(
            AudioResolver(_NoRequestClient(), self.cache),  # type: ignore[arg-type]
            self.player,
        )

        self.warnings: list[str] = []
        self.questions: list[str] = []
        self.question_answer = QMessageBox.StandardButton.Yes
        self._patch(QMessageBox, "warning", self._record_warning)
        self._patch(QMessageBox, "question", self._record_question)

        self.window = MainWindow(
            client=_NoRequestClient(),  # type: ignore[arg-type]
            cache=self.cache,
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=ConfigStore(self.tmp / "config.json"),
            playback=self.playback,
            # 会话存储必须落进沙箱:默认路径是用户真实的配置目录,不注入就会去读
            # 用户真实的 session.json(见 main_window 的模块 docstring)
            session_store=SessionStore(self.tmp / "session.json"),
        )
        self.addCleanup(self.window.close)

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

    def _seed(
        self,
        *,
        bvid: str = "BV1",
        cid: int = 100,
        title: str = "晴天",
        author: str = "周杰伦",
        page_index: int = 1,
        multipart: bool = True,
        quality_id: int = 30280,
        size: int = 2048,
    ) -> Path:
        """往缓存里放一条"文件 + 索引"都齐的记录。

        Returns:
            音频文件路径。
        """
        pages = [Page(index=page_index, cid=cid, title=title, duration=215)]
        if multipart:
            pages.append(Page(index=page_index + 1, cid=cid + 1, title="另一首", duration=200))
        video = Video(
            bvid=bvid,
            title=f"视频{bvid}",
            author=author,
            cid=cid,
            cover_url="//i0.hdslb.com/a.jpg",
            pages=pages,
        )
        page = video.page(page_index)
        assert page is not None
        path = self.cache.path_for(bvid, cid, quality_id, "mp4a.40.2")
        path.write_bytes(b"x" * size)
        track = AudioTrack(quality_id=quality_id, codec="mp4a.40.2", bandwidth=191_900, url="")
        self.cache.remember(video, page, track, path)
        return path

    def _open_cache_page(self) -> None:
        """点侧栏的"本地缓存"入口(顺带触发一次索引刷新)。"""
        self.window.sidebar.nav_buttons["cache"].click()


class TestCachePageWiring(_WiringCase):
    """主窗口与本地缓存页的接线。"""

    def test_nav_entry_opens_the_real_page(self) -> None:
        """"本地缓存"现在是**真的页面**,不再落进占位页(路线图 M2.2 已落地)。"""
        self._open_cache_page()
        self.assertIs(self.window.pages.currentWidget(), self.window.cache_page)
        self.assertTrue(self.window.sidebar.nav_buttons["cache"].isChecked())

    def test_page_lists_what_the_index_holds(self) -> None:
        """缓存的歌要真的出现在页面上(索引 -> 界面这条链路)。"""
        self._seed(title="晴天")
        self._seed(bvid="BV2", cid=200, title="七里香", size=4096)
        self._open_cache_page()
        self.assertEqual(self.window.cache_page.list.rowCount(), 2)
        self.assertEqual(self.window.cache_page.count_label.text(), "2 首")
        self.assertEqual(
            self.window.cache_page.size_label.text(),
            "占用 6 KB",
        )

    def test_page_shows_nothing_when_the_cache_is_empty(self) -> None:
        """缓存是空的时给空态页,而不是一张空表格。"""
        self._open_cache_page()
        self.assertEqual(self.window.cache_page.list.rowCount(), 0)
        self.assertIs(
            self.window.cache_page.stack.currentWidget(),
            self.window.cache_page.empty_page,
        )

    def test_refresh_drops_entries_whose_file_was_deleted(self) -> None:
        """文件被应用之外删掉的记录要在刷新时剪掉(否则会列出点不开的歌)。"""
        path = self._seed()
        path.unlink()
        self._open_cache_page()
        self.assertEqual(self.window.cache_page.list.rowCount(), 0)
        self.assertEqual(self.cache.index.entries(), ())

    def test_double_click_plays_offline_with_zero_requests(self) -> None:
        """双击缓存里的一首:按本地文件开播,一个网络请求都不发。

        解析器是真的,客户端替身一被调用就会断言失败 —— 所以这条用例证明的是
        "离线能播",而不是"大概没发请求"。
        """
        path = self._seed(title="晴天")
        self._open_cache_page()
        self.window.cache_page.row_activated.emit(0)
        self.assertEqual(self.player.loaded, [path])
        self.assertEqual(self.playback.current.video.bvid, "BV1")  # type: ignore[union-attr]

    def test_offline_play_keeps_the_cached_page_number(self) -> None:
        """记录里是第几P,入队就是第几P —— 丢了它多P合集会串到第 1P 上。"""
        self._seed(cid=300, page_index=3)
        self._open_cache_page()
        self.window.cache_page.row_activated.emit(0)
        item = self.playback.current
        assert item is not None
        self.assertEqual(item.page_index, 3)
        page = item.video.page(3)
        assert page is not None
        self.assertEqual(page.cid, 300)

    def test_double_click_turns_the_visible_list_into_the_queue(self) -> None:
        """双击 = 眼前这张列表整列成为队列,从那一行开始(与搜索结果页同一套行为)。"""
        self._seed(bvid="BV1", cid=100)
        self._seed(bvid="BV2", cid=200)
        self._seed(bvid="BV3", cid=300)
        self._open_cache_page()
        self.window.cache_page.row_activated.emit(1)
        self.assertEqual(len(self.playback.queue), 3)
        self.assertEqual(self.playback.current.video.bvid, "BV2")  # type: ignore[union-attr]

    def test_add_button_enqueues_without_playing(self) -> None:
        """行内"+"把这一首加到队列末尾,不改动已经在播/在排的那一首。

        索引是"最近缓存的排前面",所以后 seed 的 BV2 在第 0 行(与界面上看到的顺序一致)。
        第一次入队时队列本来是空的,按编排层的既有规则会直接开播;第二次就不会打断它。
        """
        self._seed(bvid="BV1", cid=100)
        self._seed(bvid="BV2", cid=200)
        self._open_cache_page()
        entry = self.window.cache_page.entry_at(0)
        assert entry is not None
        self.assertEqual(entry.bvid, "BV2")  # 第 0 行是最近缓存的那一首
        self.window.cache_page.add_requested.emit(0)
        self.window.cache_page.add_requested.emit(1)
        self.assertEqual(
            [item.video.bvid for item in self.playback.queue.items], ["BV2", "BV1"]
        )
        # 只加载了第一首(空队列入队即播);第二首只是排队,没有打断
        self.assertEqual(len(self.player.loaded), 1)

    def test_playing_track_is_marked_on_the_page(self) -> None:
        """正在播的那一首在缓存页上要被标出来(切页之后也还在)。"""
        self._seed(bvid="BV1", cid=100)
        self._seed(bvid="BV2", cid=200)
        self._open_cache_page()
        self.window.cache_page.row_activated.emit(1)
        self.assertEqual(self.window.cache_page.list.highlighted_row(), 1)

    # ---------------------------------------------------------- 删除与清空

    def test_remove_deletes_file_and_index_after_confirmation(self) -> None:
        """确认后删除:文件与索引记录都要没,页面跟着刷新。"""
        path = self._seed()
        self._open_cache_page()
        self.window.cache_page.remove_requested.emit(0)
        self.assertEqual(len(self.questions), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.cache.index.entries(), ())
        self.assertEqual(self.window.cache_page.list.rowCount(), 0)

    def test_remove_asks_before_deleting(self) -> None:
        """删除不可撤销,必须先问一句;默认按钮是"否"。"""
        path = self._seed()
        self._open_cache_page()
        self.question_answer = QMessageBox.StandardButton.No
        self.window.cache_page.remove_requested.emit(0)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.cache.index.entries()), 1)

    def test_remove_failure_is_reported(self) -> None:
        """删不掉(文件被播放器占着)时要说明原因,不能静默失败。"""
        self._seed()
        self._open_cache_page()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("被占用")):
            self.window.cache_page.remove_requested.emit(0)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("正在被播放器使用", self.warnings[0])
        self.assertEqual(len(self.cache.index.entries()), 1)  # 索引保持不动,还能再试

    def test_remove_on_an_empty_row_does_nothing(self) -> None:
        """越界行号不该抛异常(过滤之后行号会变,信号可能指到空处)。"""
        self._open_cache_page()
        self.window.cache_page.remove_requested.emit(5)
        self.assertEqual(self.questions, [])

    def test_clear_deletes_everything_after_confirmation(self) -> None:
        """清空缓存:文件与索引一起清,页面回到空态。"""
        self._seed(bvid="BV1", cid=100)
        self._seed(bvid="BV2", cid=200)
        self._open_cache_page()
        self.window.cache_page.clear_requested.emit()
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.cache.index.entries(), ())
        self.assertEqual(self.cache.size_bytes(), 0)
        self.assertEqual(self.window.cache_page.list.rowCount(), 0)
        self.assertEqual(self.warnings, [])

    def test_clear_when_cancelled_keeps_everything(self) -> None:
        """在确认框上选"否"就什么都不该发生。"""
        path = self._seed()
        self._open_cache_page()
        self.question_answer = QMessageBox.StandardButton.No
        self.window.cache_page.clear_requested.emit()
        self.assertTrue(path.exists())
        self.assertEqual(len(self.cache.index.entries()), 1)

    def test_clear_with_an_empty_cache_does_not_ask(self) -> None:
        """本来就是空的时不该弹确认框(问了也没得删)。"""
        self._open_cache_page()
        self.window.cache_page.clear_requested.emit()
        self.assertEqual(self.questions, [])

    # ---------------------------------------------------------- 与播放联动

    def test_audio_ready_marks_the_playing_row_when_the_page_is_visible(self) -> None:
        """正在缓存页上播一首时,播完要立刻出现在列表里并高亮它。

        索引那一笔由解析器在成功出口写入(见 ``AudioResolver._remember``),这里照着
        那一步手动补上 —— 用例要验的是界面在收到 ``audio_ready`` 之后会不会刷新。
        """
        self._open_cache_page()
        track = AudioTrack(quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url="")
        path = self.cache.path_for("BVNEW", 999, 30280, "mp4a.40.2")
        path.write_bytes(b"x" * 16)
        video = Video(
            bvid="BVNEW",
            title="新歌",
            author="UP",
            cid=999,
            pages=[Page(1, 999, "新歌", 180)],
        )
        page = video.first_page()
        assert page is not None
        self.cache.remember(video, page, track, path)
        resolved = ResolvedAudio(video=video, page=page, track=track, path=path)
        self.playback.audio_ready.emit(resolved)
        self.assertEqual(self.window.cache_page.list.rowCount(), 1)
        self.assertEqual(self.window.cache_page.list.highlighted_row(), 0)


if __name__ == "__main__":
    unittest.main()
