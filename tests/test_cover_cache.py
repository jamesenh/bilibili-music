"""封面磁盘缓存的单元测试:落盘、命中、坏条目、容量上限与清理。

纯逻辑,不碰 Qt、不触网:被测对象只认 ``Path``,所以每个用例注入
``tests/_scratch/<用例名>`` 当缓存目录(与 ``test_config`` / ``test_ui_wiring``
同一套做法,不用 ``tempfile`` —— 见 ``AGENTS.md`` 第 6 节)。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.cover_cache import (  # noqa: E402
    COVERS_DIR_NAME,
    DEFAULT_MAX_BYTES,
    CoverCache,
)

#: 落盘类用例的临时目录根(与 test_config / test_ui_wiring 共用,不用 tempfile)。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"

_URL = "https://i0.hdslb.com/bfs/archive/aaa.jpg"

#: 一小段 PNG 头,够让魔数判断认出格式(缓存层不解码,所以不需要真图)。
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

#: 一小段 JPEG 头。
_JPG = b"\xff\xd8\xff\xe0" + b"0" * 32

#: RIFF 容器 + WEBP FourCC。
_WEBP = b"RIFF" + (40).to_bytes(4, "little") + b"WEBP" + b"0" * 32


class _CacheCase(unittest.TestCase):
    """装好注入式缓存目录的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建逐用例的沙箱目录并绑定一个缓存实例。"""
        self.tmp = _SCRATCH / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = CoverCache(self.tmp)

    def _files(self) -> list[str]:
        """缓存目录里的文件名(排序后,便于断言)。"""
        return sorted(p.name for p in self.tmp.iterdir())


class TestCoverCacheRoundTrip(_CacheCase):
    """落盘与命中。"""

    def test_stored_bytes_can_be_read_back(self) -> None:
        """存进去的字节要能原样读回来 —— 这是缓存的基本契约。"""
        self.cache.store(_URL, _PNG)
        self.assertEqual(self.cache.read(_URL), _PNG)

    def test_lookup_finds_the_file_and_read_is_byte_exact(self) -> None:
        """lookup 给出的路径就是真正的缓存文件,而且内容一致。"""
        path = self.cache.store(_URL, _JPG)
        self.assertIsNotNone(path)
        self.assertEqual(self.cache.lookup(_URL), path)
        self.assertEqual(path.read_bytes(), _JPG)  # type: ignore[union-attr]

    def test_same_url_maps_to_the_same_key(self) -> None:
        """同一个 URL 必须始终映射到同一个键,否则缓存永远命不中。"""
        self.assertEqual(self.cache.key_for(_URL), self.cache.key_for(_URL))

    def test_different_urls_get_different_keys(self) -> None:
        """不同 URL 不能共用一个键(会把别的歌的封面贴上来)。"""
        other = _URL.replace("aaa", "bbb")
        self.assertNotEqual(self.cache.key_for(_URL), self.cache.key_for(other))

    def test_unknown_url_is_a_miss(self) -> None:
        """没存过的 URL 读出来是 None,而不是抛异常。"""
        self.assertIsNone(self.cache.lookup(_URL))
        self.assertIsNone(self.cache.read(_URL))

    def test_empty_url_is_a_miss(self) -> None:
        """空 URL 直接当未命中(列表里没有封面的行就是空串)。"""
        self.assertIsNone(self.cache.lookup(""))
        self.assertIsNone(self.cache.read(""))

    def test_store_ignores_empty_data(self) -> None:
        """空字节不落盘:否则会留下一个"命中但贴不出图"的 0 字节文件。"""
        self.assertIsNone(self.cache.store(_URL, b""))
        self.assertIsNone(self.cache.read(_URL))

    def test_zero_byte_file_counts_as_a_miss(self) -> None:
        """磁盘上已有的 0 字节文件按未命中处理(上次写到一半被杀的残留)。"""
        path = self.tmp / f"{self.cache.key_for(_URL)}.jpg"
        path.write_bytes(b"")
        self.assertIsNone(self.cache.lookup(_URL))


class TestCoverCacheSuffix(_CacheCase):
    """扩展名按图片魔数推断(缓存目录是给用户看的)。"""

    def test_png_jpeg_and_webp_get_real_extensions(self) -> None:
        """常见格式要落成一眼能认出来的文件名。"""
        for url, data, suffix in (
            (_URL, _PNG, ".png"),
            (_URL.replace("aaa", "b"), _JPG, ".jpg"),
            (_URL.replace("aaa", "c"), _WEBP, ".webp"),
        ):
            path = self.cache.store(url, data)
            self.assertIsNotNone(path)
            self.assertEqual(path.suffix, suffix)  # type: ignore[union-attr]

    def test_riff_that_is_not_webp_falls_back(self) -> None:
        """RIFF 是容器头,WAV 也会带它 —— 不能把音频当成图片存成 .webp。"""
        wav = b"RIFF" + (40).to_bytes(4, "little") + b"WAVE" + b"0" * 32
        path = self.cache.store(_URL, wav)
        self.assertEqual(path.suffix, ".img")  # type: ignore[union-attr]

    def test_unrecognised_format_falls_back(self) -> None:
        """认不出格式时用兜底后缀,内容照样能存能读(读回按内容解码)。"""
        path = self.cache.store(_URL, b"\x00\x01\x02\x03")
        self.assertEqual(path.suffix, ".img")  # type: ignore[union-attr]
        self.assertEqual(self.cache.read(_URL), b"\x00\x01\x02\x03")

    def test_lookup_works_regardless_of_suffix(self) -> None:
        """查找是按候选后缀依次 stat 的,四种格式都要能命中。"""
        for index, data in enumerate((_PNG, _JPG, _WEBP, b"\x00\x01")):
            url = _URL.replace("aaa", f"v{index}")
            self.cache.store(url, data)
            self.assertEqual(self.cache.read(url), data)

    def test_restoring_with_another_format_leaves_no_duplicate(self) -> None:
        """同一 URL 换了格式重新落盘后,旧的另一种后缀文件必须被清掉。"""
        self.cache.store(_URL, _PNG)
        self.cache.store(_URL, _JPG)
        self.assertEqual(len(self._files()), 1)
        self.assertEqual(self.cache.read(_URL), _JPG)


class TestCoverCacheAtomicity(_CacheCase):
    """写入必须原子:不留临时文件,失败也不影响调用方。"""

    def test_no_temp_file_is_left_behind(self) -> None:
        """正常落盘后目录里只有正式文件(``.part`` 已被 os.replace 顶掉)。"""
        self.cache.store(_URL, _PNG)
        self.assertFalse([name for name in self._files() if name.endswith(".part")])

    def test_write_failure_returns_none_instead_of_raising(self) -> None:
        """写不进去(这里让缓存路径被一个文件占住)只能当这次没缓存,不能抛异常。"""
        blocked = self.tmp / "blocked"
        blocked.write_bytes(b"x")
        self.assertIsNone(CoverCache(blocked).store(_URL, _PNG))

    def test_write_failure_does_not_create_the_directory(self) -> None:
        """失败之后不该留下半个目录或临时文件。"""
        blocked = self.tmp / "blocked"
        blocked.write_bytes(b"x")
        CoverCache(blocked).store(_URL, _PNG)
        self.assertTrue(blocked.is_file())
        self.assertEqual(self._files(), ["blocked"])

    def test_discard_removes_the_file(self) -> None:
        """坏条目要能删掉:留着它每次加载都要白读一遍盘。"""
        self.cache.store(_URL, _PNG)
        self.cache.discard(_URL)
        self.assertIsNone(self.cache.read(_URL))
        self.assertEqual(self._files(), [])

    def test_discard_is_safe_when_nothing_is_cached(self) -> None:
        """重复删、删不存在的东西都不该出错。"""
        self.cache.discard(_URL)
        self.cache.discard("")


class TestCoverCacheSize(_CacheCase):
    """占用统计、清理与容量上限。"""

    def test_size_bytes_sums_the_files(self) -> None:
        """size_bytes 要如实反映目录占用(界面显示"缓存占用"靠它)。"""
        self.assertEqual(self.cache.size_bytes(), 0)
        self.cache.store(_URL, _PNG)
        self.cache.store(_URL.replace("aaa", "b"), _JPG)
        self.assertEqual(self.cache.size_bytes(), len(_PNG) + len(_JPG))

    def test_size_bytes_on_a_missing_directory_is_zero(self) -> None:
        """目录还不存在(一张封面都没取过)时统计出 0,而不是报错。"""
        self.assertEqual(CoverCache(self.tmp / "not-yet").size_bytes(), 0)

    def test_clear_removes_everything(self) -> None:
        """清空要删掉所有封面并返回删除个数。"""
        self.cache.store(_URL, _PNG)
        self.cache.store(_URL.replace("aaa", "b"), _JPG)
        self.assertEqual(self.cache.clear(), 2)
        self.assertEqual(self.cache.size_bytes(), 0)

    def test_clear_also_cleans_part_leftovers(self) -> None:
        """异常退出留下的 ``.part`` 也要一起清掉(它永远不会被复用)。"""
        leftover = self.tmp / "deadbeef00000000.jpg.part"
        leftover.write_bytes(b"half")
        self.cache.clear()
        self.assertEqual(self._files(), [])

    def test_default_limit_is_a_sane_positive_number(self) -> None:
        """默认上限要是正数且不至于小到存不下几张封面。"""
        self.assertGreater(DEFAULT_MAX_BYTES, 10 * 1024 * 1024)

    def test_default_root_is_a_covers_subdirectory(self) -> None:
        """默认目录是缓存根下的 covers 子目录:与音频文件分开,便于用户分辨。"""
        self.assertEqual(CoverCache().root.name, COVERS_DIR_NAME)

    def test_trim_drops_the_oldest_files(self) -> None:
        """超过上限时删掉**最早**落盘的那几张,最近看的必须留着。"""
        cache = CoverCache(self.tmp, max_bytes=3 * len(_PNG))
        urls = [_URL.replace("aaa", f"v{i}") for i in range(5)]
        for index, url in enumerate(urls):
            cache.store(url, _PNG)
            # 显式拉开 mtime:同一毫秒内落盘的文件靠时间戳分不出先后
            os.utime(cache.lookup(url), (1000 + index, 1000 + index))  # type: ignore[arg-type]
        cache.store(urls[-1], _PNG)  # 再触发一次清理
        self.assertLessEqual(cache.size_bytes(), 3 * len(_PNG))
        self.assertIsNotNone(cache.lookup(urls[-1]))  # 最新的还在
        self.assertIsNone(cache.lookup(urls[0]))  # 最早的被淘汰

    def test_trim_respects_a_zero_limit_as_unlimited(self) -> None:
        """``max_bytes <= 0`` 表示不限制:测试与"我就是要全留着"的场景用。"""
        cache = CoverCache(self.tmp, max_bytes=0)
        for index in range(4):
            cache.store(_URL.replace("aaa", f"v{index}"), _PNG)
        self.assertEqual(cache.size_bytes(), 4 * len(_PNG))

    def test_trim_keeps_everything_under_the_limit(self) -> None:
        """没超上限时不许删任何东西。"""
        cache = CoverCache(self.tmp, max_bytes=len(_PNG) * 10)
        for index in range(3):
            cache.store(_URL.replace("aaa", f"v{index}"), _PNG)
        self.assertEqual(cache.size_bytes(), 3 * len(_PNG))

    def test_trim_uses_real_sizes_not_the_estimate(self) -> None:
        """估算值偏高时(用户手动删过文件)必须以扫出来的真实占用为准,不能误删。"""
        cache = CoverCache(self.tmp, max_bytes=len(_PNG) * 2)
        cache.store(_URL, _PNG)
        cache._approx_bytes = 10**9  # type: ignore[attr-defined]  # 模拟"估算失真"
        cache.store(_URL.replace("aaa", "b"), _PNG)
        self.assertEqual(cache.size_bytes(), 2 * len(_PNG))

    def test_trim_tolerates_files_that_are_already_gone(self) -> None:
        """清理时文件已经不在了(多窗口共用同一个目录)不该让落盘报错。"""
        cache = CoverCache(self.tmp, max_bytes=len(_PNG))
        ghost = self.tmp / "ghost.jpg"
        ghost.write_bytes(_PNG)
        cache._approx_bytes = len(_PNG) * 4  # type: ignore[attr-defined]  # 估算值记着已删的文件
        ghost.unlink()
        cache.store(_URL, _PNG)
        self.assertIsNotNone(cache.lookup(_URL))

    def test_read_failure_is_a_miss(self) -> None:
        """读盘出错(文件被并发删掉、磁盘错误)按未命中处理,不能把异常抛给界面。"""
        self.cache.store(_URL, _PNG)
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("磁盘错误")):
            self.assertIsNone(self.cache.read(_URL))


if __name__ == "__main__":
    unittest.main()
