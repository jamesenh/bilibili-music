"""缓存索引的单元测试:记录读写、容错、与音频文件的一致性。

不触网、不碰 Qt。落盘类用例把目录指向 ``tests/_scratch/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒;
见 ``AGENTS.md`` 第 7.1 节)。

钉住四件事:

1. 索引文件是**可再生的**:坏 JSON、缺字段、被手改过的路径穿越记录都不能让读取抛异常,
   能救多少救多少;
2. **重复记同一首不重复落盘**,而且首次缓存时间不会被刷新(否则"缓存时间"永远显示成今天);
3. 索引与磁盘**对得上**:文件被手删过就剪掉记录,删不掉的缓存不许把记录一起抹掉;
4. 索引记录能还原成可播放的 ``Video``(离线点播的前提)。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.audio.resolver import pick_best_cached  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.cache_index import (  # noqa: E402
    INDEX_FILE_NAME,
    CachedTrack,
    CacheIndex,
    entry_for,
    video_from_entry,
)
from bilibili_music.core.models import (  # noqa: E402
    AudioTrack,
    Page,
    Video,
    format_size,
    track_title,
)

#: 落盘类用例的临时目录根;每个用例用自己的子目录,互不干扰。
_SCRATCH_ROOT = Path(__file__).resolve().parent / "_scratch"


def _entry(
    bvid: str = "BV1",
    cid: int = 100,
    *,
    file_name: str | None = None,
    quality_id: int = 30280,
    bandwidth: int = 191_900,
    title: str = "第一首",
    author: str = "某UP",
    page_index: int = 1,
    multipart: bool = True,
    duration: int = 180,
    size_bytes: int = 2048,
    cached_at: float = 1000.0,
) -> CachedTrack:
    """造一条索引记录(默认参数凑成"多P合集里的第二首"那种常见形态)。"""
    return CachedTrack(
        file_name=file_name or f"{bvid}_{cid}_{quality_id}.m4a",
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
        cached_at=cached_at,
    )


class _ScratchCase(unittest.TestCase):
    """给需要落盘的用例准备一个干净目录(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建空目录并把 ``self.tmp`` 指向它。"""
        self.tmp = _SCRATCH_ROOT / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """删掉本用例的目录(清理失败不让用例变红)。"""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _index(self) -> CacheIndex:
        """在沙箱目录里建一个索引。"""
        return CacheIndex(self.tmp)


class TestFormatSize(unittest.TestCase):
    """体积格式化(纯函数)。"""

    def test_units_step_by_1024(self) -> None:
        """四档单位的分界与小数位:KB 取整、MB 一位、GB 两位。"""
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(900), "900 B")
        self.assertEqual(format_size(1024), "1 KB")
        self.assertEqual(format_size(1024 * 812), "812 KB")
        self.assertEqual(format_size(int(1024**2 * 3.4)), "3.4 MB")
        self.assertEqual(format_size(int(1024**3 * 1.25)), "1.25 GB")

    def test_negative_is_clamped(self) -> None:
        """负数按 0 处理(接口/手改数据可能给负值,界面上不该出现 -5 B)。"""
        self.assertEqual(format_size(-1), "0 B")


class TestEntryFor(_ScratchCase):
    """从解析结果造记录(纯函数 + 领域铁律)。"""

    def test_uses_the_pages_own_title_and_duration(self) -> None:
        """多P合集必须用**分P自己**的标题与时长,不能用视频级的(那是所有分P之和)。"""
        video = Video(
            bvid="BV1",
            title="合集",
            author="UP",
            duration=9999,
            cid=11,
            pages=[Page(1, 11, "第一首", 180), Page(2, 22, "第二首", 200)],
        )
        page2 = video.page(2)
        assert page2 is not None
        track = AudioTrack(quality_id=30232, codec="mp4a.40.2", bandwidth=132_000, url="")
        entry = entry_for(video, page2, track, Path("C:/cache/abc.m4a"), size_bytes=1234)
        self.assertEqual(entry.file_name, "abc.m4a")
        self.assertEqual(entry.cid, 22)
        self.assertEqual(entry.page_index, 2)
        self.assertEqual(entry.title, "第二首")
        self.assertEqual(entry.duration, 200)
        self.assertEqual(entry.bandwidth, 132_000)
        self.assertTrue(entry.multipart)
        self.assertEqual(entry.size_bytes, 1234)

    def test_single_page_video_is_not_marked_multipart(self) -> None:
        """单P视频不该带 ``multipart``:界面据此决定副标题要不要显示 ``P1``。"""
        video = Video(
            bvid="BV2", title="单曲", author="UP", cid=33, pages=[Page(1, 33, "", 180)]
        )
        page = video.first_page()
        assert page is not None
        track = AudioTrack(quality_id=30216, codec="mp4a.40.5", bandwidth=64_000, url="")
        entry = entry_for(video, page, track, Path("C:/cache/x.m4a"))
        self.assertFalse(entry.multipart)
        self.assertEqual(entry.title, "单曲")  # 分P标题是空串时退回视频标题


class TestVideoFromEntry(unittest.TestCase):
    """索引记录还原成可播放的 ``Video``(离线点播的前提)。"""

    def test_keeps_the_cached_page_and_its_cid(self) -> None:
        """还原出来的 ``Video`` 只能有被缓存的那一P,而且 cid / 序号都要对上。

        有了 ``pages`` 解析器就不会再请求详情,所以这一步正确与否直接决定"断网能不能播";
        cid 错了则会串到同合集的另一首上(领域铁律)。
        """
        entry = _entry(bvid="BV9", cid=77, page_index=3, title="第三首", duration=210)
        video = video_from_entry(entry)
        self.assertEqual(video.bvid, "BV9")
        self.assertEqual(video.cid, 77)
        self.assertEqual(len(video.pages), 1)
        page = video.page(3)
        assert page is not None
        self.assertEqual(page.cid, 77)
        self.assertEqual(page.duration, 210)
        # 展示名保持不变:记录里存的就是当时算好的名字
        self.assertEqual(track_title(video, page), "第三首")
        self.assertEqual(video.cover_url, entry.cover_url)

    def test_missing_title_falls_back_to_the_bvid(self) -> None:
        """标题缺失时用 bvid 顶上:界面上宁可显示编号,也不要一行空白。"""
        entry = _entry(bvid="BVEMPTY", title="")
        self.assertEqual(video_from_entry(entry).title, "BVEMPTY")


class TestCacheIndexReadWrite(_ScratchCase):
    """索引的读写、排序与"不重复落盘"。"""

    def test_missing_file_reads_as_empty(self) -> None:
        """索引还不存在(从没缓存过)时要当空索引,不能抛异常。"""
        self.assertEqual(self._index().entries(), ())
        self.assertIsNone(self._index().find("BV1", 100))

    def test_remember_then_entries(self) -> None:
        """记一笔再读回来,字段要原样保留。"""
        index = self._index()
        self.assertTrue(index.remember(_entry(title="晴天", size_bytes=4096)))
        entries = index.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "晴天")
        self.assertEqual(entries[0].size_bytes, 4096)
        self.assertEqual(entries[0].key, ("BV1", 100, 30280, "mp4a.40.2"))

    def test_entries_are_newest_first(self) -> None:
        """最近缓存的排在前面(界面直接按这个顺序显示)。

        顺序取**写入顺序倒序**:与 ``cached_at`` 无关 —— 同一批下载的时间戳可能完全相同
        (Windows 的 ``time.time()`` 分辨率约 15.6 ms),靠它排序会变成不确定的。
        """
        index = self._index()
        index.remember(_entry(bvid="BVOLD", cached_at=9999.0))
        index.remember(_entry(bvid="BVNEW", cached_at=1.0))
        self.assertEqual([e.bvid for e in index.entries()], ["BVNEW", "BVOLD"])

    def test_updating_an_entry_does_not_move_it_to_the_front(self) -> None:
        """更新已有条目(比如重新量了体积)不该让它看起来像"刚缓存的"。"""
        index = self._index()
        index.remember(_entry(bvid="BV1", size_bytes=1))
        index.remember(_entry(bvid="BV2", cid=2))
        index.remember(_entry(bvid="BV1", size_bytes=2))
        self.assertEqual([e.bvid for e in index.entries()], ["BV2", "BV1"])

    def test_remembering_the_same_track_twice_does_not_rewrite(self) -> None:
        """内容没变就不落盘,并且**首次缓存时间不被刷新**。"""
        index = self._index()
        index.remember(_entry(cached_at=1000.0))
        before = index.path.read_bytes()
        # 第二次带上"现在"的时间戳,模拟"同一首歌又播了一遍"
        self.assertFalse(index.remember(_entry(cached_at=9999.0)))
        self.assertEqual(index.entries()[0].cached_at, 1000.0)
        self.assertEqual(index.path.read_bytes(), before)

    def test_changed_content_is_written_and_keeps_cached_at(self) -> None:
        """内容真的变了要落盘,但缓存时间仍然保留第一次的。"""
        index = self._index()
        index.remember(_entry(cached_at=1000.0, size_bytes=1))
        self.assertTrue(index.remember(_entry(cached_at=9999.0, size_bytes=2)))
        self.assertEqual(index.entries()[0].size_bytes, 2)
        self.assertEqual(index.entries()[0].cached_at, 1000.0)

    def test_zero_cached_at_is_filled_with_now(self) -> None:
        """调用方没给时间戳时补当前时间(否则排序会把新歌排到最后)。"""
        index = self._index()
        index.remember(_entry(cached_at=0.0))
        self.assertGreater(index.entries()[0].cached_at, 0)

    def test_find_prefers_the_highest_bandwidth(self) -> None:
        """同一分P缓存过多个档位时取最好的那一份。"""
        index = self._index()
        index.remember(_entry(quality_id=30216, bandwidth=64_000, file_name="low.m4a"))
        index.remember(_entry(quality_id=30280, bandwidth=191_900, file_name="high.m4a"))
        found = index.find("BV1", 100)
        assert found is not None
        self.assertEqual(found.file_name, "high.m4a")

    def test_find_is_scoped_to_the_page(self) -> None:
        """``find`` 必须按 ``(bvid, cid)`` 找:多P合集里每个分P是一首独立的歌。"""
        index = self._index()
        index.remember(_entry(cid=100))
        self.assertIsNone(index.find("BV1", 200))
        self.assertIsNone(index.find("BVOTHER", 100))

    def test_forget_removes_only_that_entry(self) -> None:
        """删一条不影响另一条。"""
        index = self._index()
        index.remember(_entry(cid=1))
        index.remember(_entry(cid=2))
        self.assertTrue(index.forget(("BV1", 1, 30280, "mp4a.40.2")))
        self.assertEqual([e.cid for e in index.entries()], [2])
        self.assertFalse(index.forget(("BV1", 1, 30280, "mp4a.40.2")))

    def test_retain_files_drops_the_rest(self) -> None:
        """只保留给定文件名对应的记录,返回被删掉的条数。"""
        index = self._index()
        index.remember(_entry(cid=1, file_name="a.m4a"))
        index.remember(_entry(cid=2, file_name="b.m4a"))
        self.assertEqual(index.retain_files(["a.m4a"]), 1)
        self.assertEqual([e.file_name for e in index.entries()], ["a.m4a"])
        self.assertEqual(index.retain_files(["a.m4a"]), 0)  # 没变化就不落盘

    def test_clear_empties_the_index(self) -> None:
        """清空返回被清掉的条数,再清一次是 0。"""
        index = self._index()
        index.remember(_entry(cid=1))
        index.remember(_entry(cid=2))
        self.assertEqual(index.clear(), 2)
        self.assertEqual(index.entries(), ())
        self.assertEqual(index.clear(), 0)

    def test_reload_picks_up_outside_changes(self) -> None:
        """``reload`` 要重新读盘:用户在应用之外动过缓存目录时只有它能看见。"""
        index = self._index()
        index.remember(_entry(cid=1))
        # 另起一个实例改文件,模拟"别的进程动了索引"
        CacheIndex(self.tmp).remember(_entry(cid=2))
        self.assertEqual(len(index.entries()), 1)  # 内存副本还没跟上
        self.assertEqual(len(index.reload()), 2)

    def test_write_leaves_no_part_file(self) -> None:
        """原子写入不能留下 ``.part`` 残文件。"""
        index = self._index()
        index.remember(_entry())
        self.assertEqual(list(self.tmp.glob("*.part")), [])

    def test_saved_payload_carries_a_version(self) -> None:
        """索引文件要带格式版本号:以后真要改结构时能一眼看出它是哪一代写的。"""
        index = self._index()
        index.remember(_entry())
        payload = json.loads(index.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["tracks"]), 1)

    def test_write_failure_is_swallowed(self) -> None:
        """写不进去(磁盘满 / 无权限)时只当这次记录作废,绝不抛异常。

        调用方是播放解析流程 —— 为一份索引把一次播放搞崩是本末倒置。
        """
        # 把一个**文件**当成索引目录的父目录,写盘必然失败(而且失败后不能留下 .part)
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        index = CacheIndex(blocker / "sub")
        self.assertFalse(index.remember(_entry()))
        self.assertEqual(list(blocker.parent.glob("*.part")), [])


class TestCacheIndexTolerance(_ScratchCase):
    """读取容错:坏数据能救多少救多少,一条都不能把整次读取炸掉。"""

    def _write(self, payload: object) -> None:
        """把任意负载写进索引文件(可以是坏 JSON 之外的各种结构)。"""
        (self.tmp / INDEX_FILE_NAME).write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def test_broken_json_reads_as_empty(self) -> None:
        """写到一半被杀的 JSON 当空索引处理。"""
        self._write('{"tracks": [{"bvid":')
        self.assertEqual(self._index().entries(), ())

    def test_top_level_not_an_object_reads_as_empty(self) -> None:
        """顶层是数组(被手改过)当空索引处理。"""
        self._write([1, 2, 3])
        self.assertEqual(self._index().entries(), ())

    def test_bad_records_are_skipped_one_by_one(self) -> None:
        """坏记录单条跳过,其余照常读出来。"""
        self._write(
            {
                "version": 1,
                "tracks": [
                    "不是对象",
                    {"file_name": "no_bvid.m4a", "cid": 1},
                    {"file_name": "no_cid.m4a", "bvid": "BV1"},
                    {"bvid": "BV1", "cid": 5},
                    _good_record(),
                ],
            }
        )
        entries = self._index().entries()
        self.assertEqual([e.file_name for e in entries], ["good.m4a"])

    def test_path_traversal_in_file_name_is_rejected(self) -> None:
        """``file_name`` 只认纯文件名:否则一次"删除缓存"会删到缓存目录之外。

        索引文件在用户目录里、可以被手改,所以这条防线必须在**读进来**的时候就设好。
        """
        self._write(
            {
                "tracks": [
                    {"file_name": "../outside.m4a", "bvid": "BV1", "cid": 1},
                    {"file_name": "sub/dir.m4a", "bvid": "BV1", "cid": 2},
                    {"file_name": r"..\..\outside.m4a", "bvid": "BV1", "cid": 3},
                    {"file_name": "..", "bvid": "BV1", "cid": 4},
                ]
            }
        )
        self.assertEqual(self._index().entries(), ())

    def test_unknown_keys_are_ignored(self) -> None:
        """多出来的字段被忽略(向前兼容:旧版本读到新版本写的文件不该整份作废)。"""
        record = _good_record()
        record["future_field"] = "以后才有的东西"
        self._write({"version": 99, "tracks": [record]})
        entries = self._index().entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "旧记录")

    def test_wrong_types_are_coerced(self) -> None:
        """数字/字符串类型不对时收敛到合法值,而不是丢弃整条记录。"""
        self._write(
            {
                "tracks": [
                    {
                        "file_name": "x.m4a",
                        "bvid": "BV1",
                        "cid": "12",
                        "quality_id": None,
                        "duration": -5,
                        "size_bytes": "abc",
                        "page_index": 0,
                        "multipart": 1,
                        "cached_at": "oops",
                    }
                ]
            }
        )
        entry = self._index().entries()[0]
        self.assertEqual(entry.cid, 12)
        self.assertEqual(entry.quality_id, 0)
        self.assertEqual(entry.duration, 0)
        self.assertEqual(entry.size_bytes, 0)
        self.assertEqual(entry.page_index, 1)
        self.assertTrue(entry.multipart)
        self.assertEqual(entry.cached_at, 0.0)

    def test_remember_rejects_an_illegal_file_name(self) -> None:
        """写进去的也得是纯文件名,免得自己造出一条读不回来的记录。"""
        index = self._index()
        self.assertFalse(index.remember(_entry(file_name="../bad.m4a")))
        self.assertEqual(index.entries(), ())


def _good_record() -> dict[str, object]:
    """一条结构完整、可以直接读出来的记录。"""
    return {
        "file_name": "good.m4a",
        "bvid": "BVOK",
        "cid": 7,
        "quality_id": 30280,
        "codec": "mp4a.40.2",
        "bandwidth": 191_900,
        "title": "旧记录",
        "author": "某UP",
        "page_index": 1,
        "page_title": "旧记录",
        "multipart": False,
        "duration": 180,
        "cover_url": "",
        "size_bytes": 1024,
        "cached_at": 1.0,
    }


class TestAudioCacheIndex(_ScratchCase):
    """缓存与索引的配合:命中、删除、剪枝、清空。"""

    def _cache_with_track(
        self,
        *,
        bvid: str = "BV1",
        cid: int = 100,
        quality_id: int = 30280,
        codec: str = "mp4a.40.2",
    ) -> tuple[AudioCache, Path]:
        """建一个缓存,并在里面放一条"文件 + 记录"都齐的缓存。

        Returns:
            ``(缓存, 音频文件路径)``。
        """
        cache = AudioCache(self.tmp)
        path = cache.path_for(bvid, cid, quality_id, codec)
        path.write_bytes(b"fake audio")
        video = Video(
            bvid=bvid,
            title="合集",
            author="UP",
            cid=cid,
            pages=[Page(1, cid, "第一首", 180), Page(2, cid + 1, "第二首", 200)],
        )
        page = video.page(1)
        assert page is not None
        track = AudioTrack(
            quality_id=quality_id, codec=codec, bandwidth=191_900, url=""
        )
        cache.remember(video, page, track, path)
        return cache, path

    def test_indexed_hit_returns_the_entry_and_path(self) -> None:
        """索引命中要同时给出记录与真实文件路径。"""
        cache, path = self._cache_with_track()
        hit = cache.indexed_hit("BV1", 100)
        assert hit is not None
        entry, found = hit
        self.assertEqual(entry.title, "第一首")
        self.assertEqual(found, path)

    def test_indexed_hit_ignores_a_file_deleted_by_hand(self) -> None:
        """索引里有、文件被手删了 —— 必须当成未命中,否则会去播一个不存在的文件。"""
        cache, path = self._cache_with_track()
        path.unlink()
        self.assertIsNone(cache.indexed_hit("BV1", 100))

    def test_indexed_hit_ignores_a_zero_byte_file(self) -> None:
        """0 字节文件不算命中(进程被杀可能留下半截文件,播它只会一直没声音)。"""
        cache, path = self._cache_with_track()
        path.write_bytes(b"")
        self.assertIsNone(cache.indexed_hit("BV1", 100))

    def test_pick_best_cached_uses_the_index(self) -> None:
        """整条快路径:索引命中时不必盲查,连非常规 codec 也能对上。"""
        cache, path = self._cache_with_track(quality_id=30232, codec="fLaC")
        video = Video(bvid="BV1", title="合集", cid=100, pages=[Page(1, 100, "第一首", 180)])
        page = video.first_page()
        assert page is not None
        hit = pick_best_cached(cache, video, page)
        assert hit is not None
        self.assertEqual(hit.path, path)
        self.assertEqual(hit.codec, "fLaC")
        self.assertEqual(hit.bandwidth, 191_900)

    def test_forget_deletes_file_and_entry(self) -> None:
        """删一条缓存:文件与记录都要没。"""
        cache, path = self._cache_with_track()
        entry = cache.index.entries()[0]
        self.assertTrue(cache.forget(entry))
        self.assertFalse(path.exists())
        self.assertEqual(cache.index.entries(), ())

    def test_forget_tolerates_an_already_missing_file(self) -> None:
        """文件本来就不在了:照样把记录清掉(否则界面上会留一条点了没反应的歌)。"""
        cache, path = self._cache_with_track()
        path.unlink()
        self.assertTrue(cache.forget(cache.index.entries()[0]))
        self.assertEqual(cache.index.entries(), ())

    def test_prune_drops_entries_whose_file_is_gone(self) -> None:
        """剪枝:文件被手删过的记录要清掉,返回清掉的条数。"""
        cache, path = self._cache_with_track()
        path.unlink()
        self.assertEqual(cache.prune(), 1)
        self.assertEqual(cache.index.entries(), ())
        self.assertEqual(cache.prune(), 0)

    def test_clear_removes_files_and_index_together(self) -> None:
        """清空缓存要连索引一起清:留下空记录会显示出一堆点不开的歌。"""
        cache, path = self._cache_with_track()
        self.assertEqual(cache.clear(), 1)
        self.assertFalse(path.exists())
        self.assertEqual(cache.index.entries(), ())
        self.assertEqual(cache.size_bytes(), 0)

    def test_index_shares_the_cache_directory(self) -> None:
        """索引与音频文件必须同目录,否则注入沙箱目录时容易只注一个。"""
        cache = AudioCache(self.tmp)
        self.assertEqual(cache.index.root, cache.root)
        self.assertEqual(cache.index.path, self.tmp / INDEX_FILE_NAME)


if __name__ == "__main__":
    unittest.main()
