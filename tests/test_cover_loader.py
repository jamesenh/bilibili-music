"""封面加载器的单元测试:缓存、过期响应与失败兜底。

不触网:取图函数换成替身,按 URL 记住回调,于是可以精确模拟"旧请求晚回来"。
需要一个 ``QApplication``(``QPixmap`` / ``QPixmapCache`` 都要求)。
"""

from __future__ import annotations

import os
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QPixmap, QPixmapCache  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bilibili_music.ui.cover_loader import CoverLoader  # noqa: E402

_URL_A = "https://i0.hdslb.com/a.jpg"
_URL_B = "https://i0.hdslb.com/b.jpg"


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
    """过期响应:先发的请求会后回来,不能把上一首的封面贴到当前这首上。"""

    def test_slow_response_for_an_old_url_is_dropped(self) -> None:
        """切歌之后才回来的旧封面要被丢弃。"""
        self.loader.load(_URL_A)
        self.loader.load(_URL_B)
        self.fetch.succeed(_URL_A, _png_bytes())  # 旧请求晚到
        self.assertEqual(self.loaded, [])
        self.assertEqual(self.failed, [])

    def test_late_failure_for_an_old_url_is_dropped(self) -> None:
        """旧请求失败也不该让当前封面退回占位图。"""
        self.loader.load(_URL_A)
        self.loader.load(_URL_B)
        self.fetch.fail(_URL_A, RuntimeError("超时"))
        self.assertEqual(self.failed, [])

    def test_current_url_still_wins(self) -> None:
        """当前 URL 的响应照常生效(证明上一条不是因为整条链路坏了)。"""
        self.loader.load(_URL_A)
        self.loader.load(_URL_B)
        self.fetch.succeed(_URL_B, _png_bytes())
        self.assertEqual([url for url, *_ in self.loaded], [_URL_B])

    def test_is_current_tracks_the_latest_request(self) -> None:
        """is_current 只认最近一次请求,供调用方判断结果还要不要。"""
        self.loader.load(_URL_A)
        self.assertTrue(self.loader.is_current(_URL_A))
        self.loader.load(_URL_B)
        self.assertFalse(self.loader.is_current(_URL_A))
        self.assertTrue(self.loader.is_current(_URL_B))
        self.assertFalse(self.loader.is_current(""))


if __name__ == "__main__":
    unittest.main()
