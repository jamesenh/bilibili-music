"""纯逻辑单元测试:不触网,只验证解析、模型、缓存与解压。

运行::

    uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.api.bilibili import _clean_text, _parse_duration  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.headers import (  # noqa: E402
    IncrementalDecoder,
    api_headers,
    decompress_all,
    media_headers,
)
from bilibili_music.core.models import (  # noqa: E402
    Page,
    Video,
    format_count,
    format_duration,
)


class TestFormatDuration(unittest.TestCase):
    """秒数转人类可读时长,列表里展示的正是分P自己的 duration。"""
    def test_minutes_and_seconds(self) -> None:
        """一小时以内的时长必须补零成 M:SS,否则界面会显示成 3:5 这种错位文本。"""
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(59), "0:59")
        self.assertEqual(format_duration(234), "3:54")

    def test_hours(self) -> None:
        """超过 3600 秒要切换到 H:MM:SS,长合集才不会显示成 845:22 这种分钟数溢出。"""
        self.assertEqual(format_duration(3600), "1:00:00")
        self.assertEqual(format_duration(50722), "14:05:22")

    def test_negative_is_clamped(self) -> None:
        """接口给到异常负数时要收敛成 0:00,不能让负号漏进用户看到的文案。"""
        self.assertEqual(format_duration(-5), "0:00")


class TestFormatCount(unittest.TestCase):
    """播放量、弹幕数等计数的中文单位缩写(万/亿),界面上的计数文本靠它渲染。"""
    def test_scales(self) -> None:
        """万与亿两个量级的换算要留 1 位小数,保证和 B 站站内的显示口径一致。"""
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(76883_000 // 1000), "7.7万")
        self.assertEqual(format_count(123_456_789), "1.2亿")


class TestCleanText(unittest.TestCase):
    """清洗搜索接口返回的标题文本,去掉高亮标签与 HTML 实体。"""
    def test_strips_highlight_tags(self) -> None:
        """命中关键词会被接口包成 <em class="keyword">,清洗后不能把标签当歌名显示。"""
        self.assertEqual(_clean_text("<em class=\"keyword\">周杰伦</em> MV"), "周杰伦 MV")

    def test_unescapes_entities(self) -> None:
        """标题里的 &amp; / &quot; 要还原成真实字符,否则歌名会显示成转义串。"""
        self.assertEqual(_clean_text("A &amp; B &quot;C&quot;"), 'A & B "C"')

    def test_empty(self) -> None:
        """空串与纯空白都归一成空串,避免界面上出现只剩空格的标题。"""
        self.assertEqual(_clean_text(""), "")
        self.assertEqual(_clean_text("   "), "")


class TestParseDuration(unittest.TestCase):
    """把接口返回的时长文本(如 "3:54")解析回秒数,供展示与排序使用。"""
    def test_mm_ss(self) -> None:
        """MM:SS 要按 60 进制换算,3:54 必须解析成 234 秒而不是 3 或 354。"""
        self.assertEqual(_parse_duration("3:54"), 234)

    def test_hh_mm_ss(self) -> None:
        """HH:MM:SS 要把小时一起换算,14:05:22 必须解析成 50722 秒。"""
        self.assertEqual(_parse_duration("14:05:22"), 50722)

    def test_numeric_and_plain_seconds(self) -> None:
        """整数与纯数字字符串都按秒数理解,兼容接口的两种返回形态。"""
        self.assertEqual(_parse_duration(234), 234)
        self.assertEqual(_parse_duration("234"), 234)

    def test_garbage_returns_zero(self) -> None:
        """无法解析的时长退化成 0 而不是抛异常,否则一条脏数据会毁掉整次搜索解析。"""
        # 搜索接口对超长视频有时返回 "--:--"
        self.assertEqual(_parse_duration("--:--"), 0)
        self.assertEqual(_parse_duration(""), 0)
        self.assertEqual(_parse_duration(None), 0)


class TestModels(unittest.TestCase):
    """Video/Page 数据模型:多P判定、页码查找、封面地址与稿件链接。"""
    def _video(self, n_pages: int) -> Video:
        """造一个含 n 个分P 的合集样本,分P 的 cid 与 duration 逐 P 递增,便于断言具体值。"""
        pages = [
            Page(index=i, cid=1000 + i, title=f"曲目{i}", duration=200 + i)
            for i in range(1, n_pages + 1)
        ]
        return Video(
            bvid="BV1test",
            title="合集",
            duration=sum(p.duration for p in pages),
            cid=pages[0].cid,
            pages=pages,
        )

    def test_multipart_flag(self) -> None:
        """只有 1P 的不算合集,否则单曲也会被界面当成多P而多显示一套分P 列表。"""
        self.assertFalse(self._video(1).is_multipart)
        self.assertTrue(self._video(3).is_multipart)
        self.assertEqual(self._video(3).part_count, 3)

    def test_page_lookup_is_one_based(self) -> None:
        """page() 按分P 序号(1 起始)取值,越界必须返回 None 而不是抛异常。"""
        video = self._video(3)
        self.assertEqual(video.page(2).title, "曲目2")
        self.assertEqual(video.page(3).cid, 1003)
        self.assertIsNone(video.page(0))
        self.assertIsNone(video.page(99))

    def test_page_zero_is_none_not_first(self) -> None:
        """分P是 1 起始的,page(0) 必须是 None 而不是悄悄返回第 1P。"""
        self.assertIsNone(self._video(2).page(0))

    def test_cover_scheme_upgrade(self) -> None:
        """封面地址可能是 // 或 http:// 开头,统一升级成 https 才不会触发混合内容拦截。"""
        video = Video(bvid="BV1", title="t", cover_url="//i0.hdslb.com/x.jpg")
        self.assertTrue(video.cover_https.startswith("https://"))
        video2 = Video(bvid="BV1", title="t", cover_url="http://i0.hdslb.com/x.jpg")
        self.assertTrue(video2.cover_https.startswith("https://i0.hdslb.com"))

    def test_web_url(self) -> None:
        """web_url 只由 bvid 拼出,保证界面上的“打开网页”永远指向正确稿件。"""
        self.assertEqual(Video(bvid="BV1abc", title="t").web_url, "https://www.bilibili.com/video/BV1abc")


class TestAudioCache(unittest.TestCase):
    """音频文件缓存:缓存键、命中判定、原子落盘与清理。"""
    def setUp(self) -> None:
        """为每个用例新建独立临时目录,避免用例之间互相污染缓存内容。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = AudioCache(Path(self._tmp.name))

    def tearDown(self) -> None:
        """用例结束后销毁临时目录,不留下残留文件影响后续断言。"""
        self._tmp.cleanup()

    def test_key_depends_on_all_inputs(self) -> None:
        """缓存键必须同时包含 bvid、cid、音质与编码,相同输入则稳定复现同一个键。"""
        base = self.cache.key_for("BV1", 100, 30280, "mp4a.40.2")
        # 不同分P必须落在不同缓存文件上,否则会串歌
        self.assertNotEqual(base, self.cache.key_for("BV1", 101, 30280, "mp4a.40.2"))
        self.assertNotEqual(base, self.cache.key_for("BV1", 100, 30232, "mp4a.40.2"))
        self.assertNotEqual(base, self.cache.key_for("BV2", 100, 30280, "mp4a.40.2"))
        self.assertEqual(base, self.cache.key_for("BV1", 100, 30280, "mp4a.40.2"))

    def test_miss_then_store_then_hit(self) -> None:
        """未下过必须返回 None,落盘后 lookup 命中同一路径,这是“缓存生效”的最小闭环。"""
        self.assertIsNone(self.cache.lookup("BV1", 100, 30280, "c"))
        payload = b"ftypiso5" + b"\x00" * 100
        path = self.cache.store_chunks("BV1", 100, 30280, "c", iter([payload]))
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(self.cache.lookup("BV1", 100, 30280, "c"), path)

    def test_partial_file_is_not_a_hit(self) -> None:
        """下载中断时留下的 .part 不能被当成有效缓存。"""
        final = self.cache.path_for("BV1", 100, 30280, "c")
        (final.with_suffix(final.suffix + ".part")).write_bytes(b"half")
        self.assertIsNone(self.cache.lookup("BV1", 100, 30280, "c"))
        self.cache.clear()
        self.assertIsNone(self.cache.lookup("BV1", 100, 30280, "c"))

    def test_empty_download_raises_and_leaves_no_file(self) -> None:
        """音轨为空(0 字节)时既要报错又不能留下缓存,否则会命中一个放不出声的文件。"""
        from bilibili_music.core.errors import BiliMusicError

        with self.assertRaises(BiliMusicError):
            self.cache.store_chunks("BV1", 100, 30280, "c", iter([]))
        self.assertIsNone(self.cache.lookup("BV1", 100, 30280, "c"))
        self.assertEqual(list(self.cache.root.glob("*.part")), [])

    def test_clear(self) -> None:
        """clear() 要删掉全部缓存文件并返回删除数量,统计字节数随之归零。"""
        self.cache.store_chunks("BV1", 1, 30280, "c", iter([b"x" * 10]))
        self.cache.store_chunks("BV2", 1, 30280, "c", iter([b"y" * 10]))
        self.assertEqual(self.cache.clear(), 2)
        self.assertEqual(self.cache.size_bytes(), 0)


class TestRateLimiterAndBackoff(unittest.TestCase):
    """限速与退避的时序逻辑,纯计算,可直接验证。"""

    def test_first_request_is_immediate(self) -> None:
        """首个请求不等待,限速只约束连续请求,否则冷启动会白等一轮。"""
        from bilibili_music.net.base import RateLimiter

        self.assertEqual(RateLimiter(0.8).wait_time(), 0.0)

    def test_second_request_must_wait(self) -> None:
        """两次请求之间要隔够 interval 秒,这是规避 B 站 412/429 风控的核心手段。"""
        from bilibili_music.net.base import RateLimiter

        limiter = RateLimiter(5.0)
        limiter.touch()
        self.assertGreater(limiter.wait_time(), 4.0)
        self.assertLessEqual(limiter.wait_time(), 5.0)

    def test_zero_interval_never_waits(self) -> None:
        """interval 为 0 时不得引入任何等待,关闭限速的场景依赖这个行为。"""
        from bilibili_music.net.base import RateLimiter

        limiter = RateLimiter(0.0)
        limiter.touch()
        self.assertEqual(limiter.wait_time(), 0.0)

    def test_backoff_grows_with_attempts(self) -> None:
        """退避时间随重试次数单调增长,保证风控持续时请求间隔逐步拉开。"""
        from bilibili_music.net.base import backoff_delay

        # 取多次采样的下界比较,抵消随机抖动
        lows = [min(backoff_delay(i) for _ in range(50)) for i in range(4)]
        for earlier, later in zip(lows, lows[1:]):
            self.assertLess(earlier, later)

    def test_backoff_is_capped(self) -> None:
        """退避必须有上限,否则连续失败后等待时间无限膨胀,界面会像卡死一样。"""
        from bilibili_music.core.http import MAX_BACKOFF
        from bilibili_music.net.base import backoff_delay

        self.assertLessEqual(backoff_delay(30), MAX_BACKOFF + 0.5)


class TestDownloadSink(unittest.TestCase):
    """sink 是两种后端共用的落盘路径,原子性必须可靠。"""

    def setUp(self) -> None:
        """每个用例一个独立临时目录,保证落盘断言互不干扰。"""
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        """清理临时目录,避免用例之间的残留文件互相干扰。"""
        self._tmp.cleanup()

    def test_commit_publishes_file(self) -> None:
        """commit() 后正式文件出现且 .part 消失,这是原子写入对外的可见结果。"""
        from bilibili_music.core.cache import DownloadSink

        final = self.root / "a.m4a"
        with DownloadSink(final) as sink:
            sink.write(b"abc")
            sink.write(b"def")
            sink.commit()
        self.assertEqual(final.read_bytes(), b"abcdef")
        self.assertFalse(sink.tmp_path.exists())

    def test_abort_removes_temp_and_keeps_target_absent(self) -> None:
        """abort() 要删掉 .part 且不产出正式文件,失败下载不能污染缓存。"""
        from bilibili_music.core.cache import DownloadSink

        final = self.root / "b.m4a"
        sink = DownloadSink(final).open()
        sink.write(b"partial")
        sink.abort()
        self.assertFalse(final.exists())
        self.assertFalse(sink.tmp_path.exists())

    def test_exception_inside_context_aborts(self) -> None:
        """with 块内抛异常要自动 abort,否则中断的下载会留下半个文件被当成完整缓存。"""
        from bilibili_music.core.cache import DownloadSink

        final = self.root / "c.m4a"
        sink = DownloadSink(final)
        with self.assertRaises(RuntimeError):
            with sink:
                sink.write(b"partial")
                raise RuntimeError("boom")
        self.assertFalse(final.exists())
        self.assertFalse(sink.tmp_path.exists())

    def test_commit_without_data_fails(self) -> None:
        """一个字节都没收到就 commit 属于异常路径,必须报错而不是写出空文件。"""
        from bilibili_music.core.cache import DownloadSink
        from bilibili_music.core.errors import BiliMusicError

        sink = DownloadSink(self.root / "d.m4a").open()
        with self.assertRaises(BiliMusicError):
            sink.commit()
        self.assertFalse((self.root / "d.m4a").exists())

    def test_bytes_written_counter(self) -> None:
        """write() 的返回值与 bytes_written 都要累计真实字节数,下载进度条依赖它。"""
        from bilibili_music.core.cache import DownloadSink

        sink = DownloadSink(self.root / "e.m4a").open()
        self.assertEqual(sink.write(b"12345"), 5)
        self.assertEqual(sink.bytes_written, 5)
        sink.abort()


class TestDecode(unittest.TestCase):
    """解压逻辑必须可靠:漏解压会得到乱码,之前真踩过一次。"""

    def test_gzip(self) -> None:
        """gzip 压缩的响应体必须还原成原始 JSON,漏解压会得到乱码。"""
        raw = '{"code":0}'.encode()
        self.assertEqual(decompress_all(gzip.compress(raw), "gzip"), raw)

    def test_deflate_zlib_wrapped(self) -> None:
        """带 zlib 头的 deflate 要能解压,这是接口最常见的压缩返回形态。"""
        raw = b"hello"
        self.assertEqual(decompress_all(zlib.compress(raw), "deflate"), raw)

    def test_deflate_raw(self) -> None:
        """没有 zlib 头的裸 deflate 也要能解压,两种 deflate 变体都不能漏。"""
        raw = b"hello"
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        payload = compressor.compress(raw) + compressor.flush()
        self.assertEqual(decompress_all(payload, "deflate"), raw)

    def test_identity(self) -> None:
        """空编码与 identity 表示没压缩,必须原样透传。"""
        self.assertEqual(decompress_all(b"plain", ""), b"plain")
        self.assertEqual(decompress_all(b"plain", "identity"), b"plain")

    def test_unknown_encoding_passthrough(self) -> None:
        """不认识的编码(如 br)原样返回,不能抛异常把整个请求链路打断。"""
        self.assertEqual(decompress_all(b"plain", "br"), b"plain")


class TestIncrementalDecoder(unittest.TestCase):
    """流式解压:分块喂入的结果必须与一次性解压一致。"""

    def test_gzip_split_across_chunks(self) -> None:
        """gzip 流被切成不规则小块喂入时,解压结果必须与一次性解压完全一致。"""
        raw = b"x" * 5000
        payload = gzip.compress(raw)
        decoder = IncrementalDecoder("gzip")
        out = bytearray()
        # 故意切成不规则的块,模拟网络分包
        for i in range(0, len(payload), 7):
            out.extend(decoder.feed(payload[i : i + 7]))
        out.extend(decoder.finish())
        self.assertEqual(bytes(out), raw)

    def test_deflate_raw_fallback(self) -> None:
        """裸 deflate 的流式解压要能自动回退尝试,覆盖不发 zlib 头的服务器。"""
        raw = b"hello world" * 100
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        payload = compressor.compress(raw) + compressor.flush()
        decoder = IncrementalDecoder("deflate")
        out = bytearray(decoder.feed(payload))
        out.extend(decoder.finish())
        self.assertEqual(bytes(out), raw)

    def test_identity_is_inactive(self) -> None:
        """未压缩时 active 为 False 且 feed 原样返回,调用方据此跳过多余处理。"""
        decoder = IncrementalDecoder("")
        self.assertFalse(decoder.active)
        self.assertEqual(decoder.feed(b"abc"), b"abc")


class TestRequestHeaders(unittest.TestCase):
    """请求头是踩坑重灾区,用测试把关键约束钉死。"""

    def test_api_headers_carry_referer(self) -> None:
        """API 请求必须带 B 站 Referer,缺了会被接口侧直接拒绝。"""
        headers = api_headers()
        self.assertEqual(headers["Referer"], "https://www.bilibili.com/")

    def test_api_headers_may_declare_gzip(self) -> None:
        """默认声明 gzip/deflate 以省流量,显式关闭压缩时不能残留这个头。"""
        self.assertEqual(api_headers()["Accept-Encoding"], "gzip, deflate")
        self.assertNotIn("Accept-Encoding", api_headers(with_compression=False))

    def test_media_headers_keep_referer(self) -> None:
        """CDN 防盗链:缺 Referer 直接 403。"""
        self.assertEqual(media_headers()["Referer"], "https://www.bilibili.com/")

    def test_media_headers_drop_origin(self) -> None:
        """带 Origin 会让 CDN 按 CORS 处理并可能拒绝。"""
        self.assertNotIn("Origin", media_headers())

    def test_media_headers_skip_compression(self) -> None:
        """音视频已压缩,不该再声明 Accept-Encoding。"""
        self.assertNotIn("Accept-Encoding", media_headers())


if __name__ == "__main__":
    unittest.main(verbosity=2)
