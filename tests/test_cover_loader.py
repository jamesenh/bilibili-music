"""封面加载器的单元测试:缓存、串行取图、整批清空与失败兜底。

不触网:取图函数换成替身,按 URL 记住回调,于是可以精确模拟"旧请求晚回来"与
"一次只发一张"。

需要一个 ``QApplication``(``QPixmap`` / ``QPixmapCache`` 都要求)。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QPixmap, QPixmapCache  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bilibili_music.core.cover_cache import CoverCache  # noqa: E402
from bilibili_music.ui.cover_loader import CoverLoader  # noqa: E402

_URL_A = "https://i0.hdslb.com/a.jpg"
_URL_B = "https://i0.hdslb.com/b.jpg"
_URL_C = "https://i0.hdslb.com/c.jpg"

#: 落盘类用例的临时目录根(与 test_config / test_ui_wiring 同一套做法:不用 tempfile)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"


def setUpModule() -> None:
    """整个模块共用一个 ``QApplication``。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)


def _png_bytes() -> bytes:
    """造一段**真能解码**的 PNG。

    否则失败路径与成功路径就区分不出来了 —— ``QPixmap.loadFromData(b"x")`` 也是失败。
    """
    pixmap = QPixmap(4, 4)
    pixmap.fill(Qt.GlobalColor.red)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


class _FakeFetch:
    """取图替身:按 URL 保存回调,便于模拟"先发的请求后回来"。"""

    def __init__(self) -> None:
        """建一个没有请求记录的替身。"""
        self.calls: list[str] = []
        self._callbacks: dict[str, tuple[Callable, Callable]] = {}

    def __call__(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """记录一次取图请求。"""
        self.calls.append(url)
        self._callbacks[url] = (on_success, on_error)

    def succeed(self, url: str, data: bytes) -> None:
        """触发某个 URL 的成功回调。"""
        self._callbacks[url][0](data)

    def fail(self, url: str, exc: Exception) -> None:
        """触发某个 URL 的失败回调。"""
        self._callbacks[url][1](exc)


def _record(signal) -> list[tuple]:  # noqa: ANN001 - Qt Signal
    """把信号参数按元组收集进列表。"""
    got: list[tuple] = []
    signal.connect(lambda *args: got.append(args))
    return got


class _LoaderCase(unittest.TestCase):
    """装好替身与加载器的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建一份干净的替身 + 加载器,并收集两个信号。

        **必须清 ``QPixmapCache``**:它是进程级共享的,上一个用例缓存过的封面会让
        这一个用例直接命中缓存、根本不发请求 —— 表现为 "取图替身没被调用"。
        """
        QPixmapCache.clear()
        self.fetch = _FakeFetch()
        self.loader = CoverLoader(self.fetch)  # type: ignore[arg-type]
        self.loaded = _record(self.loader.loaded)
        self.failed = _record(self.loader.failed)


class TestCoverLoaderSuccess(_LoaderCase):
    """成功路径。"""

    def test_decodable_image_emits_a_pixmap(self) -> None:
        """拿到能解码的图片要发出 QPixmap。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(len(self.loaded), 1)
        url, pixmap = self.loaded[0]
        self.assertEqual(url, _URL_A)
        self.assertFalse(pixmap.isNull())

    def test_second_load_of_the_same_url_hits_the_cache(self) -> None:
        """同一张图第二次要用缓存,不再发请求(来回切歌时会反复命中)。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, _png_bytes())
        self.loader.load(_URL_A)
        self.assertEqual(self.fetch.calls, [_URL_A])  # 只请求过一次
        self.assertEqual(len(self.loaded), 2)  # 但每次都通知了调用方

    def test_non_image_bytes_count_as_failure(self) -> None:
        """拿到的不是图片按失败处理,不能把乱码当封面贴上去。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, b"definitely not an image")
        self.assertEqual(self.loaded, [])
        self.assertEqual([url for url, *_ in self.failed], [_URL_A])

    def test_empty_body_counts_as_failure(self) -> None:
        """空响应体同样是失败:CDN 偶尔会返回 200 但内容为空。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, b"")
        self.assertEqual(self.loaded, [])
        self.assertEqual(len(self.failed), 1)


class TestCoverLoaderFailure(_LoaderCase):
    """失败与边界。"""

    def test_network_error_emits_failed(self) -> None:
        """网络失败要发 failed,由调用方显示占位图。"""
        self.loader.load(_URL_A)
        self.fetch.fail(_URL_A, RuntimeError("连不上"))
        self.assertEqual(self.loaded, [])
        self.assertEqual([url for url, *_ in self.failed], [_URL_A])

    def test_empty_url_fails_without_any_request(self) -> None:
        """空 URL 表示"这一首没有封面":立刻失败,且**不发**请求。"""
        self.loader.load("")
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(self.failed, [("",)])

    def test_blank_url_is_treated_as_empty(self) -> None:
        """只有空白的 URL 与空串等价。"""
        self.loader.load("   ")
        self.assertEqual(self.fetch.calls, [])


class TestCoverLoaderStaleness(_LoaderCase):
    """清空之后的旧响应:先发的请求会后回来,不许再往界面上贴。"""

    def test_responses_for_several_urls_all_arrive(self) -> None:
        """多张封面同时在等是常态(列表一屏十几行),每一张回来都要通知调用方。"""
        self.loader.load(_URL_A)
        self.loader.load(_URL_B)
        # 一次只发一张,所以 B 要先等 A 回来
        self.assertEqual(self.fetch.calls, [_URL_A])
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(self.fetch.calls, [_URL_A, _URL_B])
        self.fetch.succeed(_URL_B, _png_bytes())
        self.assertEqual([url for url, *_ in self.loaded], [_URL_A, _URL_B])

    def test_late_failure_after_clear_is_dropped(self) -> None:
        """清空之后回来的失败不该把当前封面打回占位图。"""
        self.loader.load(_URL_A)
        self.loader.load("")
        self.fetch.fail(_URL_A, RuntimeError("超时"))
        self.assertEqual(self.failed, [("",)])

    def test_late_success_after_clear_is_dropped(self) -> None:
        """清空之后回来的封面同样要丢掉(切歌时旧封面不能贴到新歌上)。"""
        self.loader.load(_URL_A)
        self.loader.load("")
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(self.loaded, [])

    def test_is_current_tracks_the_latest_request(self) -> None:
        """is_current 只认最近一次请求,供播放条判断结果还要不要。"""
        self.loader.load(_URL_A)
        self.assertTrue(self.loader.is_current(_URL_A))
        self.loader.load(_URL_B)
        self.assertFalse(self.loader.is_current(_URL_A))
        self.assertTrue(self.loader.is_current(_URL_B))
        self.assertFalse(self.loader.is_current(""))


class TestCoverLoaderQueue(_LoaderCase):
    """取图队列:同一时刻只放一个请求出去、重复地址不重复请求。"""

    def test_only_one_request_is_in_flight(self) -> None:
        """一次只发一张 —— 一屏几十张封面要是同时甩出去就是请求突发(风控最忌)。"""
        for url in (_URL_A, _URL_B, _URL_C):
            self.loader.load(url)
        self.assertEqual(self.fetch.calls, [_URL_A])

    def test_queued_urls_are_fetched_in_order(self) -> None:
        """排队顺序就是入队顺序(列表从上往下填封面,先来的先取)。"""
        for url in (_URL_A, _URL_B, _URL_C):
            self.loader.load(url)
        self.fetch.succeed(_URL_A, _png_bytes())
        self.fetch.succeed(_URL_B, _png_bytes())
        self.fetch.succeed(_URL_C, _png_bytes())
        self.assertEqual(self.fetch.calls, [_URL_A, _URL_B, _URL_C])
        self.assertEqual([url for url, *_ in self.loaded], [_URL_A, _URL_B, _URL_C])

    def test_loading_the_same_url_twice_requests_it_once(self) -> None:
        """同一张图排两次队只该取一次(列表里同一首歌可能出现多次)。"""
        self.loader.load(_URL_A)
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(self.fetch.calls, [_URL_A])

    def test_clear_drops_the_whole_queue(self) -> None:
        """清空之后不再发新的请求:列表已经换了一批,旧图没人看了。"""
        for url in (_URL_A, _URL_B):
            self.loader.load(url)
        self.loader.clear()
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(self.fetch.calls, [_URL_A])  # 只发过第一张
        self.assertEqual(self.loaded, [])  # 而且回来的那张也不要了

    def test_clear_does_not_report_failure(self) -> None:
        """``clear`` 与 ``load("")`` 的区别就在这里:清空不表示"当前封面没了"。"""
        self.loader.load(_URL_A)
        self.loader.clear()
        self.assertEqual(self.failed, [])

    def test_failure_releases_the_queue(self) -> None:
        """一张失败不能把后面几张卡死 —— 失败之后要继续取下一张。"""
        self.loader.load(_URL_A)
        self.loader.load(_URL_B)
        self.fetch.fail(_URL_A, RuntimeError("连不上"))
        self.assertEqual(self.fetch.calls, [_URL_A, _URL_B])


class TestCoverLoaderDiskCache(_LoaderCase):
    """磁盘缓存:跨进程命中、成功落盘、坏条目自愈,以及"清空不删盘"。"""

    def setUp(self) -> None:
        """在公共基类之上再挂一个注入式缓存目录与带磁盘缓存的加载器。"""
        super().setUp()
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = CoverCache(self.tmp)
        self.loader = CoverLoader(self.fetch, cache=self.cache)  # type: ignore[arg-type]
        self.loaded = _record(self.loader.loaded)
        self.failed = _record(self.loader.failed)

    def test_successful_fetch_is_stored_on_disk(self) -> None:
        """取回来的图要落盘 —— 否则"下次启动不用重新下载"就无从谈起。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(self.cache.read(_URL_A), _png_bytes())

    def test_network_failure_stores_nothing(self) -> None:
        """网络失败时磁盘上不该多出任何文件(尤其不能是 0 字节的假命中)。"""
        self.loader.load(_URL_A)
        self.fetch.fail(_URL_A, RuntimeError("连不上"))
        self.assertIsNone(self.cache.read(_URL_A))

    def test_disk_hit_does_not_touch_the_network(self) -> None:
        """磁盘命中要立刻发信号且**不发请求**:封面与 API 共用限速器,省一次是一次。"""
        self.cache.store(_URL_A, _png_bytes())
        self.loader.load(_URL_A)
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(len(self.loaded), 1)
        self.assertFalse(self.loaded[0][1].isNull())

    def test_disk_hit_survives_a_restart(self) -> None:
        """重开应用(新的加载器 + 空的 QPixmapCache)仍然命中磁盘,不再下载。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, _png_bytes())
        # 模拟进程重启:内存缓存全清,换一个全新的加载器,磁盘缓存保持不变
        QPixmapCache.clear()
        restarted = CoverLoader(self.fetch, cache=self.cache)  # type: ignore[arg-type]
        loaded = _record(restarted.loaded)
        restarted.load(_URL_A)
        self.assertEqual(self.fetch.calls, [_URL_A])  # 只发过第一次那一个请求
        self.assertEqual(len(loaded), 1)
        self.assertFalse(loaded[0][1].isNull())

    def test_disk_hit_is_also_put_into_the_memory_cache(self) -> None:
        """磁盘命中要回填内存缓存:否则同一张图在列表里滚动时会反复读盘。"""
        self.cache.store(_URL_A, _png_bytes())
        self.loader.load(_URL_A)
        self.assertIsNotNone(QPixmapCache.find(_URL_A))

    def test_corrupt_cache_entry_is_dropped_and_refetched(self) -> None:
        """磁盘上的坏条目(解不出图)要清掉并重新下载,而不是永远卡在占位图。"""
        self.cache.store(_URL_A, b"definitely not an image")
        self.loader.load(_URL_A)
        self.assertEqual(self.fetch.calls, [_URL_A])  # 坏条目不能算命中
        self.fetch.succeed(_URL_A, _png_bytes())
        self.assertEqual(self.cache.read(_URL_A), _png_bytes())  # 已被好图覆盖

    def test_clear_keeps_the_disk_cache(self) -> None:
        """``clear`` 清的是排队与在飞请求,不动磁盘:列表换一批不该删掉用户攒下的封面。"""
        self.loader.load(_URL_A)
        self.fetch.succeed(_URL_A, _png_bytes())
        self.loader.clear()
        self.assertEqual(self.cache.read(_URL_A), _png_bytes())


if __name__ == "__main__":
    unittest.main()
