"""音源快路径的单元测试:缓存索引 / 盲查与"命中就不发请求"。

不触网:缓存与接口客户端都用替身。钉住四件事:

1. 命中缓存时**一个网络请求都不发** —— 用"一被调用就断言失败"的客户端替身证明,
   这比断言"请求次数为 0"更硬:真发了请求会直接把用例炸红。
2. **索引优先、盲查兜底** —— 索引能给出真实档位与 codec(含非常规档位),
   它帮不上忙时才按常见档位从高到低试。
3. **指定了音质档位时不走快路径** —— 那种情况下连 codec 都要等接口返回才知道,
   拼不出缓存键,只能老老实实请求。
4. 成功出口会把这一首**写进缓存索引**(本地缓存页靠它枚举)。
"""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.audio.resolver import (  # noqa: E402
    KNOWN_QUALITIES,
    AudioResolver,
    CachedHit,
    pick_best_cached,
)
from bilibili_music.core.models import AudioTrack, Page, Video  # noqa: E402

#: 盲查只认这几个 (档位, codec) 组合,断言的期望顺序就是"从高到低"。
_CODEC = "mp4a.40.2"


def _video(bvid: str = "BVTEST", *, pages: int = 2) -> Video:
    """造一个**已带分P**的视频:这样 resolve 直接走到查缓存那一步,不请求详情。"""
    return Video(
        bvid=bvid,
        title=f"视频{bvid}",
        author="某UP",
        cid=1000,
        pages=[
            Page(index=i + 1, cid=1000 + i, title=f"第{i + 1}首", duration=180)
            for i in range(pages)
        ],
    )


class _FakeCache:
    """缓存替身:实现 ``indexed_hit`` / ``lookup`` / ``remember`` 三件事。

    ``indexed_hit`` 默认什么都不返回,于是大多数用例走的仍是盲查那条路 ——
    与"索引缺失"时真缓存的行为一致。
    """

    def __init__(
        self, available: dict[tuple[int, str], Path] | None = None
    ) -> None:
        """记录"哪些键命中",并准备收集查询记录。

        Args:
            available: 键为 ``(档位, codec)``、值为命中路径的映射;空表示全部未命中。
        """
        self.available = dict(available or {})
        self.queries: list[tuple[str, int, int, str]] = []
        #: 索引查询记录 ``(bvid, cid)``
        self.index_queries: list[tuple[str, int]] = []
        #: 写索引记录 ``(bvid, cid, 档位, 路径)``
        self.remembered: list[tuple[str, int, int, Path]] = []
        #: 预设的索引命中 ``((档位, codec, 码率), 路径)``;``None`` 表示索引里没有
        self.indexed: tuple[tuple[int, str, int], Path] | None = None

    def indexed_hit(self, bvid: str, cid: int):
        """记录一次索引查询,并按预设返回结果。"""
        self.index_queries.append((bvid, cid))
        if self.indexed is None:
            return None
        (quality_id, codec, bandwidth), path = self.indexed
        return (
            _FakeEntry(bvid, cid, quality_id, codec, bandwidth),
            path,
        )

    def lookup(
        self, bvid: str, cid: int, quality_id: int, codec: str = ""
    ) -> Path | None:
        """记录一次查询并按预设返回结果。

        Returns:
            预设的命中路径;该键不在 ``available`` 里时返回 ``None``。
        """
        self.queries.append((bvid, cid, quality_id, codec))
        return self.available.get((quality_id, codec))

    def remember(self, video: Video, page: Page, track: AudioTrack, path: Path) -> bool:
        """记录一次"写索引"。"""
        self.remembered.append((video.bvid, page.cid, track.quality_id, path))
        return True


class _FakeEntry:
    """索引命中结果的替身:解析器只读它的这几个字段。"""

    def __init__(
        self, bvid: str, cid: int, quality_id: int, codec: str, bandwidth: int
    ) -> None:
        """按字段建一条假记录。"""
        self.bvid = bvid
        self.cid = cid
        self.quality_id = quality_id
        self.codec = codec
        self.bandwidth = bandwidth


class _NoRequestClient:
    """客户端替身:**任何**请求都让用例失败,用来证明快路径真的没发请求。"""

    def fetch_video(
        self,
        bvid: str,
        *,
        on_success: Callable[[Video], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """缓存命中时不该补详情。"""
        raise AssertionError("缓存命中时不该请求详情接口")

    def fetch_audio_tracks(
        self,
        bvid: str,
        cid: int,
        *,
        on_success: Callable[[list[AudioTrack]], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """缓存命中时不该请求 playurl。"""
        raise AssertionError("缓存命中时不该请求 playurl 接口")


class _StubClient:
    """客户端替身:记录 playurl 请求,并立刻回调失败以结束流程。"""

    def __init__(self) -> None:
        """建一个没有请求记录的替身。"""
        self.tracks_calls: list[tuple[str, int]] = []

    def fetch_audio_tracks(
        self,
        bvid: str,
        cid: int,
        *,
        on_success: Callable[[list[AudioTrack]], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        """记录请求并立即以失败收尾 —— 用例只关心"到底有没有发请求"。"""
        self.tracks_calls.append((bvid, cid))
        on_error(RuntimeError("到此为止"))


class TestPickBestCached(unittest.TestCase):
    """盲查的优先级与返回内容。"""

    def setUp(self) -> None:
        """准备一个两分P的视频,以及它第 1P 的对象。"""
        self.video = _video()
        self.page = self.video.page(1)
        assert self.page is not None

    def test_prefers_the_highest_quality(self) -> None:
        """只缓存了中档时,要命中中档而不是当成未命中。"""
        cache = _FakeCache({(30232, _CODEC): Path("C:/fake/mid.m4a")})
        hit = pick_best_cached(cache, self.video, self.page)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.quality_id, 30232)
        self.assertEqual(hit.path, Path("C:/fake/mid.m4a"))

    def test_stops_at_the_first_hit(self) -> None:
        """命中之后不该继续往下试 —— 每一次 lookup 都是一次磁盘 stat。"""
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/high.m4a")})
        pick_best_cached(cache, self.video, self.page)
        self.assertEqual(len(cache.queries), 1)
        self.assertEqual(cache.queries[0][2], 30280)

    def test_queries_all_known_qualities_in_descending_order(self) -> None:
        """全部未命中时要按"从高到低"试完所有已知档位,顺序不能乱。"""
        cache = _FakeCache()
        self.assertIsNone(pick_best_cached(cache, self.video, self.page))
        self.assertEqual(
            [(quality_id, codec) for _, _, quality_id, codec in cache.queries],
            list(KNOWN_QUALITIES),
        )

    def test_hit_carries_codec_for_rebuilding_the_track(self) -> None:
        """命中结果必须带 codec:上层要据此重建 AudioTrack 才能在界面显示音质。"""
        cache = _FakeCache({(30216, _CODEC): Path("C:/fake/low.m4a")})
        hit = pick_best_cached(cache, self.video, self.page)
        self.assertEqual(
            hit,
            CachedHit(quality_id=30216, codec=_CODEC, path=Path("C:/fake/low.m4a")),
        )

    def test_index_wins_over_the_blind_lookup(self) -> None:
        """索引里有的就按索引来:它记的是**真实**档位与 codec,比猜的准。

        这一条同时钉住"命中索引后不再盲查" —— 每一次 lookup 都是一次磁盘 stat。
        """
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/high.m4a")})
        cache.indexed = ((30232, "fLaC", 1411200), Path("C:/fake/indexed.m4a"))
        hit = pick_best_cached(cache, self.video, self.page)
        assert hit is not None
        self.assertEqual(hit.quality_id, 30232)
        self.assertEqual(hit.codec, "fLaC")
        self.assertEqual(hit.path, Path("C:/fake/indexed.m4a"))
        self.assertEqual(hit.bandwidth, 1411200)
        self.assertEqual(cache.queries, [])  # 一次盲查都没做

    def test_index_lookup_uses_the_pages_own_cid(self) -> None:
        """索引查询同样要带对应分P的 cid(多P合集每个分P是一首独立的歌)。"""
        video = _video(pages=3)
        cache = _FakeCache()
        page3 = video.page(3)
        assert page3 is not None
        pick_best_cached(cache, video, page3)
        self.assertEqual(cache.index_queries[0], (video.bvid, page3.cid))

    def test_missing_index_falls_back_to_the_blind_lookup(self) -> None:
        """索引里没有(或读不出来)时必须退回盲查,不能因此当成未命中。"""
        cache = _FakeCache({(30216, _CODEC): Path("C:/fake/low.m4a")})
        hit = pick_best_cached(cache, self.video, self.page)
        assert hit is not None
        self.assertEqual(hit.quality_id, 30216)
        self.assertEqual(hit.bandwidth, 0)  # 盲查拿不到真实码率
        self.assertEqual(len(cache.queries), 3)  # 三个档位都试过


class TestResolverCachePreflight(unittest.TestCase):
    """解析器在请求 playurl 之前的盲查。"""

    def _resolve(
        self, resolver: AudioResolver, video: Video, **kwargs: object
    ) -> tuple[list, list]:
        """跑一次解析,返回 ``(成功结果列表, 失败异常列表)``。"""
        ok: list = []
        failed: list = []
        resolver.resolve(video, on_success=ok.append, on_error=failed.append, **kwargs)  # type: ignore[arg-type]
        return ok, failed

    def test_cache_hit_sends_no_request_at_all(self) -> None:
        """命中缓存时一个请求都不该发(客户端替身被调用就会让用例失败)。"""
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/hit.m4a")})
        resolver = AudioResolver(_NoRequestClient(), cache)  # type: ignore[arg-type]
        ok, failed = self._resolve(resolver, _video())
        self.assertEqual(failed, [])
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0].path, Path("C:/fake/hit.m4a"))

    def test_cache_hit_rebuilds_track_with_nominal_quality(self) -> None:
        """盲查命中时拿不到接口响应,音轨只能按档位取标称码率。"""
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/hit.m4a")})
        resolver = AudioResolver(_NoRequestClient(), cache)  # type: ignore[arg-type]
        ok, _ = self._resolve(resolver, _video())
        self.assertEqual(ok[0].track.quality_id, 30280)
        self.assertEqual(ok[0].track.codec, _CODEC)
        self.assertEqual(ok[0].track.kbps, 192)

    def test_index_hit_uses_the_real_bandwidth(self) -> None:
        """索引里记着上次接口给的真实码率:界面据此显示的是真值,不是标称值。"""
        cache = _FakeCache()
        cache.indexed = ((30280, _CODEC, 191_900), Path("C:/fake/hit.m4a"))
        resolver = AudioResolver(_NoRequestClient(), cache)  # type: ignore[arg-type]
        ok, _ = self._resolve(resolver, _video())
        self.assertEqual(ok[0].track.bandwidth, 191_900)
        self.assertEqual(ok[0].track.kbps, 192)

    def test_success_records_the_track_into_the_index(self) -> None:
        """成功出口要把这一首写进缓存索引:否则"本地缓存"页永远枚举不到它。"""
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/hit.m4a")})
        resolver = AudioResolver(_NoRequestClient(), cache)  # type: ignore[arg-type]
        video = _video()
        self._resolve(resolver, video, page_index=2)
        self.assertEqual(
            cache.remembered,
            [(video.bvid, 1001, 30280, Path("C:/fake/hit.m4a"))],
        )

    def test_failure_does_not_touch_the_index(self) -> None:
        """失败不该留下记录:索引指向一个不存在的文件,本地缓存页就会多一首点不开的歌。"""
        cache = _FakeCache()
        resolver = AudioResolver(_StubClient(), cache)  # type: ignore[arg-type]
        self._resolve(resolver, _video())
        self.assertEqual(cache.remembered, [])

    def test_cache_hit_leaves_resolver_idle(self) -> None:
        """快路径走完必须把状态清干净,否则后续 resolve 会被当成"还在忙"。"""
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/hit.m4a")})
        resolver = AudioResolver(_NoRequestClient(), cache)  # type: ignore[arg-type]
        self._resolve(resolver, _video())
        self.assertFalse(resolver.busy)

    def test_second_page_uses_its_own_cache_key(self) -> None:
        """多P合集里每个分P是一首独立的歌,盲查必须带对应分P的 cid。"""
        video = _video(pages=2)
        page2 = video.page(2)
        assert page2 is not None
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/p2.m4a")})
        resolver = AudioResolver(_NoRequestClient(), cache)  # type: ignore[arg-type]
        self._resolve(resolver, video, page_index=2)
        self.assertEqual(cache.queries[0][1], page2.cid)

    def test_pinned_quality_bypasses_the_preflight(self) -> None:
        """指定了档位就不能走快路径:codec 未知,拼不出缓存键,只能去请求。"""
        cache = _FakeCache({(30280, _CODEC): Path("C:/fake/hit.m4a")})
        client = _StubClient()
        resolver = AudioResolver(client, cache)  # type: ignore[arg-type]
        ok, failed = self._resolve(resolver, _video(), quality_id=30280)
        self.assertEqual(ok, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(client.tracks_calls), 1)
        self.assertEqual(cache.queries, [])  # 一次盲查都没有做

    def test_cache_miss_falls_through_to_the_network(self) -> None:
        """没命中缓存时照旧请求 playurl,快路径不能把正常流程挡掉。"""
        cache = _FakeCache()
        client = _StubClient()
        resolver = AudioResolver(client, cache)  # type: ignore[arg-type]
        _, failed = self._resolve(resolver, _video())
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(client.tracks_calls), 1)
        self.assertEqual(len(cache.queries), len(KNOWN_QUALITIES))


if __name__ == "__main__":
    unittest.main()
