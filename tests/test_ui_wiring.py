"""界面接线测试:在离屏平台上真的把主窗口造出来,验证信号接对了。

这是 M1 里唯一能**自动**验证界面接线的办法。MVP 阶段界面没有测试,改一处信号连接
只能手动开应用去点 —— 而"双击搜索结果会不会真的换掉队列""点下一首会不会真的前进"
这类问题正是拆 widget 时最容易接错的。

离屏平台(``QT_QPA_PLATFORM=offscreen``)让 ``QWidget`` 在无显示器环境里也能构造。

**不触网、不出声**:解析器与播放器都换成替身(真实解析器会发请求,真实播放器会构造
``QMediaPlayer``)。``QMessageBox`` 被替换成记录器,否则一旦走到报错路径,模态弹窗会把
测试挂住。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须在建应用实例之前设置,否则无显示环境下起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 注意导入顺序:先 QtWidgets/QtGui 再 QtCore(见 AGENTS.md 第 5 节)
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox  # noqa: E402
from PySide6.QtGui import QDesktopServices, QPalette, QPixmap, QPixmapCache  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QObject, QPoint, Qt, Signal, qVersion  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from bilibili_music.api.bilibili import (  # noqa: E402
    AccountInfo,
    FavFolder,
    FavItem,
    SearchResult,
    track_from_quality,
)
from bilibili_music.api.bilibili import FavPage as FavPageData  # noqa: E402
from bilibili_music.audio.playback import PlaybackController  # noqa: E402
from bilibili_music.audio.resolver import ResolvedAudio  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.config import AppConfig, ConfigStore  # noqa: E402
from bilibili_music.core.cover_cache import CoverCache  # noqa: E402
from bilibili_music.core.errors import NetworkError  # noqa: E402
from bilibili_music.core.library_db import LibraryDb  # noqa: E402
from bilibili_music.core.logging_setup import ENVIRONMENT_FILE_NAME  # noqa: E402
from bilibili_music.core.models import Page, Video  # noqa: E402
from bilibili_music.core.queue import PlayMode  # noqa: E402
from bilibili_music.core.session import SessionStore, session_from_cookies  # noqa: E402
from bilibili_music.ui import main_window as main_window_module  # noqa: E402
from bilibili_music.ui.icons import DARK  # noqa: E402
from bilibili_music.ui.main_window import _SEARCH_MAX_PAGES, MainWindow  # noqa: E402
from bilibili_music.ui.theme import SURFACES  # noqa: E402
from bilibili_music.ui.widgets import FavVisibilityDialog  # noqa: E402
from bilibili_music.ui.widgets.page_selector import EMPTY_LABEL  # noqa: E402

#: 落盘类用例的临时目录根(与 test_config 同一套做法:不用 tempfile)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``;进程里已经有一个就复用。

    测试进程只允许存在一个应用实例,而 ``tests/test_icons.py`` 也会建 —— 复用是
    唯一可行的做法(两处都用 ``QApplication``,不用 ``QGuiApplication``)。
    """
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


# ====================================================================== 替身


@dataclass(slots=True)
class _Pending:
    """一次解析请求的回调组,交给用例自己触发。"""

    on_success: Callable[[ResolvedAudio], None]
    on_error: Callable[[Exception], None]


class _FakeResolver:
    """解析器替身:记录请求,把回调攒下来供用例触发。"""

    def __init__(self) -> None:
        """建一个没有在飞请求的替身。"""
        self.calls: list[tuple[Video, int, int | None]] = []
        self.cancel_count = 0
        self._pending: _Pending | None = None

    def resolve(
        self,
        video: Video,
        *,
        page_index: int = 1,
        quality_id: int | None = None,
        on_success: Callable[[ResolvedAudio], None],
        on_error: Callable[[Exception], None],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """记录一次解析请求(不发任何网络动作)。"""
        self.calls.append((video, page_index, quality_id))
        self._pending = _Pending(on_success, on_error)

    def cancel(self) -> None:
        """记录一次取消并丢弃待触发的回调。"""
        self.cancel_count += 1
        self._pending = None

    def succeed(self, resolved: ResolvedAudio) -> None:
        """触发最近一次请求的成功回调。"""
        assert self._pending is not None, "没有在飞的解析请求"
        self._pending.on_success(resolved)

    def fail(self, exc: Exception) -> None:
        """触发最近一次请求的失败回调。"""
        assert self._pending is not None, "没有在飞的解析请求"
        self._pending.on_error(exc)


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
        self.seeks: list[int] = []
        self.volumes: list[float] = []
        self.stop_count = 0
        self.play_count = 0
        self.pause_count = 0
        self.toggle_count = 0
        self._position_ms = 0
        self._is_playing = False

    @property
    def is_playing(self) -> bool:
        """是否处于播放状态。"""
        return self._is_playing

    @property
    def position_ms(self) -> int:
        """当前播放位置;用例直接改它来模拟"播到一半"。"""
        return self._position_ms

    def load(self, path: Path, *, autoplay: bool = True) -> None:
        """记录一次加载。"""
        self.loaded.append(Path(path))
        self._is_playing = autoplay

    def play(self) -> None:
        """记录一次继续播放。"""
        self.play_count += 1
        self._is_playing = True

    def pause(self) -> None:
        """记录一次暂停。"""
        self.pause_count += 1
        self._is_playing = False

    def toggle(self) -> None:
        """记录一次播放/暂停切换。"""
        self.toggle_count += 1
        self._is_playing = not self._is_playing

    def stop(self) -> None:
        """记录一次停止。"""
        self.stop_count += 1
        self._is_playing = False

    def seek(self, position_ms: int) -> None:
        """记录一次跳转。"""
        self.seeks.append(int(position_ms))

    def set_volume(self, volume: float) -> None:
        """记录一次音量设置。"""
        self.volumes.append(float(volume))


@dataclass(slots=True)
class _PendingSearch:
    """一次还没被应答的搜索请求(用来模拟慢响应与"迟到的旧回调")。"""

    seq: int
    keyword: str
    page: int
    on_success: Callable[[SearchResult], None]
    on_error: Callable[[Exception], None]

    def succeed(self, result: SearchResult) -> None:
        """触发这一页的成功回调。"""
        self.on_success(result)

    def fail(self, exc: Exception) -> None:
        """触发这一页的失败回调。"""
        self.on_error(exc)


class _FakeFetchHandle:
    """搜索请求句柄替身:记录取消,并按契约丢弃尚未触发的回调。"""

    def __init__(self, client: _FakeClient, seq: int) -> None:
        """绑定所属客户端与请求序号。

        Args:
            client: 发起这次请求的替身客户端。
            seq: 请求序号(第几次请求,1 起)。
        """
        self._client = client
        self._seq = seq

    def cancel(self) -> None:
        """记录取消;真实后端保证"取消后两个回调都不再触发",这里照做。"""
        self._client.cancelled.append(self._seq)
        self._client.pending = [p for p in self._client.pending if p.seq != self._seq]


class _FakeBackend:
    """网络后端替身:只记录会话注入/清空与预热,不发任何请求。

    主窗口会在登录、登出、启动恢复三处直接碰后端(``set_session_cookies`` 等),
    而这些都是"必须发生、且顺序有讲究"的动作(登出要连匿名 Cookie 一起清、
    清完还要强制预热),所以替身把它们记下来供断言。
    """

    def __init__(self) -> None:
        """建一个空记录。"""
        #: 每次注入的 Cookie 副本(按调用顺序)
        self.injected: list[dict[str, str]] = []
        self.clear_count = 0
        self.warmups: list[bool] = []

    def set_session_cookies(self, cookies: dict[str, str]) -> None:
        """记录一次会话注入。"""
        self.injected.append(dict(cookies))

    def clear_session_cookies(self) -> None:
        """记录一次会话清空。"""
        self.clear_count += 1

    def warm_up(self, *, force: bool = False) -> None:
        """记录一次预热(只关心 force)。"""
        self.warmups.append(bool(force))


class _FakeClient:
    """接口客户端替身:只实现界面用到的那些 ``fetch_*`` / ``search_video`` / ``close``。"""

    def __init__(
        self,
        videos: list[Video],
        *,
        pages: dict[int, list[Video]] | None = None,
        total_pages: int = 1,
        total: int | None = None,
        defer: bool = False,
        fail_pages: set[int] | None = None,
    ) -> None:
        """记录要返回的搜索结果。

        Args:
            videos: 不翻页时每次搜索都返回这一批(多数用例只关心"搜出来一批")。
            pages: 页码 -> 该页视频;给了它就按 ``page`` 取,``videos`` 只当兜底。
            total_pages: 接口自称的总页数(``numPages``)。
            total: 接口自称的命中总数;``None`` 表示用本页条数。
            defer: ``True`` 表示**不立刻应答**,把回调攒进 :attr:`pending` 交给用例
                (只有竞态用例需要:同步应答的话根本来不及插进第二次搜索)。
            fail_pages: 这些页码的请求改走 ``on_error``(模拟风控 412 / 网络失败)。
        """
        self.videos = list(videos)
        self.pages = dict(pages) if pages is not None else None
        self.total_pages = total_pages
        self.total = total
        self.defer = defer
        self.fail_pages = set(fail_pages or ())
        self.keywords: list[str] = []
        #: 每次搜索请求的 (关键字, 页码),用来断言"翻了第几页、一共翻了几次"
        self.calls: list[tuple[str, int]] = []
        #: 被 ``cancel()`` 取消过的请求序号
        self.cancelled: list[int] = []
        #: defer 模式下还没被应答的请求
        self.pending: list[_PendingSearch] = []
        self.closed = 0
        self.cover_calls: list[str] = []
        #: URL -> 还没被应答的回调组**列表**。列表而不是单个回调:搜索列表、队列面板与
        #: 播放条各有自己的封面加载器,同一张图会被请求多次,每次请求都是独立的一笔。
        self._cover_callbacks: dict[str, list[tuple[Callable, Callable]]] = {}

        # ---------------------------------------------------------- 账号与收藏夹
        #: 后端替身(记录会话注入 / 清空 / 预热)
        self.backend = _FakeBackend()
        #: ``fetch_nav`` 要回的结果;``None`` 表示走 :attr:`nav_error`
        self.nav: AccountInfo | None = AccountInfo(
            is_login=True, mid=42, uname="测试账号", vip_type=1
        )
        #: ``fetch_nav`` 的失败原因;非 ``None`` 时优先于 :attr:`nav`
        self.nav_error: Exception | None = None
        #: ``fetch_nav`` 被调用的次数
        self.nav_calls = 0
        #: ``fetch_fav_folders`` 要回的收藏夹
        self.folders: list[FavFolder] = []
        self.folder_calls: list[int] = []
        #: 为真时收藏夹**列表**的请求**不应答**,把回调攒进 :attr:`pending_folders`
        #: (只有"刷新期间连点 / 登出后旧列表才回来"这类竞态用例需要)
        self.folders_defer = False
        self.pending_folders: list[
            tuple[int, Callable[[list[FavFolder]], None], Callable[[Exception], None]]
        ] = []
        #: 收藏夹内容: ``(media_id, 页码)`` -> 该页数据
        self.fav_pages: dict[tuple[int, int], FavPageData] = {}
        self.fav_calls: list[tuple[int, int]] = []
        #: 为真时收藏夹内容的请求**不应答**,把回调攒进 :attr:`pending_fav`
        #: (只有竞态用例需要:同步应答根本来不及插进"切到另一个收藏夹")
        self.fav_defer = False
        self.pending_fav: list[tuple[int, int, Callable[[FavPageData], None]]] = []
        #: ``fetch_video`` 要回的详情(按 bvid);缺失时按 :func:`_video` 现造一个
        self.video_details: dict[str, Video] = {}
        self.video_calls: list[str] = []

    # ------------------------------------------------------------ 账号与收藏夹

    def fetch_nav(
        self,
        *,
        on_success: Callable[[AccountInfo], None],
        on_error: Callable[[Exception], None],
    ) -> "_FakeFetchHandle":
        """应答一次登录态查询(``nav_error`` 优先)。"""
        self.nav_calls += 1
        handle = _FakeFetchHandle(self, self.nav_calls)
        if self.nav_error is not None:
            on_error(self.nav_error)
        else:
            on_success(self.nav or AccountInfo())
        return handle

    def fetch_fav_folders(
        self,
        mid: int,
        *,
        on_success: Callable[[list[FavFolder]], None],
        on_error: Callable[[Exception], None],
    ) -> "_FakeFetchHandle":
        """应答一次收藏夹列表查询;``folders_defer`` 为真时攒起来等用例触发。"""
        self.folder_calls.append(mid)
        handle = _FakeFetchHandle(self, len(self.folder_calls))
        if self.folders_defer:
            self.pending_folders.append((mid, on_success, on_error))
            return handle
        on_success(list(self.folders))
        return handle

    def succeed_folders(self, folders: list[FavFolder] | None = None) -> None:
        """手动应答一次 defer 住的收藏夹列表请求。

        Args:
            folders: 要回的数据;``None`` 表示按 :attr:`folders` 回。
        """
        if not self.pending_folders:
            raise AssertionError("没有在飞的收藏夹列表请求")
        _, on_success, _ = self.pending_folders.pop(0)
        on_success(list(self.folders if folders is None else folders))

    def fail_folders(self, exc: Exception) -> None:
        """手动让一次 defer 住的收藏夹列表请求失败。

        Args:
            exc: 交给 ``on_error`` 的失败原因。
        """
        if not self.pending_folders:
            raise AssertionError("没有在飞的收藏夹列表请求")
        _, _, on_error = self.pending_folders.pop(0)
        on_error(exc)

    def fetch_fav_page(
        self,
        media_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
        on_success: Callable[[FavPageData], None],
        on_error: Callable[[Exception], None],
    ) -> "_FakeFetchHandle":
        """应答一次收藏夹内容查询(没配的页返回空页);``fav_defer`` 为真时攒起来。"""
        self.fav_calls.append((media_id, page))
        handle = _FakeFetchHandle(self, len(self.fav_calls))
        if self.fav_defer:
            self.pending_fav.append((media_id, page, on_success))
            return handle
        data = self.fav_pages.get((media_id, page))
        if data is None:
            on_success(FavPageData(items=[], media_id=media_id, media_count=0, has_more=False))
        else:
            on_success(data)
        return handle

    def succeed_fav(self, media_id: int, page: int, data: FavPageData) -> None:
        """手动应答一次 defer 住的收藏夹请求(没配的页按空页处理)。

        Args:
            media_id: 请求时的收藏夹 id(用来挑出对应那一条)。
            page: 请求时的页码。
            data: 要回的数据。
        """
        for index, (pending_id, pending_page, on_success) in enumerate(self.pending_fav):
            if (pending_id, pending_page) == (media_id, page):
                del self.pending_fav[index]
                on_success(data)
                return
        raise AssertionError(f"没有在飞的收藏夹请求: media_id={media_id} page={page}")

    def fetch_video(
        self,
        bvid: str,
        *,
        on_success: Callable[[Video], None],
        on_error: Callable[[Exception], None],
        use_cache: bool = True,
    ) -> "_FakeFetchHandle":
        """应答一次视频详情查询(没配的按单P现造)。"""
        self.video_calls.append(bvid)
        handle = _FakeFetchHandle(self, len(self.video_calls))
        on_success(self.video_details.get(bvid) or _video(bvid))
        return handle

    def cached_video(self, bvid: str) -> Video | None:
        """详情缓存替身:一律未命中(用例要的是"发了一次详情请求")。"""
        return None

    # ------------------------------------------------------------ 搜索与封面

    def result_for(self, page: int, videos: list[Video] | None = None) -> SearchResult:
        """按当前配置造一份"第 page 页"的响应(供 defer 模式手动应答)。

        Args:
            page: 页码。
            videos: 覆盖这一页的视频;``None`` 表示按 :attr:`pages` / :attr:`videos` 取。

        Returns:
            可直接交给 ``_PendingSearch.succeed`` 的响应。
        """
        if videos is None:
            videos = (
                list(self.pages.get(page, []))
                if self.pages is not None
                else list(self.videos)
            )
        return SearchResult(
            videos=list(videos),
            page=page,
            total_pages=self.total_pages,
            total=self.total if self.total is not None else len(videos),
        )

    def search_video(
        self,
        keyword: str,
        *,
        page: int = 1,
        on_success: Callable[[SearchResult], None],
        on_error: Callable[[Exception], None],
    ) -> _FakeFetchHandle:
        """应答一页搜索结果;``defer`` 为真时改为攒起来等用例触发。"""
        self.keywords.append(keyword)
        self.calls.append((keyword, page))
        seq = len(self.calls)
        handle = _FakeFetchHandle(self, seq)
        if page in self.fail_pages:
            on_error(NetworkError(f"HTTP 412(第 {page} 页)"))
            return handle
        if self.defer:
            self.pending.append(_PendingSearch(seq, keyword, page, on_success, on_error))
        else:
            on_success(self.result_for(page))
        return handle

    def fetch_cover(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """记录封面请求,把回调攒下来交给用例触发(按 URL 存,能模拟慢响应)。"""
        self.cover_calls.append(url)
        self._cover_callbacks.setdefault(url, []).append((on_success, on_error))

    def pending_cover_urls(self) -> list[str]:
        """还有回调没被应答的封面地址(按注册顺序)。"""
        return [url for url, callbacks in self._cover_callbacks.items() if callbacks]

    def succeed_cover(self, url: str, data: bytes) -> None:
        """触发某个封面 URL 的**全部**成功回调(几个加载器可能都在等这张图)。"""
        for on_success, _ in self._cover_callbacks.pop(url, []):
            on_success(data)

    def fail_cover(self, url: str, exc: Exception) -> None:
        """触发某个封面 URL 的全部失败回调。"""
        for _, on_error in self._cover_callbacks.pop(url, []):
            on_error(exc)

    def close(self) -> None:
        """记录一次关闭。"""
        self.closed += 1


# ====================================================================== 工具


def _folder(media_id: int, title: str, count: int = 0, *, private: bool = False) -> FavFolder:
    """造一个收藏夹样本(``attr`` 的私密位是 2,与 ``fav_folders`` 的实测口径一致)。"""
    return FavFolder(
        media_id=media_id, title=title, media_count=count, attr=2 if private else 0
    )


def _fav_item(
    bvid: str,
    title: str = "",
    *,
    attr: int = 0,
    item_type: int = 2,
    page_count: int = 1,
    duration: int = 180,
) -> FavItem:
    """造一条收藏夹条目样本(``attr≠0`` 即失效,``type=2`` 是视频)。"""
    return FavItem(
        bvid=bvid,
        title=title or f"收藏{bvid}",
        author="某UP",
        duration=duration,
        page_count=page_count,
        attr=attr,
        item_type=item_type,
    )


def _video(bvid: str, *, pages: int = 1) -> Video:
    """造一个带分P与封面的视频样本(纯内存)。"""
    return Video(
        bvid=bvid,
        title=f"视频{bvid}",
        author="某UP",
        cid=1000,
        cover_url=f"//i0.hdslb.com/{bvid}.jpg",
        pages=[
            Page(index=i + 1, cid=1000 + i, title=f"第{i + 1}首", duration=180)
            for i in range(pages)
        ],
    )


def _png_bytes() -> bytes:
    """造一段**真能解码**的 PNG,用来区分"有封面"与"只有占位图"。"""
    pixmap = QPixmap(4, 4)
    pixmap.fill(Qt.GlobalColor.red)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


def _resolved(video: Video, *, page_index: int = 1, quality_id: int = 30280) -> ResolvedAudio:
    """按视频造一份解析结果,路径是假的(不会被真正读取)。"""
    page = video.page(page_index)
    assert page is not None
    return ResolvedAudio(
        video=video,
        page=page,
        track=track_from_quality(quality_id),
        path=Path(f"C:/fake/{video.bvid}.m4a"),
    )


class _WindowCase(unittest.TestCase):
    """装好替身与主窗口的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建逐用例的临时目录、替身与主窗口;并拦掉模态弹窗。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        # 用 cleanup 而不是 tearDown 来删目录:cleanup 是**后进先出**的,所以这条
        # 最先注册、最后执行。个别用例会额外建窗口并用 addCleanup(window.close)
        # 注册关闭 —— 关闭会写配置,若把删目录放在 tearDown 里,就会在删除之后
        # 又被写回来,在工作区里留下 tests/_scratch 的残骸。
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # QPixmapCache 是进程级共享的:不清的话,上一个用例缓存过的封面会让这一个
        # 用例直接命中缓存、不发请求(表现为"封面请求列表是空的")
        QPixmapCache.clear()

        self.videos = [_video("A"), _video("B", pages=2), _video("C")]
        self.client = self._make_client()
        self.resolver = _FakeResolver()
        self.player = _FakePlayer()
        self.playback = PlaybackController(self.resolver, self.player)  # type: ignore[arg-type]
        self.store = ConfigStore(self.tmp / "config.json")
        # 凭据存储**必须**指向沙箱:默认值是用户真实的配置目录,漏注入的用例会把
        # 登录凭据写到真实用户身上(AGENTS.md 第 7 节:可注入路径)
        self.session_store = SessionStore(self.tmp / "session.json")

        # 模态弹窗在离屏测试里会把用例挂住,换成记录器。
        # ``question`` 也一并拦掉:它比 ``warning`` 危险得多 —— 忘了拦就是**整个模块死等**
        # (写过一次,见 TestLogWiring 里那条回归用例的注释)。默认答"否",用例要"是"
        # 就自己再套一层 patch(后套的先生效、先还原)。
        self.warnings: list[str] = []
        patcher = mock.patch.object(
            QMessageBox, "warning", lambda *args, **kwargs: self.warnings.append(args[2])
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.questions: list[str] = []

        def _decline(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
            """记下被问的问题并按"否"回答(离屏测试里绝不允许真的开模态框)。"""
            self.questions.append(str(args[2]) if len(args) > 2 else "")
            return QMessageBox.StandardButton.No

        question_patcher = mock.patch.object(QMessageBox, "question", _decline)
        question_patcher.start()
        self.addCleanup(question_patcher.stop)

        self.window = MainWindow(
            client=self.client,  # type: ignore[arg-type]
            # 音频缓存要指向沙箱目录:切到"本地缓存"页会读它的索引(默认值会去翻真实
            # 用户的 %LOCALAPPDATA%,那些记录不属于任何用例)
            cache=AudioCache(self.tmp / "cache", db=LibraryDb(self.tmp / "library.db")),
            # 封面必须落进沙箱目录:默认值会写到真实用户的 %LOCALAPPDATA%,
            # 而且第二个用例会因为"上一轮的图还在磁盘上"而不再发假请求,断言随之失灵
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=self.store,
            playback=self.playback,
            session_store=self.session_store,
            # 日志目录同样指向沙箱:默认值是用户真实的 ``~/Library/Logs``,
            # 导出类用例一旦忘了用替身就会往那里写文件
            log_dir=self.tmp / "logs",
        )

    def tearDown(self) -> None:
        """关掉窗口(触发退出清理);临时目录由 cleanup 负责删。"""
        self.window.close()

    def _search(self, keyword: str = "周杰伦") -> None:
        """走一遍"输入关键字 + 点搜索"。"""
        self.window.title_bar.search_input.setText(keyword)
        self.window.on_search()

    def _make_client(self) -> _FakeClient:
        """造本次用例用的客户端替身;翻页类用例会覆盖它。"""
        return _FakeClient(self.videos)

    def _search_and_play(self, row: int) -> None:
        """走一遍"搜索 + 双击第 row 行"。"""
        self._search()
        self.window.result_list.row_activated.emit(row)

    def _rebuild_window(self, client: _FakeClient) -> None:
        """换一个客户端替身重建主窗口(旧窗口先关掉)。

        客户端是在 ``setUp`` 里注入窗口的,而翻页用例需要自己决定"每页返回什么、哪一页
        失败",所以只能重建 —— 给基类开一堆可覆盖的钩子比重建更难读。重建用另一个封面
        缓存目录,免得命中上一个窗口留下的磁盘缓存。

        ``session_store`` **必须一起传**:漏了它,窗口就会去读**用户真实配置目录**里的
        ``session.json``(漏注入的后果见 ``main_window.py`` 的模块 docstring),
        实测表现为"登录成功,但凭据没能保存到磁盘"的弹窗混进 ``self.warnings``,
        把与登录毫不相干的用例一起搞红。

        Args:
            client: 新的客户端替身。
        """
        self.window.close()
        self.client = client
        self.window = MainWindow(
            client=client,  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            cover_cache=CoverCache(self.tmp / "covers_next"),
            config_store=self.store,
            library=LibraryDb(self.tmp / "library.db"),
            playback=self.playback,
            session_store=self.session_store,
        )


# ====================================================================== 用例


class TestSearchWiring(_WindowCase):
    """搜索与结果列表。"""

    def test_search_fills_the_result_list(self) -> None:
        """搜索成功后结果表要填满,分P列在详情补全前显示占位符。"""
        self._search()
        self.assertEqual(self.client.keywords, ["周杰伦"])
        self.assertEqual(self.window.result_list.rowCount(), 3)
        self.assertEqual(self.window.result_list.title_at(0), "视频A")
        self.assertEqual(self.window.result_list.item(0, 2).text(), "某UP")
        self.assertEqual(self.window.result_list.item(0, 4).text(), "?")
        self.assertEqual(self.window.result_list.item(0, 0).text(), "1")

    def test_header_shows_the_keyword_and_the_total(self) -> None:
        """标题行要显示搜了什么、命中多少条,否则用户不知道看的是哪次搜索的结果。"""
        self._search()
        self.assertIn("周杰伦", self.window.query_label.text())
        self.assertIn("3", self.window.total_label.text())

    def test_empty_keyword_does_not_search(self) -> None:
        """空关键字不发请求(否则会白挨一次风控)。"""
        self.window.title_bar.search_input.setText("   ")
        self.window.on_search()
        self.assertEqual(self.client.keywords, [])

    def test_search_button_is_reused_from_the_title_bar(self) -> None:
        """搜索按钮在自绘标题栏里;点它要真的发请求(接线别落在空处)。"""
        self.window.title_bar.search_input.setText("晴天")
        self.window.title_bar.search_button.click()
        self.assertEqual(self.client.keywords, ["晴天"])

    def test_double_click_replaces_the_queue_from_that_row(self) -> None:
        """双击搜索结果:整个结果成为队列,并从那一行开始播。"""
        self._search_and_play(1)
        self.assertEqual(len(self.playback.queue), 3)
        self.assertEqual(self.playback.current.video.bvid, "B")
        self.assertEqual(self.resolver.calls[-1][0].bvid, "B")

    def test_track_list_maps_double_click_to_a_row_number(self) -> None:
        """Qt 的 doubleClicked 信号要能折算成行号,否则双击不会有反应。"""
        self._search()
        rows: list[int] = []
        self.window.result_list.row_activated.connect(rows.append)
        self.window.result_list.doubleClicked.emit(
            self.window.result_list.model().index(2, 0)
        )
        self.assertEqual(rows, [2])

    def test_page_count_is_filled_after_detail_is_known(self) -> None:
        """详情补全后,结果表里那一行的分P数要变成真实值。"""
        self._search_and_play(1)
        self.resolver.succeed(_resolved(self.videos[1]))
        self.assertEqual(self.window.result_list.item(1, 4).text(), "2")

    def test_cover_is_requested_for_the_listed_videos(self) -> None:
        """列表一填上就要去取封面(带封面的行没有图等于没做)。"""
        self._search()
        self.assertEqual(self.window.result_list.cover_url_at(0), self.videos[0].cover_https)
        self.assertIn(self.videos[0].cover_https, self.client.cover_calls)

    def test_row_cover_is_shown_when_it_arrives(self) -> None:
        """封面到了要贴到对应的行上,而且只贴那几行。"""
        self._search()
        url = self.videos[0].cover_https
        self.assertFalse(self.window.result_list.has_cover_at(0))
        self.client.succeed_cover(url, _png_bytes())
        self.assertTrue(self.window.result_list.has_cover_at(0))
        self.assertFalse(self.window.result_list.has_cover_at(1))

    def test_row_add_button_enqueues(self) -> None:
        """行内"+"要真的把这一首加进队列(按钮不能只是画着好看)。"""
        self._search()
        self.window.result_list.add_requested.emit(2)
        self.assertEqual(len(self.playback.queue), 1)
        self.assertEqual(self.playback.queue.items[0].video.bvid, "C")


class TestSearchHistoryWiring(_WindowCase):
    """搜索历史的接线:记一条、下拉框列什么、点一条会发生什么。

    数据侧用的是**真的** :class:`~bilibili_music.core.search_history.SearchHistory`
    (落在沙箱库里),所以这里失败说明是接线问题,而不是某条纯逻辑改了。
    """

    def test_search_records_the_keyword(self) -> None:
        """提交一次搜索要在历史里留下这个词(否则下拉框永远是空的)。"""
        self._search("周杰伦")
        self.assertEqual(self.window.search_history.entries(), ("周杰伦",))

    def test_searching_the_same_keyword_again_keeps_one_entry(self) -> None:
        """重搜同一个词:只把它提到最前,而不是多出一条重复项。"""
        self._search("周杰伦")
        self._search("晴天")
        self._search("周杰伦")
        self.assertEqual(self.window.search_history.entries(), ("周杰伦", "晴天"))

    def test_blank_keyword_is_not_recorded(self) -> None:
        """空白关键字早退,不该在历史里留下一行空白(它点也没法搜)。"""
        self.window.title_bar.search_input.setText("   ")
        self.window.on_search()
        self.assertEqual(self.window.search_history.entries(), ())

    def test_typing_filters_the_history_list_with_the_core_rule(self) -> None:
        """输入时按**包含匹配**过滤(过滤规则来自 core,而不是控件里另写一套)。"""
        for keyword in ("周杰伦 MV", "晴天", "夜曲"):
            self.window.search_history.record(keyword)
        self.window.title_bar.search_input.setText("杰伦")
        self.window.title_bar.search_input.textEdited.emit("杰伦")
        self.assertTrue(self.window.search_suggest.is_open)
        self.assertEqual(self.window.search_suggest.terms(), ("周杰伦 MV",))

    def test_clicking_a_history_term_fills_the_box_and_searches(self) -> None:
        """点一条历史词:填回输入框 + 立刻发请求(需求里的第 4 条)。"""
        self.window.search_history.record("晴天")
        self.window.search_suggest.refresh()
        self.assertEqual(self.window.search_suggest.terms(), ("晴天",))
        self.window.search_suggest.rows[0].click()
        self.assertEqual(self.window.title_bar.search_input.text(), "晴天")
        self.assertEqual(self.client.keywords, ["晴天"])
        self.assertFalse(self.window.search_suggest.is_open)

    def test_searching_closes_the_history_list(self) -> None:
        """下拉框开着的时候提交搜索:搜完不该还留着一个浮层。"""
        self.window.search_history.record("周杰伦")
        self.window.title_bar.search_input.setText("周杰伦")
        self.window.search_suggest.refresh()
        self.assertTrue(self.window.search_suggest.is_open)
        self.window.on_search()
        self.assertFalse(self.window.search_suggest.is_open)

    def test_clicking_outside_closes_the_list_and_drops_focus(self) -> None:
        """点搜索结果区(既不是搜索框也不是下拉框):收起 + 输入框不再有焦点。

        必须先把窗口 ``show()`` 出来:焦点只对可见窗口有意义(与 ``test_ui_widgets``
        里测省略、把手时必须先 show 是同一个原因)。
        """
        self.addCleanup(self.window.hide)
        self.window.show()
        QApplication.processEvents()
        self.window.search_history.record("周杰伦")
        self.window.title_bar.search_input.setFocus()
        self.window.search_suggest.refresh()
        self.assertTrue(self.window.title_bar.search_input.hasFocus())
        self.assertTrue(self.window.search_suggest.is_open)

        QTest.mouseClick(
            self.window.result_list, Qt.MouseButton.LeftButton, pos=QPoint(20, 20)
        )
        self.assertFalse(self.window.search_suggest.is_open)
        self.assertFalse(self.window.title_bar.search_input.hasFocus())


class TestSearchPagingWiring(_WindowCase):
    """搜索结果的翻页加载:滚到底追加、去重、停止条件与竞态。"""

    def _make_client(self) -> _FakeClient:
        """三页结果,其中第 2 页**故意重复**第 1 页的一条 —— 实测接口真的会这样。

        第 2 页重复 ``B``,所以三页全部加载完应该是 A B C D E F G H 共 8 行。
        """
        self.page1 = [_video("A"), _video("B"), _video("C")]
        self.page2 = [_video("B"), _video("D"), _video("E")]
        self.page3 = [_video("F"), _video("G"), _video("H")]
        return _FakeClient(
            self.page1,
            pages={1: self.page1, 2: self.page2, 3: self.page3},
            total_pages=3,
            total=8,
        )

    def _titles(self) -> list[str]:
        """结果表当前所有行的标题。"""
        table = self.window.result_list
        return [table.title_at(row) for row in range(table.rowCount())]

    def _page_of(self, index: int) -> int:
        """第 index 次搜索请求请求的是第几页(0 起)。"""
        return self.client.calls[index][1]

    def test_search_only_asks_for_the_first_page(self) -> None:
        """首次搜索只取第 1 页,不预取(每页都是一次请求,不能开局就打一串)。"""
        self._search()
        self.assertEqual(self.client.calls, [("周杰伦", 1)])

    def test_scrolling_to_the_bottom_appends_the_next_page(self) -> None:
        """滚到底要自动取下一页并**追加**,而不是把已有结果换掉。"""
        self._search()
        self.assertEqual(self._titles(), ["视频A", "视频B", "视频C"])

        self.window.result_list.load_more_requested.emit()

        self.assertEqual(self._page_of(1), 2)
        self.assertEqual(
            self._titles(), ["视频A", "视频B", "视频C", "视频D", "视频E"]
        )
        # 追加的行序号接着排,不是从 1 重来
        self.assertEqual(self.window.result_list.item(4, 0).text(), "5")

    def test_duplicate_bvids_are_listed_once(self) -> None:
        """相邻两页会重叠(实测 p1 与 p2 有 3 条重复),重复的不能再显示一行。"""
        self._search()
        self.window.result_list.load_more_requested.emit()  # 第 2 页带回了 B
        self.assertEqual(self._titles().count("视频B"), 1)
        self.assertEqual(self.window.result_list.rowCount(), 5)

    def test_keeps_loading_until_the_last_page_then_stops(self) -> None:
        """翻到接口说的末页就停:不能一直请求下去。"""
        self._search()
        self.window.result_list.load_more_requested.emit()
        self.window.result_list.load_more_requested.emit()
        self.assertEqual(self._titles(), [f"视频{x}" for x in "ABCDEFGH"])
        self.assertEqual(len(self.client.calls), 3)

        self.window.result_list.load_more_requested.emit()  # 到底了再滚也不发请求
        self.assertEqual(len(self.client.calls), 3)
        self.assertIn("全部", self.window.status_label.text())

    def test_a_page_without_any_new_item_stops_the_paging(self) -> None:
        """本页没带来新条目就停。

        实测**越界页码会被接口静默当成第 1 页返回**,所以"还有下一页"不能只信
        ``numPages`` —— 这里让第 2 页原样回第 1 页的数据,必须立刻停,否则会无限重复。
        """
        client = _FakeClient(
            self.videos,
            pages={1: list(self.videos), 2: list(self.videos)},
            total_pages=99,
            total=200,
        )
        self._rebuild_window(client)
        self._search()
        self.window.result_list.load_more_requested.emit()
        self.assertEqual(len(client.calls), 2)

        self.window.result_list.load_more_requested.emit()
        self.assertEqual(len(client.calls), 2, "重复的第 1 页不该被一遍遍拉")
        self.assertEqual(self.window.result_list.rowCount(), 3)

    def test_stops_at_the_configured_page_limit(self) -> None:
        """自设上限到了就停:接口给几十页,不能真的翻几十次(风控)。"""
        pages = {
            page: [_video(f"P{page}-{i}") for i in range(3)]
            for page in range(1, 10)
        }
        self._rebuild_window(
            _FakeClient(pages[1], pages=pages, total_pages=99, total=999)
        )
        self._search()
        for _ in range(10):
            self.window.result_list.load_more_requested.emit()

        self.assertEqual(len(self.client.calls), _SEARCH_MAX_PAGES)
        self.assertEqual(
            self.window.result_list.rowCount(), _SEARCH_MAX_PAGES * 3
        )
        self.assertIn(str(_SEARCH_MAX_PAGES), self.window.status_label.text())

    def test_a_new_search_resets_the_paging_state(self) -> None:
        """换关键字重新搜索要从第 1 页重来,不能接着上一次的页码往下翻。"""
        self._search()
        self.window.result_list.load_more_requested.emit()  # 到第 2 页
        self.window.title_bar.search_input.setText("晴天")
        self.window.on_search()
        self.assertEqual(self._page_of(2), 1)
        self.assertEqual(self._titles(), ["视频A", "视频B", "视频C"])

    def test_late_response_of_a_replaced_search_is_dropped(self) -> None:
        """被换掉的搜索即使响应迟到,也不能污染新结果(序号对不上就丢弃)。"""
        client = _FakeClient(self.videos, defer=True)
        self._rebuild_window(client)

        self.window.title_bar.search_input.setText("第一个")
        self.window.on_search()
        stale = client.pending[0]  # 抓住旧请求的回调,模拟"已经在路上"

        self.window.title_bar.search_input.setText("第二个")
        self.window.on_search()
        self.assertEqual(client.cancelled, [1], "旧请求要被取消")

        stale.succeed(SearchResult(videos=[_video("ZZZ")], page=1, total_pages=1))
        self.assertEqual(self.window.result_list.rowCount(), 0, "旧响应必须被丢弃")

        client.pending[0].succeed(client.result_for(1))
        self.assertEqual(self._titles(), ["视频A", "视频B", "视频C"])

    def test_failed_next_page_offers_a_retry_button(self) -> None:
        """翻页失败不能静默:要给一个看得见、点得动的重试出口(滚动触发是隐式的)。"""
        pages = {1: list(self.videos), 2: [_video("D")]}
        self._rebuild_window(
            _FakeClient(
                self.videos, pages=pages, total_pages=3, total=4, fail_pages={2}
            )
        )
        self._search()
        self.assertTrue(self.window.load_more_button.isHidden(), "平时不该占地方")

        self.window.result_list.load_more_requested.emit()  # 第 2 页失败
        self.assertFalse(self.window.load_more_button.isHidden())
        self.assertIn("失败", self.window.status_label.text())
        self.assertEqual(self.warnings, [], "翻页失败不该弹模态框打断用户")

    def test_retry_button_loads_the_failed_page(self) -> None:
        """点重试要真的把那页再取一次,成功后按钮自己收起来。"""
        pages = {1: list(self.videos), 2: [_video("D")]}
        client = _FakeClient(
            self.videos, pages=pages, total_pages=3, total=4, fail_pages={2}
        )
        self._rebuild_window(client)
        self._search()
        self.window.result_list.load_more_requested.emit()
        self.assertFalse(self.window.load_more_button.isHidden())

        client.fail_pages.clear()  # 风控过去了
        self.window.load_more_button.click()

        self.assertEqual(self._titles(), ["视频A", "视频B", "视频C", "视频D"])
        self.assertTrue(self.window.load_more_button.isHidden())
        self.assertEqual(self._page_of(2), 2)

    def test_failed_first_page_still_warns_with_a_dialog(self) -> None:
        """第 1 页失败是用户主动点的搜索,必须弹窗让他看见(与翻页失败区别对待)。"""
        self._rebuild_window(
            _FakeClient(self.videos, total_pages=1, fail_pages={1})
        )
        self._search()
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("搜索失败", self.warnings[0])
        self.assertTrue(self.window.load_more_button.isHidden())


class TestPlayerBarWiring(_WindowCase):
    """播放条的按钮与滑块。"""

    def test_play_button_toggles(self) -> None:
        """播放/暂停按钮要走到编排层的 toggle。"""
        self._search_and_play(0)
        self.window.player_bar.play_button.click()
        self.assertEqual(self.player.toggle_count, 1)

    def test_next_button_advances(self) -> None:
        """下一首要真的换歌。"""
        self._search_and_play(0)
        self.window.player_bar.next_button.click()
        self.assertEqual(self.playback.current.video.bvid, "B")

    def test_next_at_the_end_reports_instead_of_doing_nothing(self) -> None:
        """最后一首再点下一首要给出提示,不能让按钮看起来像坏了。"""
        self._search_and_play(2)
        self.window.player_bar.next_button.click()
        self.assertEqual(self.window.status_label.text(), "已经是最后一首")

    def test_previous_button_goes_back(self) -> None:
        """上一首要回到前一首。"""
        self._search_and_play(2)
        self.window.player_bar.previous_button.click()
        self.assertEqual(self.playback.current.video.bvid, "B")

    def test_mode_button_cycles_and_persists(self) -> None:
        """模式按钮按顺序切换,并把结果落盘。"""
        self.window.player_bar.mode_button.click()
        self.assertIs(self.playback.queue.mode, PlayMode.REPEAT_ALL)
        self.assertIs(self.store.load().play_mode, PlayMode.REPEAT_ALL)

    def test_quality_combo_drives_quality(self) -> None:
        """选中音质下拉框要带着档位重新解析当前曲目。"""
        self._search_and_play(0)
        index = self.window.player_bar.quality_combo.findData(30216)
        self.window.player_bar.quality_combo.setCurrentIndex(index)
        self.assertEqual(self.playback.quality_id, 30216)
        self.assertEqual(self.resolver.calls[-1][2], 30216)

    def test_volume_change_applies_and_persists(self) -> None:
        """音量滑块要同时作用到播放器与配置。"""
        self.window.player_bar.volume_slider.setValue(30)
        self.assertEqual(self.player.volumes[-1], 0.3)
        self.assertEqual(self.store.load().volume, 30)

    def test_seek_is_forwarded(self) -> None:
        """拖完进度条要把目标位置交给编排层。"""
        self._search_and_play(0)
        self.window.player_bar.position_slider.setRange(0, 200_000)
        self.window.player_bar.position_slider.setValue(12_345)
        self.window.player_bar.position_slider.sliderReleased.emit()
        self.assertEqual(self.player.seeks[-1], 12_345)


class TestPageSelectorWiring(_WindowCase):
    """播放条上的分P选择器:状态由编排层推下来,选择交给编排层。"""

    @property
    def _selector(self):  # noqa: ANN201 - PageSelector
        """被接线的分P选择器。"""
        return self.window.player_bar.page_selector

    def test_disabled_before_anything_plays(self) -> None:
        """还没有当前视频:选择器禁用并显示占位,而不是留着空按钮可点。"""
        self.assertFalse(self._selector.isEnabled())
        self.assertEqual(self._selector.full_label_text(), EMPTY_LABEL)

    def test_shows_the_current_video_page_as_soon_as_it_plays(self) -> None:
        """开始播某个视频后,选择器要显示这个视频当前那P的标题。"""
        self._search_and_play(1)  # 视频 B:两个分P
        self.assertTrue(self._selector.isEnabled())
        self.assertEqual(self._selector.full_label_text(), "分P:P1 第1首")

    def test_menu_lists_the_current_videos_pages_only(self) -> None:
        """菜单里只有当前视频的分P,并且标出正在播的那一P。"""
        self._search_and_play(1)
        self._selector.click()
        self.addCleanup(self._selector.close_popup)
        rows = self._selector.popup_rows()
        self.assertEqual([row.page_index for row in rows], [1, 2])
        self.assertEqual([row.is_current for row in rows], [True, False])
        self.assertEqual(rows[1].title_label.full_text(), "第2首")

    def test_choosing_a_menu_row_switches_the_playback_target(self) -> None:
        """在菜单里点第 2P:编排层按该分P重新解析,选择器跟着挪过去。"""
        self._search_and_play(1)
        self._selector.click()
        self._selector.popup_rows()[1].click()
        self.assertEqual(self.resolver.calls[-1][1], 2)
        self.assertFalse(self._selector.is_popup_open)
        self.assertEqual(self._selector.full_label_text(), "分P:P2 第2首")

    def test_audio_ready_refreshes_the_selector_with_the_page_title(self) -> None:
        """详情补全后分P标题才知道:音源就绪时要把选择器再刷一遍。"""
        # 先摘掉分P列表,模拟"搜索阶段只有视频标题、详情还没补全"的状态
        pages = self.videos[1].pages
        self.videos[1].pages = []
        self._search_and_play(1)
        self.assertEqual(self._selector.full_label_text(), "分P:P1")
        self.assertFalse(self._selector.isEnabled())  # 详情未知,先不给点
        # 解析器补全详情时会把分P列表写回**同一个** Video 对象(audio/resolver.py)
        self.videos[1].pages = pages
        self.resolver.succeed(_resolved(self.videos[1]))
        self.assertTrue(self._selector.isEnabled())
        self.assertEqual(self._selector.full_label_text(), "分P:P1 第1首")

    def test_single_page_video_shows_the_page_but_is_disabled(self) -> None:
        """单P视频也照样显示当前分P,只是点不开(控件位置因此不会跳)。"""
        self._search_and_play(0)  # 视频 A:一个分P
        self.resolver.succeed(_resolved(self.videos[0]))
        self.assertFalse(self._selector.isEnabled())
        self.assertEqual(self._selector.full_label_text(), "分P:P1 第1首")

    def test_switching_to_another_queue_video_resets_the_selector(self) -> None:
        """队列换到另一行时,选择器跟着换成那个视频与它自己的分P。"""
        self._search_and_play(1)
        self._selector.page_selected.emit(2)
        self.resolver.succeed(_resolved(self.videos[1], page_index=2))
        self.window.player_bar.next_button.click()  # → 视频 C(单P)
        self.assertFalse(self._selector.isEnabled())
        self.assertEqual(self._selector.full_label_text(), "分P:P1 第1首")

    def test_clearing_the_queue_disables_the_selector(self) -> None:
        """清空队列后没有当前视频:选择器要收回占位并禁用。"""
        self._search_and_play(1)
        self.playback.clear()
        self.assertFalse(self._selector.isEnabled())
        self.assertEqual(self._selector.full_label_text(), EMPTY_LABEL)

    def test_menu_avoids_the_queue_drawer(self) -> None:
        """菜单要避开的正是右侧队列面板 —— 这层关系由主窗口接线。"""
        self.assertIs(self._selector.avoid_widget, self.window.queue_drawer)


class TestQueueDrawerWiring(_WindowCase):
    """右侧队列面板。"""

    def test_drawer_lists_the_queue_and_marks_current(self) -> None:
        """队列内容与数量要跟着编排层走。"""
        self._search_and_play(1)
        self.assertEqual(self.window.queue_drawer.list.rowCount(), 3)
        self.assertEqual(self.window.queue_drawer.count_label.text(), "3 首")
        self.assertEqual(self.window.queue_drawer.list.highlighted_row(), 1)

    def test_drawer_rows_show_the_expected_fields(self) -> None:
        """队列行要有曲名与时长(设计稿里每一行就是这两样)。"""
        self._search_and_play(0)
        row0 = self.window.queue_drawer.list
        self.assertEqual(row0.title_at(0), self.videos[0].title)
        self.assertEqual(row0.item(0, 2).text(), self.videos[0].pages[0].duration_text)

    def test_activating_a_row_jumps_to_it(self) -> None:
        """双击队列某一行要跳到那一首。"""
        self._search_and_play(0)
        self.window.queue_drawer.row_activated.emit(2)
        self.assertEqual(self.playback.current.video.bvid, "C")

    def test_remove_requested_removes_from_the_queue(self) -> None:
        """抽屉发出的"移除"要作用到队列上。"""
        self._search_and_play(0)
        self.window.queue_drawer.remove_requested.emit(2)
        self.assertEqual(len(self.playback.queue), 2)

    def test_clear_requested_empties_the_queue(self) -> None:
        """清空按钮要清空队列。"""
        self._search_and_play(0)
        self.window.queue_drawer.clear_requested.emit()
        self.assertEqual(len(self.playback.queue), 0)


class TestQueueVisibilityWiring(_WindowCase):
    """队列面板的显示/隐藏:开关只有播放条上那一个。"""

    def _visible(self) -> bool:
        """队列面板当前是否可见。"""
        return not self.window.queue_drawer.isHidden()

    def test_queue_is_visible_on_start(self) -> None:
        """设计稿里队列面板默认是展开的,播放条上的开关要是选中态。"""
        self.assertTrue(self._visible())
        self.assertTrue(self.window.player_bar.queue_button.isChecked())

    def test_player_bar_button_hides_and_shows(self) -> None:
        """播放条上的队列开关要真的收起/展开面板。"""
        self.window.player_bar.queue_button.click()
        self.assertFalse(self._visible())
        self.assertFalse(self.window.player_bar.queue_button.isChecked())
        self.window.player_bar.queue_button.click()
        self.assertTrue(self._visible())
        self.assertTrue(self.window.player_bar.queue_button.isChecked())


class TestNavigationWiring(_WindowCase):
    """侧栏导航:哪一页是真的、哪一页是占位。"""

    def test_results_entry_shows_the_results_page(self) -> None:
        """点"搜索结果"要回到结果页。"""
        self.window.sidebar.nav_buttons["discover"].click()
        self.window.sidebar.nav_buttons["results"].click()
        self.assertIs(self.window.pages.currentWidget(), self.window.results_page)

    def test_unimplemented_entries_show_a_placeholder(self) -> None:
        """"发现"还没有功能:必须给占位页,而不是点了没反应。"""
        self.window.sidebar.nav_buttons["discover"].click()
        self.assertIs(self.window.pages.currentWidget(), self.window.placeholder_page)
        self.assertEqual(self.window.placeholder_page.title_label.full_text(), "发现")
        self.assertIn("还没实现", self.window.placeholder_page.hint_label.full_text())

    def test_cache_entry_shows_the_local_cache_page(self) -> None:
        """"本地缓存"已经是真的页面了,不再落进占位页(路线图 M2.2)。

        页面内容与离线点播在 ``tests/test_cache_page.py`` 里细验,这里只钉住"侧栏这一格
        通向哪一页"这条接线。
        """
        self.window.sidebar.nav_buttons["cache"].click()
        self.assertIs(self.window.pages.currentWidget(), self.window.cache_page)
        self.assertTrue(self.window.sidebar.nav_buttons["cache"].isChecked())

    def test_playlist_entry_opens_the_fav_page(self) -> None:
        """点收藏夹:打开收藏夹页并加载第一页(路线图 M5 S1,不再落进占位页)。"""
        self.client.folders = [_folder(169038169, "默认收藏夹", 128)]
        self.window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))
        self.window.sidebar.playlist_buttons["169038169"].click()
        self.assertIs(self.window.pages.currentWidget(), self.window.fav_page)
        self.assertEqual(self.client.fav_calls, [(169038169, 1)])
        self.assertEqual(self.window.fav_page.title_label.text(), "默认收藏夹")

    def test_playlist_handler_is_a_qt_text_slot(self) -> None:
        """收藏夹处理函数要注册成 Qt 文本槽,避免大 id 经过 ``int`` 时溢出。"""
        slot_index = self.window.metaObject().indexOfSlot(
            "_on_playlist_selected(QString)"
        )

        self.assertGreaterEqual(slot_index, 0)

    def test_large_playlist_id_opens_the_fav_page(self) -> None:
        """大于 Qt 有符号 32 位上限的收藏夹仍能打开并按原 id 请求。"""
        media_id = 4_140_917_469
        self.client.folders = [_folder(media_id, "大收藏夹", 128)]
        self.window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))

        self.window.sidebar.playlist_buttons[str(media_id)].click()

        self.assertIs(self.window.pages.currentWidget(), self.window.fav_page)
        self.assertEqual(self.client.fav_calls, [(media_id, 1)])

    def test_create_playlist_button_shows_a_placeholder(self) -> None:
        """"+"按钮也要有反馈,不能是个哑按钮。"""
        self.window.sidebar.create_button.click()
        self.assertIs(self.window.pages.currentWidget(), self.window.placeholder_page)
        self.assertEqual(
            self.window.placeholder_page.title_label.full_text(), "新建歌单"
        )


class TestAccountWiring(_WindowCase):
    """账号与收藏夹接线(路线图 M5 S1):登录 / 启动恢复 / 登出 / 收藏夹页。

    这里钉的是**顺序与副作用**:凭据什么时候写盘、什么时候从 jar 里撤掉、清完 jar 有没有
    补一次匿名预热、切收藏夹时旧响应会不会污染新列表 —— 这些错了界面照样"看起来能用"。
    """

    def _new_window(self) -> MainWindow:
        """按当前替身**再建一个**主窗口(用来验证"启动时恢复登录态")。

        目录全部换到 ``*-2`` 后缀,免得与基类那个窗口抢同一个配置文件 / 库文件。
        """
        window = MainWindow(
            client=self.client,  # type: ignore[arg-type]
            cache=AudioCache(self.tmp / "cache2", db=LibraryDb(self.tmp / "library2.db")),
            cover_cache=CoverCache(self.tmp / "covers2"),
            config_store=ConfigStore(self.tmp / "config2.json"),
            playback=self.playback,
            session_store=self.session_store,
            log_dir=self.tmp / "logs",
        )
        self.addCleanup(window.close)
        return window

    def test_login_saves_credentials_only_after_nav_confirms(self) -> None:
        """登录成功:注入网络层、填侧栏、把凭据(含 nav 给的昵称)落盘。"""
        self.client.folders = [_folder(169038169, "默认收藏夹", 128)]
        self.window._on_login_requested("SESSDATA=a%2Cb; bili_jct=c; DedeUserID=42")

        self.assertEqual(
            self.client.backend.injected,
            [{"SESSDATA": "a%2Cb", "bili_jct": "c", "DedeUserID": "42"}],
        )
        self.assertIn("169038169", self.window.sidebar.playlist_buttons)
        self.assertEqual(self.window.sidebar.account_button.text(), "测试账号")
        saved = self.session_store.load()
        assert saved is not None
        self.assertEqual((saved.uname, saved.mid), ("测试账号", 42))

    def test_rejected_credential_is_not_saved_and_leaves_the_jar(self) -> None:
        """凭据被服务端拒:不落盘、从 jar 里撤掉、并补一次匿名预热。"""
        self.client.nav = AccountInfo(is_login=False)
        self.window._on_login_requested("SESSDATA=bad")

        self.assertIsNone(self.session_store.load())
        self.assertEqual(self.client.backend.clear_count, 1)
        self.assertEqual(self.client.backend.warmups[-1], True)
        self.assertEqual(self.window.sidebar.account_button.text(), "登录")

    def test_login_network_failure_does_not_write_a_file(self) -> None:
        """网络失败时如实报错、**不写文件** —— 写下去就等于把一份没验过的凭据当登录态。"""
        self.client.nav_error = NetworkError("HTTP 0 连接失败")
        self.window._on_login_requested("SESSDATA=a")

        self.assertIsNone(self.session_store.load())
        self.assertEqual(self.client.backend.clear_count, 0)

    def test_startup_restores_the_session(self) -> None:
        """启动时:凭据注入网络层、nav 被问一次、侧栏填上收藏夹。"""
        self.session_store.save(
            session_from_cookies({"SESSDATA": "x"}, uname="甲", mid=42)
        )
        self.client.folders = [_folder(7, "音乐", 3)]
        self._new_window()

        self.assertEqual(self.client.backend.injected, [{"SESSDATA": "x"}])
        self.assertEqual(self.client.nav_calls, 1)
        self.assertEqual(self.client.folder_calls, [42])

    def test_startup_without_credentials_stays_anonymous(self) -> None:
        """没有凭据就不发任何账号相关请求(离线启动不该被登录流程拖住)。"""
        self._new_window()
        self.assertEqual(self.client.backend.injected, [])
        self.assertEqual(self.client.nav_calls, 0)

    def test_startup_drops_an_expired_session(self) -> None:
        """服务端说凭据失效:文件删掉、jar 清空,界面退回未登录。"""
        self.session_store.save(session_from_cookies({"SESSDATA": "x"}, uname="甲", mid=7))
        self.client.nav = AccountInfo(is_login=False)
        self._new_window()

        self.assertIsNone(self.session_store.load())
        self.assertGreaterEqual(self.client.backend.clear_count, 1)

    def test_startup_network_failure_keeps_credentials(self) -> None:
        """校验时网络失败**不删凭据** —— "问不到"不等于"失效",离线启动照样该能听歌。"""
        self.session_store.save(session_from_cookies({"SESSDATA": "x"}, uname="甲", mid=7))
        self.client.nav_error = NetworkError("HTTP 0")
        window = self._new_window()

        self.assertIsNotNone(self.session_store.load())
        self.assertEqual(window.sidebar.account_button.text(), "甲")

    def test_logout_clears_file_and_jar(self) -> None:
        """登出:删文件 + 清 jar + 强制补一次匿名预热 + 侧栏退回未登录。"""
        self.client.folders = [_folder(1, "默认收藏夹", 2)]
        self.window._on_login_requested("SESSDATA=a")
        self.window._on_logout_requested()

        self.assertIsNone(self.session_store.load())
        self.assertGreaterEqual(self.client.backend.clear_count, 1)
        self.assertEqual(self.client.backend.warmups[-1], True)
        self.assertEqual(self.window.sidebar.playlist_buttons, {})
        self.assertEqual(self.window.sidebar.account_button.text(), "登录")

    def test_folder_click_loads_the_first_page_and_paging_appends(self) -> None:
        """点收藏夹加载第 1 页;点"加载更多"取第 2 页并**追加**而不是替换。"""
        self.client.folders = [_folder(9, "音乐", 3)]
        self.client.fav_pages[(9, 1)] = FavPageData(
            items=[_fav_item("BV1a"), _fav_item("BV1b")],
            media_id=9,
            media_count=3,
            has_more=True,
        )
        self.client.fav_pages[(9, 2)] = FavPageData(
            items=[_fav_item("BV1c")], media_id=9, media_count=3, has_more=False
        )
        self.window._on_login_requested("SESSDATA=a")
        self.window.sidebar.playlist_buttons["9"].click()

        self.assertEqual(self.client.fav_calls, [(9, 1)])
        self.assertEqual(self.window.fav_page.list.rowCount(), 2)

        self.window.fav_page.load_more_button.click()
        self.assertEqual(self.client.fav_calls, [(9, 1), (9, 2)])
        self.assertEqual(self.window.fav_page.list.rowCount(), 3)

    def test_stale_page_response_does_not_pollute_the_new_folder(self) -> None:
        """切收藏夹时,上一个夹子迟到的响应必须被丢掉。

        实测一个收藏夹有 128 条、一页 20 条,响应回来得慢;"先点 A 再点 B、A 的响应后到"
        是很常见的操作序列。没有 token 防护的话,B 的列表里会混进 A 的内容。
        """
        self.client.folders = [_folder(1, "夹子A", 2), _folder(2, "夹子B", 1)]
        self.client.fav_defer = True
        self.window._on_login_requested("SESSDATA=a")
        self.window.sidebar.playlist_buttons["1"].click()
        self.window.sidebar.playlist_buttons["2"].click()

        # A 的响应现在才回来:必须被忽略
        self.client.succeed_fav(
            1, 1, FavPageData(items=[_fav_item("BV1stale")], media_id=1, has_more=False)
        )
        self.assertEqual(self.window.fav_page.list.rowCount(), 0)

        self.client.succeed_fav(
            2, 1, FavPageData(items=[_fav_item("BV1fresh")], media_id=2, has_more=False)
        )
        self.assertEqual(self.window.fav_page.list.rowCount(), 1)
        self.assertEqual(self.window.fav_page.list.title_at(0), "收藏BV1fresh")

    def test_dead_item_is_not_requested_and_says_why(self) -> None:
        """失效条目双击:不发详情请求,只在状态栏说明原因。"""
        self.client.folders = [_folder(1, "夹子", 1)]
        self.client.fav_pages[(1, 1)] = FavPageData(
            items=[_fav_item("BV1dead", "已失效的歌", attr=9)], media_id=1, has_more=False
        )
        self.window._on_login_requested("SESSDATA=a")
        self.window.sidebar.playlist_buttons["1"].click()

        self.window.fav_page._on_activated(0)

        self.assertEqual(self.client.video_calls, [])
        self.assertIn("失效", self.window.status_label.text())

    def test_playable_item_fetches_detail_then_plays(self) -> None:
        """能播的条目:先补一次详情(条目没有 cid),再进播放队列。"""
        self.client.folders = [_folder(1, "夹子", 1)]
        self.client.fav_pages[(1, 1)] = FavPageData(
            items=[_fav_item("BV1ok")], media_id=1, has_more=False
        )
        self.window._on_login_requested("SESSDATA=a")
        self.window.sidebar.playlist_buttons["1"].click()

        self.window.fav_page._on_activated(0)

        self.assertEqual(self.client.video_calls, ["BV1ok"])
        self.assertEqual([item.video.bvid for item in self.playback.queue.items], ["BV1ok"])

    def test_fav_item_cache_fetches_detail_then_reuses_batch_cache(self) -> None:
        """收藏夹缓存:先补详情拿分P,再交给既有批量缓存入口。"""
        self.client.folders = [_folder(1, "夹子", 1)]
        self.client.fav_pages[(1, 1)] = FavPageData(
            items=[_fav_item("BV1cache")], media_id=1, has_more=False
        )
        self.window._on_login_requested("SESSDATA=a")
        self.window.sidebar.playlist_buttons["1"].click()

        with mock.patch.object(self.window, "_cache_all_pages") as cache_all:
            self.window._on_fav_cache(0)

        self.assertEqual(self.client.video_calls, ["BV1cache"])
        cached_video = cache_all.call_args.args[0]
        self.assertEqual(cached_video.bvid, "BV1cache")

    def test_fav_menu_dispatches_the_cache_action(self) -> None:
        """收藏夹右键菜单选「缓存到本地」后要走收藏夹缓存入口。"""
        self.client.folders = [_folder(1, "夹子", 1)]
        self.client.fav_pages[(1, 1)] = FavPageData(
            items=[_fav_item("BV1menu")], media_id=1, has_more=False
        )
        self.window._on_login_requested("SESSDATA=a")
        self.window.sidebar.playlist_buttons["1"].click()
        actions = [object() for _ in range(5)]
        menu = mock.Mock()
        menu.addAction.side_effect = actions
        menu.exec.return_value = actions[3]

        with (
            mock.patch("bilibili_music.ui.main_window.QMenu", return_value=menu),
            mock.patch.object(self.window, "_on_fav_cache") as cache,
        ):
            self.window._on_fav_menu(0, mock.Mock())

        cache.assert_called_once_with(0)


class TestFavFolderActionsWiring(_WindowCase):
    """侧栏「我的歌单」的两个操作:刷新列表 / 自定义显示隐藏。

    这里钉的是三件在界面上"看起来能用、其实会出问题"的事:**连点刷新等于连发请求**
    (风控)、**登出后迟到的列表响应会把侧栏重新填满**、**隐藏设置要真的落盘并能读回来**。
    """

    def _login(self, *folders: FavFolder) -> None:
        """走一遍"登录成功并把收藏夹填进侧栏"。"""
        self.client.folders = list(folders)
        self.window._on_login_requested("SESSDATA=a")

    def _accept_hiding(self, *media_ids: int) -> Callable[[FavVisibilityDialog], int]:
        """造一个"用户点了确定"的 ``exec`` 替身:按给定 id 取消勾选。

        真弹窗会把用例挂在模态循环里,所以把它换成确定性动作;勾选与取值仍走真实控件
        (那部分在 ``tests/test_fav_visibility_dialog.py`` 里单独验)。
        """

        def fake_exec(dialog: FavVisibilityDialog) -> int:
            """按用例的意图改勾选,然后当作点了"确定"。"""
            for media_id in media_ids:
                dialog.boxes[media_id].setChecked(False)
            return QDialog.DialogCode.Accepted

        return fake_exec

    def _accept_all(self) -> Callable[[FavVisibilityDialog], int]:
        """造一个"用户点全选再点确定"的 ``exec`` 替身(用来验恢复显示)。"""

        def fake_exec(dialog: FavVisibilityDialog) -> int:
            """点「全选」把所有收藏夹放出来,然后当作点了"确定"。"""
            dialog.select_all_button.click()
            return QDialog.DialogCode.Accepted

        return fake_exec

    def _patch_exec(self, replacement: Callable[[FavVisibilityDialog], int]) -> None:
        """把弹窗的 ``exec`` 换掉,并在用例结束时还原。"""
        patcher = mock.patch.object(FavVisibilityDialog, "exec", replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_button_refetches_the_list(self) -> None:
        """点「刷新」要真的再发一次列表请求,并换成服务端最新的那一份。"""
        self._login(_folder(1, "旧夹子", 2))
        self.assertEqual(self.client.folder_calls, [42])

        # 服务端那边多了一个夹子:本机不刷新是看不到的,这正是这个按钮的意义
        self.client.folders = [_folder(1, "旧夹子", 2), _folder(2, "新夹子", 5)]
        self.window.sidebar.refresh_button.click()

        self.assertEqual(self.client.folder_calls, [42, 42])
        self.assertEqual(sorted(self.window.sidebar.playlist_buttons), ["1", "2"])
        self.assertIn("新夹子", self.window.sidebar.playlist_buttons["2"].text())

    def test_refresh_keeps_the_open_folder_selected(self) -> None:
        """刷新会整组重建侧栏按钮,选中态必须接回来(否则用户会以为选择丢了)。"""
        self._login(_folder(1, "夹子A", 2), _folder(2, "夹子B", 1))
        self.window.sidebar.playlist_buttons["1"].click()

        self.window.sidebar.refresh_button.click()

        self.assertTrue(self.window.sidebar.playlist_buttons["1"].isChecked())
        self.assertFalse(self.window.sidebar.playlist_buttons["2"].isChecked())

    def test_refresh_is_ignored_while_one_request_is_in_flight(self) -> None:
        """一次刷新还没回来时再点一次不该发第二个请求 —— 这个接口每次都是真请求。"""
        self.client.folders_defer = True
        self.window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))
        self.assertEqual(self.client.folder_calls, [42])
        self.assertEqual(self.window.sidebar.refresh_button.text(), "刷新中…")
        self.assertFalse(self.window.sidebar.refresh_button.isEnabled())

        # 直接调槽,绕过"按钮已经禁用"这一层:守卫本身也要成立(信号可能来自别处)
        self.window._on_refresh_playlists()

        self.assertEqual(self.client.folder_calls, [42])
        self.client.succeed_folders([_folder(9, "夹子", 1)])
        self.assertEqual(self.window.sidebar.refresh_button.text(), "刷新")
        self.assertTrue(self.window.sidebar.refresh_button.isEnabled())
        self.assertIn("9", self.window.sidebar.playlist_buttons)

    def test_stale_list_response_is_dropped_after_logout(self) -> None:
        """刷新途中登出:那份迟到的列表不许把侧栏重新填满。"""
        self.client.folders_defer = True
        self.window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))
        self.window._on_logout_requested()

        self.client.succeed_folders([_folder(1, "夹子", 2)])

        self.assertEqual(self.window.sidebar.playlist_buttons, {})
        self.assertFalse(self.window.sidebar.refresh_button.isEnabled())

    def test_refresh_failure_keeps_the_list_already_shown(self) -> None:
        """刷新失败要说原因,但**不许**把用户正看着的那份列表擦掉。"""
        self._login(_folder(1, "夹子", 2))
        self.client.folders_defer = True
        self.window.sidebar.refresh_button.click()

        self.client.fail_folders(NetworkError("HTTP 412"))

        self.assertIn("1", self.window.sidebar.playlist_buttons)
        self.assertIn("412", self.window.status_label.text())
        self.assertTrue(self.window.sidebar.refresh_button.isEnabled())

    def test_hidden_folders_from_config_are_not_listed(self) -> None:
        """启动时读到隐藏设置:被隐藏的夹子不出现,其余照常。"""
        self.store.save(AppConfig(fav_hidden_ids=[1]))
        self.client.folders = [_folder(1, "藏起来的", 2), _folder(2, "看得见的", 3)]
        window = MainWindow(
            client=self.client,  # type: ignore[arg-type]
            cache=AudioCache(self.tmp / "cache2", db=LibraryDb(self.tmp / "library2.db")),
            cover_cache=CoverCache(self.tmp / "covers2"),
            config_store=self.store,
            playback=self.playback,
            session_store=self.session_store,
            log_dir=self.tmp / "logs",
        )
        self.addCleanup(window.close)

        window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))

        self.assertEqual(list(window.sidebar.playlist_buttons), ["2"])
        # 隐藏只影响侧栏列出什么,收藏夹内容本身仍然读得到
        self.assertEqual([f.media_id for f in window._folders], [1, 2])  # noqa: SLF001

    def test_dialog_selection_is_saved_and_applied(self) -> None:
        """弹窗里取消勾选 → 侧栏不再列出它 → 配置落盘(重启后还在)。"""
        self._login(_folder(1, "夹子A", 2), _folder(2, "夹子B", 1))
        self._patch_exec(self._accept_hiding(2))

        self.window.sidebar.visibility_button.click()

        self.assertEqual(list(self.window.sidebar.playlist_buttons), ["1"])
        self.assertEqual(self.store.load().fav_hidden_ids, [2])
        self.assertIn("隐藏", self.window.status_label.text())

    def test_hiding_the_folder_being_browsed_keeps_its_page(self) -> None:
        """隐藏的正是当前打开的那个夹子时,内容页留着 —— 别把用户正在听的东西抽走。"""
        self._login(_folder(1, "夹子A", 2))
        self.window.sidebar.playlist_buttons["1"].click()
        self.assertIs(self.window.pages.currentWidget(), self.window.fav_page)

        self._patch_exec(self._accept_hiding(1))
        self.window.sidebar.visibility_button.click()

        self.assertEqual(self.window.sidebar.playlist_buttons, {})
        self.assertIs(self.window.pages.currentWidget(), self.window.fav_page)

    def test_unhiding_puts_the_folder_back(self) -> None:
        """全部放开时要恢复列出,并把配置里的隐藏列表清空。"""
        self.store.save(AppConfig(fav_hidden_ids=[1, 2]))
        window = MainWindow(
            client=self.client,  # type: ignore[arg-type]
            cache=AudioCache(self.tmp / "cache3", db=LibraryDb(self.tmp / "library3.db")),
            cover_cache=CoverCache(self.tmp / "covers3"),
            config_store=self.store,
            playback=self.playback,
            session_store=self.session_store,
            log_dir=self.tmp / "logs",
        )
        self.addCleanup(window.close)
        self.client.folders = [_folder(1, "夹子A", 2), _folder(2, "夹子B", 1)]
        window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))
        self.assertEqual(window.sidebar.playlist_buttons, {})
        self.assertIn("隐藏", window.sidebar.playlist_hint.text())

        self._patch_exec(self._accept_all())
        window.sidebar.visibility_button.click()

        self.assertEqual(sorted(window.sidebar.playlist_buttons), ["1", "2"])
        self.assertEqual(self.store.load().fav_hidden_ids, [])
        self.assertIn("已显示全部收藏夹", window.status_label.text())

    def test_dialog_cancel_changes_nothing(self) -> None:
        """点"取消"不该动侧栏,也不该写配置。"""
        self._login(_folder(1, "夹子A", 2))
        self._patch_exec(lambda dialog: QDialog.DialogCode.Rejected)

        self.window.sidebar.visibility_button.click()

        self.assertEqual(list(self.window.sidebar.playlist_buttons), ["1"])
        self.assertEqual(self.store.load().fav_hidden_ids, [])
        self.assertIsNone(self.window.visibility_dialog)

    def test_manage_without_folders_asks_to_refresh_first(self) -> None:
        """还没拿到列表时点「显示/隐藏」:说清楚先刷新,而不是弹一个空弹窗。"""
        self.client.folders = []
        self.window._apply_account(AccountInfo(is_login=True, mid=42, uname="测试账号"))

        self.window.sidebar.visibility_button.click()

        self.assertIn("刷新", self.window.status_label.text())
        self.assertIsNone(self.window.visibility_dialog)

    def test_logged_out_clears_the_empty_hint_wording(self) -> None:
        """登出后侧栏要退回"登录后显示收藏夹",而不是留着"都被隐藏了"。"""
        self._login(_folder(1, "夹子A", 2))
        self.window._set_hidden_folders([1])
        self.assertIn("隐藏", self.window.sidebar.playlist_hint.text())

        self.window._on_logout_requested()

        self.assertIn("登录", self.window.sidebar.playlist_hint.text())
        self.assertFalse(self.window.sidebar.refresh_button.isEnabled())


class TestTitleBarWiring(_WindowCase):
    """自绘标题栏:搜索、窗口按钮与无边框窗口的状态。"""

    def test_window_is_frameless(self) -> None:
        """既然自绘了标题栏,就不能再留着系统边框(否则会出现两条标题栏)。"""
        self.assertTrue(
            self.window.windowFlags() & Qt.WindowType.FramelessWindowHint
        )

    def test_close_button_closes_the_window(self) -> None:
        """关闭键要真的关窗(并走退出清理:落盘配置、停播放、关客户端)。"""
        closed: list[bool] = []
        self.window.closeEvent = lambda event: closed.append(True)  # type: ignore[method-assign]
        self.window.title_bar.close_button.click()
        self.assertTrue(closed)

    def test_search_signal_is_raised_by_return_pressed(self) -> None:
        """在搜索框里回车等同于点"搜索"。"""
        got: list[bool] = []
        self.window.title_bar.search_requested.connect(lambda: got.append(True))
        self.window.title_bar.search_input.returnPressed.emit()
        self.assertEqual(got, [True])


class TestPlaybackFeedbackWiring(_WindowCase):
    """编排层信号回到界面的那一半。"""

    def test_audio_ready_updates_now_playing(self) -> None:
        """音源就绪后标题、副标题与状态栏都要更新。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        self.assertIn("视频A", self.window.player_bar.title_label.text())
        self.assertIn("192K", self.window.player_bar.subtitle_label.text())
        self.assertIn("A.m4a", self.window.status_label.text())
        self.assertTrue(self.window.progress.isHidden())  # 缓存完成,进度条收起
    def test_progress_bar_tracks_cache_progress(self) -> None:
        """下载进度要显示成百分比。"""
        self._search_and_play(0)
        self.playback.progress.emit(512, 1024)
        self.assertEqual(self.window.progress.value(), 50)
        self.assertIn("缓存中", self.window.status_label.text())

    def test_state_changed_drives_the_play_button(self) -> None:
        """播放状态要换掉播放按钮的图标与提示。"""
        self.player.state_changed.emit(True)
        self.assertEqual(self.window.player_bar.play_button.toolTip(), "暂停")
        self.player.state_changed.emit(False)
        self.assertEqual(self.window.player_bar.play_button.toolTip(), "播放")

    def test_position_changed_updates_the_bar(self) -> None:
        """位置信号要驱动进度条与时间标签。"""
        self.player.position_changed.emit(65_000, 180_000)
        self.assertEqual(self.window.player_bar.duration_label.text(), "3:00")
        self.assertEqual(self.window.player_bar.position_label.text(), "1:05")

    def test_track_finished_walks_pages_within_the_ui(self) -> None:
        """合集内部播完一P要自动接下一P(整条链路走界面拿到的信号)。"""
        self._search_and_play(1)
        self.player.track_finished.emit()
        self.assertEqual(self.resolver.calls[-1][0].bvid, "B")
        self.assertEqual(self.resolver.calls[-1][1], 2)

    def test_stopped_shows_a_message(self) -> None:
        """编排层说没有下一项时状态栏要说话。"""
        self.playback.stopped.emit()
        self.assertEqual(self.window.status_label.text(), "播放结束")

    def test_resolve_failure_shows_a_warning(self) -> None:
        """解析失败必须让用户看见(这里用记录器代替模态弹窗)。"""
        self._search_and_play(0)
        self.resolver.fail(RuntimeError("音源没了"))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("音源没了", self.warnings[0])
        self.assertIn("音源没了", self.window.status_label.text())


class TestConfigWiring(_WindowCase):
    """配置的读取与落盘。"""

    def test_saved_volume_and_mode_are_applied_on_start(self) -> None:
        """启动时要套用上次的音量与播放模式。"""
        self.store.save(AppConfig(volume=42, play_mode=PlayMode.SHUFFLE))
        window = MainWindow(
            client=_FakeClient([]),  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=self.store,
            library=LibraryDb(self.tmp / "library.db"),
            playback=PlaybackController(_FakeResolver(), _FakePlayer()),  # type: ignore[arg-type]
            # 会话存储也要落到沙箱:默认路径是用户真实的配置目录(见模块 docstring)
            session_store=SessionStore(self.tmp / "session.json"),
        )
        self.addCleanup(window.close)
        self.assertEqual(window.player_bar.volume_slider.value(), 42)
        self.assertIs(window.playback.queue.mode, PlayMode.SHUFFLE)

    def test_switch_track_persists_the_previous_position(self) -> None:
        """切歌时把上一首的进度落盘,这样"上次播到哪"才有意义。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        self.player.position_changed.emit(45_000, 180_000)
        self.window.player_bar.next_button.click()
        saved = self.store.load()
        self.assertEqual(saved.last_bvid, "A")
        self.assertEqual(saved.last_cid, 1000)
        self.assertEqual(saved.last_position_ms, 45_000)

    def test_close_persists_position_and_releases_resources(self) -> None:
        """退出时要落盘进度、停播放并关掉网络客户端。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        self.player.position_changed.emit(9_000, 180_000)
        self.window.close()
        self.assertEqual(self.store.load().last_position_ms, 9_000)
        self.assertGreaterEqual(self.player.stop_count, 1)
        self.assertEqual(self.client.closed, 1)

    def test_config_save_failure_does_not_break_playback(self) -> None:
        """配置写不进去时只提示,不能中断播放。"""
        with mock.patch.object(
            ConfigStore, "save", side_effect=OSError("磁盘满了")
        ):
            self.window.player_bar.volume_slider.setValue(10)
        self.assertIn("配置保存失败", self.window.status_label.text())
        self.assertEqual(self.player.volumes[-1], 0.1)


class TestCoverWiring(_WindowCase):
    """封面:请求时机、显示、失败兜底与"切歌作废旧响应"。"""

    def _play_and_request_cover(self) -> str:
        """播第一首并返回它请求的封面地址。"""
        self._search_and_play(0)
        self.resolver.succeed(_resolved(self.videos[0]))
        return self.client.cover_calls[-1]

    def test_audio_ready_requests_the_https_cover(self) -> None:
        """详情就绪后要去取封面,而且用的是升级成 https 的地址。

        列表与队列也会为同一批视频取封面(各有各的加载器),所以这里只看**最后一次**
        请求 —— 那一次是播放条为当前曲目发的。
        """
        self._play_and_request_cover()
        self.assertEqual(self.client.cover_calls[-1], self.videos[0].cover_https)
        self.assertTrue(self.client.cover_calls[-1].startswith("https://"))

    def test_cover_is_shown_when_it_arrives(self) -> None:
        """封面到达后要真的贴上,而不是一直挂占位图。"""
        url = self._play_and_request_cover()
        self.assertFalse(self.window.player_bar.has_cover)
        self.client.succeed_cover(url, _png_bytes())
        self.assertTrue(self.window.player_bar.has_cover)

    def test_unreadable_bytes_fall_back_to_placeholder(self) -> None:
        """拿到的不是图片就退回占位图,不能把乱码贴上去。"""
        url = self._play_and_request_cover()
        self.client.succeed_cover(url, b"not an image")
        self.assertFalse(self.window.player_bar.has_cover)

    def test_cover_failure_is_silent(self) -> None:
        """取封面失败不干扰用户:不弹窗、不报警,只显示占位图。"""
        url = self._play_and_request_cover()
        self.client.fail_cover(url, RuntimeError("超时"))
        self.assertFalse(self.window.player_bar.has_cover)
        self.assertEqual(self.warnings, [])

    def test_switching_track_clears_the_previous_cover(self) -> None:
        """换歌要先把旧封面撤掉,否则会挂着上一首的图等新封面。"""
        url = self._play_and_request_cover()
        self.client.succeed_cover(url, _png_bytes())
        self.assertTrue(self.window.player_bar.has_cover)
        self.window.player_bar.next_button.click()
        self.assertFalse(self.window.player_bar.has_cover)

    def test_late_cover_of_the_previous_track_is_ignored(self) -> None:
        """切歌之后才回来的旧封面不许贴上去。"""
        url = self._play_and_request_cover()
        self.window.player_bar.next_button.click()
        self.client.succeed_cover(url, _png_bytes())
        self.assertFalse(self.window.player_bar.has_cover)

    def test_fetched_cover_is_reused_by_the_next_run(self) -> None:
        """封面要落进磁盘缓存:重开应用(新窗口 + 清空的内存缓存)能直接贴出来,不再请求。

        这是磁盘缓存存在的**唯一理由** —— 封面与 API 共用限速器,冷启动首屏每张图都
        重新下载的话,十几行要等十几秒。列表里其余几行仍然走网络(它们本来就没缓存过),
        所以这里只断言"这一张没被再请求"。
        """
        url = self._play_and_request_cover()
        self.client.succeed_cover(url, _png_bytes())
        self.assertIsNotNone(CoverCache(self.tmp / "covers").read(url))

        # 模拟重启:内存缓存清空,QMessageBox 记录器与配置沿用同一个沙箱
        QPixmapCache.clear()
        restarted = _FakeClient(self.videos)
        window = MainWindow(
            client=restarted,  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            cover_cache=CoverCache(self.tmp / "covers"),
            config_store=self.store,
            library=LibraryDb(self.tmp / "library.db"),
            playback=PlaybackController(_FakeResolver(), _FakePlayer()),  # type: ignore[arg-type]
            # 同上:会话存储必须指向沙箱,否则这个"重启"会去读用户真实的凭据文件
            session_store=SessionStore(self.tmp / "session.json"),
        )
        self.addCleanup(window.close)
        window.title_bar.search_input.setText("周杰伦")
        window.on_search()

        self.assertEqual(window.result_list.cover_url_at(0), url)
        self.assertTrue(window.result_list.has_cover_at(0))  # 列表填上时就已经有图
        self.assertNotIn(url, restarted.cover_calls)


class TestThemeWiring(_WindowCase):
    """深色主题的应用(单主题,没有切换按钮)。"""

    def _window_color(self) -> str:
        """当前应用级窗口底色。"""
        app = QApplication.instance()
        assert app is not None
        return app.palette().color(QPalette.ColorRole.Window).name()

    def test_dark_palette_and_stylesheet_are_applied_on_start(self) -> None:
        """造出主窗口就该是深色的:调色板与样式表两样都要落到应用上。"""
        app = QApplication.instance()
        assert app is not None
        self.assertEqual(self._window_color(), SURFACES.window.lower())
        self.assertIn(DARK.accent, app.styleSheet())

    def test_no_theme_switch_button(self) -> None:
        """深色单主题:标题栏上不该再有"深浅色"切换按钮。"""
        self.assertFalse(hasattr(self.window, "theme_button"))
        self.assertFalse(hasattr(self.window.title_bar, "theme_button"))

    def test_labels_carry_the_styled_object_names(self) -> None:
        """样式全部按 ``#objectName`` 选,所以控件必须挂上约定好的名字。

        这是"样式表写好了但没人命中"的唯一防线 —— 挂错名字在界面上表现为"没样式",
        靠肉眼很难发现是哪个控件漏了。
        """
        self.assertEqual(self.window.status_label.objectName(), "StatusLabel")
        self.assertEqual(self.window.player_bar.subtitle_label.objectName(), "TrackSubtitle")
        self.assertEqual(self.window.queue_drawer.count_label.objectName(), "MutedLabel")
        self.assertEqual(self.window.result_list.objectName(), "TrackTable")


class TestLogWiring(_WindowCase):
    """日志入口:侧栏两个按钮 → 打开目录 / 导出压缩包。

    日志是这个应用里唯一"用户需要把它交给别人"的东西,所以这条路径必须真的能走通:
    导出失败要有人话提示,用户在知情同意里选了"否"就什么都别写。
    """

    def _accept_export(self, target: Path) -> None:
        """替掉两个模态弹窗,让导出路径能一口气走完。

        Args:
            target: 用户"选中"的保存路径。
        """
        question = mock.patch.object(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        save = mock.patch.object(
            QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(target), "压缩包 (*.zip)"),
        )
        question.start()
        save.start()
        self.addCleanup(question.stop)
        self.addCleanup(save.stop)

    def test_sidebar_shows_both_log_entries(self) -> None:
        """侧栏底部有两个日志入口,而且它们不占导航项。"""
        sidebar = self.window.sidebar
        self.assertEqual(sidebar.log_dir_button.text(), "打开目录")
        self.assertEqual(sidebar.export_logs_button.text(), "导出")
        self.assertNotIn("日志", sidebar.nav_buttons)

    def test_open_log_dir_creates_the_directory_and_opens_it(self) -> None:
        """点"打开目录":目录不存在时先建出来,再交给系统文件管理器。

        目录不存在就直接调 ``openUrl`` 会让用户看到一句看不懂的系统报错。
        """
        opened: list[str] = []
        with mock.patch.object(
            QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
        ):
            self.window.sidebar.log_dir_button.click()
        self.assertEqual(opened, [str(self.tmp / "logs")])
        self.assertTrue((self.tmp / "logs").is_dir())

    def test_export_writes_an_archive_into_the_chosen_path(self) -> None:
        """点"导出":弹知情同意 → 选路径 → 真的写出一个可读的压缩包。"""
        target = self.tmp / "导出" / "BiliMusic-logs.zip"
        self._accept_export(target)
        self.window.sidebar.export_logs_button.click()
        self.assertTrue(target.exists())
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            text = archive.read(ENVIRONMENT_FILE_NAME).decode("utf-8")
        self.assertIn(ENVIRONMENT_FILE_NAME, names)
        # Qt 版本只有界面层拿得到,必须由界面注入(core 不许 import Qt)
        self.assertIn(f"Qt={qVersion()}", text)
        self.assertIn(str(target), self.window.status_label.text())

    def test_declining_the_consent_dialog_writes_nothing(self) -> None:
        """用户在知情同意里选"否"时不写任何文件。

        日志里有用户搜过的关键词,不问就发是不对的 —— 这里钉住"问了而且当真"。
        """
        target = self.tmp / "不该出现的.zip"
        with mock.patch.object(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No
        ):
            self.window.sidebar.export_logs_button.click()
        self.assertFalse(target.exists())
        self.assertFalse(list(self.tmp.glob("*.zip")))

    def test_cancelling_the_file_dialog_writes_nothing(self) -> None:
        """取消保存对话框(返回空路径)时什么都不做,也不报错。

        知情同意那一步仍要过(选"是"),否则卡在它上面 —— 这正是本用例第一版写漏的地方:
        只拦了一个弹窗,另一个就把整个模块挂死了。
        """
        with (
            mock.patch.object(
                QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
            ),
            mock.patch.object(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", "")),
        ):
            self.window.sidebar.export_logs_button.click()
        self.assertFalse(list(self.tmp.glob("*.zip")))
        self.assertNotIn("失败", self.window.status_label.text())

    def test_export_failure_is_reported_to_the_user(self) -> None:
        """导出失败(磁盘满、无权限)要变成状态栏 + 弹窗里的人话,不能把界面炸掉。"""
        target = self.tmp / "随便.zip"
        self._accept_export(target)
        with mock.patch.object(
            main_window_module, "export_logs", side_effect=OSError("磁盘满了")
        ):
            self.window.sidebar.export_logs_button.click()
        self.assertTrue(any("磁盘满了" in message for message in self.warnings))
        self.assertIn("导出日志失败", self.window.status_label.text())

    def test_status_text_is_written_to_the_log(self) -> None:
        """底部状态文字会被记进日志 —— 它就是本应用的"用户操作时间线"。"""
        with self.assertLogs("bilibili_music.ui.main_window", level="INFO") as captured:
            self.window._set_status("已加入播放队列:某首歌")  # noqa: SLF001
            self.window._set_status("")  # noqa: SLF001 - 清空提示不是事件
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any("某首歌" in message for message in messages))
        self.assertEqual(len(messages), 1, "清空状态不该记一条日志")


if __name__ == "__main__":
    unittest.main()
