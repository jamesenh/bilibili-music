"""缓存索引的单元测试:记录读写、容错、与音频文件的一致性。

不触网、不碰 Qt。落盘类用例把目录指向 ``tests/_scratch/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()``(它在 DSH 沙箱里会因 ``chmod`` 被拒;
见 ``AGENTS.md`` 第 7.1 节)。

钉住五件事:

1. 索引记录存在 sqlite 库里(``config`` 目录下的 ``library.db``),**不是**缓存目录里的
   JSON 文件;旧版 ``index.json`` 在建库成功后会被删掉;
2. 库被手改坏(缺字段、类型不对、``file_name`` 带路径穿越)时能救多少救多少,
   一条都不能把整次读取炸掉;
3. **重复记同一首不重复写库**,而且首次缓存时间不会被刷新(否则"缓存时间"永远显示成今天);
4. 索引与磁盘**对得上**:文件被手删过就剪掉记录,删不掉的缓存不许把记录一起抹掉;
5. 索引记录能还原成可播放的 ``Video``(离线点播的前提)。
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.audio.resolver import pick_best_cached  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.cache_index import (  # noqa: E402
    LEGACY_INDEX_FILE_NAME,
    CachedTrack,
    CacheIndex,
    entry_for,
    video_from_entry,
)
from bilibili_music.core.library_db import DB_FILE_NAME, SCHEMA_VERSION, LibraryDb  # noqa: E402
from bilibili_music.core.models import (  # noqa: E402
    AudioTrack,
    Page,
    Video,
    format_size,
    track_title,
)

#: 落盘类用例的临时目录根;每个用例用自己的子目录,互不干扰。
_SCRATCH_ROOT = Path(__file__).resolve().parent / "_scratch"

#: 造坏数据用的原始 SQL:绕过 dataclass 直接往表里塞一行(模拟用户拿 sqlite 工具手改)。
_INSERT_RAW = (
    "INSERT INTO cached_tracks (file_name, bvid, cid, quality_id, codec, bandwidth, "
    "title, author, page_index, page_title, multipart, duration, cover_url, size_bytes, "
    "cached_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


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

    def _db(self) -> LibraryDb:
        """在沙箱目录里建一个本地库(不连也行 —— ``LibraryDb`` 是惰性的)。"""
        return LibraryDb(self.tmp / DB_FILE_NAME)

    def _index(self) -> CacheIndex:
        """在沙箱目录里建一个索引。"""
        return CacheIndex(self._db())


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

    def test_single_page_video_is_not_marked_multipart(self) -> None:
        """单P视频不该被标成合集:否则副标题会多出一个没有信息量的 ``P1``。"""
        video = Video(
            bvid="BV1", title="单曲", author="UP", cid=11, pages=[Page(1, 11, "单曲", 180)]
        )
        page = video.first_page()
        assert page is not None
        track = AudioTrack(quality_id=30280, codec="mp4a.40.2", bandwidth=191_900, url="")
        entry = entry_for(video, page, track, Path("abc.m4a"))
        self.assertFalse(entry.multipart)
        self.assertEqual(entry.title, "单曲")
        self.assertEqual(entry.title, track_title(video, page))


class TestVideoFromEntry(unittest.TestCase):
    """记录还原成可播放模型(离线点播的前提)。"""

    def test_keeps_the_cached_page_and_its_cid(self) -> None:
        """还原出来的视频必须只含被缓存的那一P,而且 cid 是该P自己的。

        用视频级 cid(第 1P)会串歌 —— 这是音乐区多P合集的领域铁律。
        """
        entry = _entry(cid=222, page_index=3, title="第三首")
        video = video_from_entry(entry)
        self.assertEqual(video.bvid, entry.bvid)
        self.assertEqual(video.cid, 222)
        self.assertEqual(len(video.pages), 1)
        page = video.page(3)
        assert page is not None
        self.assertEqual(page.cid, 222)
        self.assertEqual(video.duration, entry.duration)

    def test_missing_title_falls_back_to_the_bvid(self) -> None:
        """标题为空时退回 bvid:列表里出现一行空白比显示编号更糟。"""
        video = video_from_entry(_entry(title=""))
        self.assertEqual(video.title, "BV1")


class TestLegacyIndexFile(_ScratchCase):
    """旧版 ``index.json`` 的处理(决定:不迁移,直接删)。"""

    def test_legacy_file_is_removed_once_the_db_works(self) -> None:
        """库建好之后旧索引就该消失,否则用户目录里会留一个永远不再更新的陌生文件。"""
        legacy = self.tmp / LEGACY_INDEX_FILE_NAME
        legacy.write_text('{"version": 1, "tracks": []}', encoding="utf-8")
        (self.tmp / f"{LEGACY_INDEX_FILE_NAME}.part").write_text("x", encoding="utf-8")
        AudioCache(self.tmp, db=self._db())
        self.assertFalse(legacy.exists())
        self.assertEqual(list(self.tmp.glob("*.part")), [])

    def test_legacy_file_survives_when_the_db_cannot_be_opened(self) -> None:
        """库建不起来时**不能**删旧索引:删了而新表没建成,用户就凭空少一份可用元数据。"""
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        cache_dir = self.tmp / "cache"
        cache_dir.mkdir()
        legacy = cache_dir / LEGACY_INDEX_FILE_NAME
        legacy.write_text('{"version": 1, "tracks": []}', encoding="utf-8")
        cache = AudioCache(cache_dir, db=LibraryDb(blocker / "sub" / DB_FILE_NAME))
        self.assertFalse(cache.db.available)
        self.assertTrue(legacy.exists())


class TestCacheIndexReadWrite(_ScratchCase):
    """索引的读写、排序与"不重复写库"。"""

    def test_missing_database_reads_as_empty(self) -> None:
        """库还从没建过(从没缓存过)时要当空索引,不能抛异常。"""
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

        顺序取**写入顺序倒序**(表里的 ``rowid``):与 ``cached_at`` 无关 —— 同一批下载的
        时间戳可能完全相同(Windows 的 ``time.time()`` 分辨率约 15.6 ms),靠它排序会变成
        不确定的。
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
        """内容没变就不写库,并且**首次缓存时间不被刷新**。"""
        index = self._index()
        index.remember(_entry(cached_at=1000.0))
        # 第二次带上"现在"的时间戳,模拟"同一首歌又播了一遍"
        self.assertFalse(index.remember(_entry(cached_at=9999.0)))
        self.assertEqual(index.entries()[0].cached_at, 1000.0)

    def test_changed_content_is_written_and_keeps_cached_at(self) -> None:
        """内容真的变了要写库,但缓存时间仍然保留第一次的。"""
        index = self._index()
        index.remember(_entry(cached_at=1000.0, size_bytes=1))
        self.assertTrue(index.remember(_entry(cached_at=9999.0, size_bytes=2)))
        self.assertEqual(index.entries()[0].size_bytes, 2)
        self.assertEqual(index.entries()[0].cached_at, 1000.0)

    def test_zero_cached_at_is_filled_with_now(self) -> None:
        """调用方没给时间戳时补当前时间(否则"缓存时间"一栏会显示成 1970 年)。"""
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
        self.assertEqual(index.retain_files(["a.m4a"]), 0)  # 没变化

    def test_retain_files_with_nothing_alive_clears_the_index(self) -> None:
        """磁盘上一首都没有时(比如整个缓存目录被删掉),索引也该是空的。"""
        index = self._index()
        index.remember(_entry(cid=1))
        self.assertEqual(index.retain_files([]), 1)
        self.assertEqual(index.entries(), ())

    def test_clear_empties_the_index(self) -> None:
        """清空返回被清掉的条数,再清一次是 0。"""
        index = self._index()
        index.remember(_entry(cid=1))
        index.remember(_entry(cid=2))
        self.assertEqual(index.clear(), 2)
        self.assertEqual(index.entries(), ())
        self.assertEqual(index.clear(), 0)

    def test_reload_sees_changes_made_by_another_instance(self) -> None:
        """``reload`` 的语义是"重新对一次账":别的进程动过库时能看到新内容。"""
        index = self._index()
        index.remember(_entry(cid=1))
        # 另起一个实例写库,模拟"别的进程动了索引"
        CacheIndex(self._db()).remember(_entry(cid=2))
        self.assertEqual(len(index.entries()), 2)
        self.assertEqual(len(index.reload()), 2)

    def test_schema_version_is_recorded(self) -> None:
        """库结构要带版本号:以后真要改表结构时能一眼看出它是哪一代建的。"""
        db = self._db()
        CacheIndex(db).remember(_entry())
        rows = db.query("PRAGMA user_version")
        self.assertEqual(int(rows[0][0]), SCHEMA_VERSION)

    def test_write_failure_is_swallowed(self) -> None:
        """库建不起来(目录不可写 / 磁盘满)时只当这次记录作废,绝不抛异常。

        调用方是播放解析流程 —— 为一份索引把一次播放搞崩是本末倒置。
        """
        # 把一个**文件**当路径的一部分,开库必然失败
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        index = CacheIndex(LibraryDb(blocker / "sub" / DB_FILE_NAME))
        self.assertFalse(index.remember(_entry()))
        self.assertEqual(index.entries(), ())
        self.assertEqual(index.clear(), 0)


class TestCacheIndexTolerance(_ScratchCase):
    """读取容错:坏数据能救多少救多少,一条都不能把整次读取炸掉。"""

    def _insert(
        self,
        file_name: object,
        bvid: object,
        cid: object,
        *,
        quality_id: object = 30280,
        duration: object = 180,
        size_bytes: object = 1024,
        page_index: object = 1,
        multipart: object = 0,
        cached_at: object = 1.0,
    ) -> None:
        """绕过记录类,直接往表里塞一行(模拟用户拿 sqlite 工具手改库)。"""
        self._db().execute(
            _INSERT_RAW,
            (
                file_name,
                bvid,
                cid,
                quality_id,
                "mp4a.40.2",
                191_900,
                "手改的记录",
                "某UP",
                page_index,
                "手改的记录",
                multipart,
                duration,
                "",
                size_bytes,
                cached_at,
            ),
        )

    def test_bad_rows_are_skipped_one_by_one(self) -> None:
        """坏行单条跳过,其余照常读出来。"""
        self._insert("no_bvid.m4a", "", 1)
        self._insert("no_cid.m4a", "BV1", 0)
        self._insert("weird_cid.m4a", "BV1", "not-a-number")
        index = self._index()
        self.assertTrue(index.remember(_entry(file_name="good.m4a")))
        self.assertEqual([e.file_name for e in index.entries()], ["good.m4a"])

    def test_path_traversal_in_file_name_is_rejected(self) -> None:
        """``file_name`` 只认纯文件名:否则一次"删除缓存"会删到缓存目录之外。

        库文件在用户目录里、可以被手改,所以这条防线必须在**读进来**的时候就设好。
        """
        self._insert("../outside.m4a", "BV1", 1)
        self._insert("sub/dir.m4a", "BV1", 2)
        self._insert(r"..\..\outside.m4a", "BV1", 3)
        self._insert("..", "BV1", 4)
        self.assertEqual(self._index().entries(), ())

    def test_wrong_types_are_coerced(self) -> None:
        """类型不对时收敛到合法值,而不是丢弃整条记录。"""
        self._insert(
            "x.m4a",
            "BV1",
            "12",
            quality_id="oops",
            duration=-5,
            size_bytes="abc",
            page_index=0,
            multipart=1,
            cached_at="oops",
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
        cache = AudioCache(self.tmp, db=self._db())
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

    def test_audio_files_and_index_live_in_different_directories(self) -> None:
        """音频在缓存目录、索引在配置目录:缓存目录被整个删掉时历史与索引都不受影响。"""
        cache = AudioCache(self.tmp / "cache", db=self._db())
        self.assertEqual(cache.index.db, cache.db)
        self.assertNotEqual(cache.db.path.parent, cache.root)
        self.assertEqual(cache.db.path.parent, self.tmp)


if __name__ == "__main__":
    unittest.main()
