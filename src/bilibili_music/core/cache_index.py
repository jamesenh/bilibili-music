"""已缓存音频的索引(让缓存**可枚举**)。

为什么需要它
------------

``core/cache.py`` 的文件名是 ``(bvid, cid, 音质, codec)`` 的 SHA-1 摘要,只能"知道键
再问文件在不在"(盲查);反过来"磁盘上现在缓存了哪些歌"是拼不出来的 —— 而"本地缓存"页
要显示曲名、UP主、音质、体积,还要能离线点播,光有文件路径拿不到这些。

所以解析成功时顺手把这一条音轨的元数据写进索引。索引是**可再生的**:它只是"磁盘上的
一次快照",坏了、丢了都不影响播放 —— :func:`~bilibili_music.audio.resolver.pick_best_cached`
在索引缺失时会退回按档位盲查(见 ``AGENTS.md`` 第 1.2 节的主线不变)。

为什么从 ``index.json`` 换成 sqlite
-----------------------------------

索引原先是一份与音频文件同目录的 ``index.json``:每次增删都要**整份重写**。播放历史
(``core/history.py``)是"每开始播一首都写一条"的高频写入,再用同一套做法就得维护第二个
整天重写的文件。现在两者共用配置目录下的 ``library.db``(见 :mod:`.library_db`):

* 增删是一行 SQL,不再重写整份数据;
* 一个库两张表,想做"这首还在不在本地"这类跨表查询时不必再对两份文件;
* 崩溃安全交给 sqlite 自己的日志(WAL),原先那套"先 ``.part`` 再 ``os.replace``"的
  纪律不再需要。

**旧数据不迁移**(2026-09-14 的决定):索引文件保留的只是"磁盘快照",没有不可再生的
信息,所以 :func:`discard_legacy_index` 在库建好之后直接把它删掉。代价是**索引出现之前
缓存下来的非常规档位(如 ``fLaC``)会变成磁盘上的僵尸文件** —— 盲查只覆盖
:data:`~bilibili_music.audio.resolver.KNOWN_QUALITIES` 里的三档,那些歌要重新缓存一次
才会重新出现在"本地缓存"页里。

设计取舍
--------

* **字段容错读取**:sqlite 的列是动态类型的,用户可以拿任意 sqlite 工具手改这个库。
  缺关键字段、类型不对的单条记录一律跳过,**不做整份作废**。
* **``file_name`` 只接受纯文件名**:它会被用来拼路径(删除、查文件),只认裸文件名就能
  挡住 ``..\\..\\x.m4a`` 这类路径穿越 —— 否则一次"删除缓存"会删到缓存目录之外。
* **写入失败不抛异常**:索引只服务于界面展示与缓存快路径,库不可用时放弃这次更新即可。
  它的调用方是播放解析流程,**绝不能让写索引失败导致播不出来**。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .library_db import LibraryDb
from .models import AudioTrack, Page, PlayableEntry, Video, track_title

__all__ = [
    "CacheKey",
    "CachedTrack",
    "CacheIndex",
    "LEGACY_INDEX_FILE_NAME",
    "discard_legacy_index",
    "entry_for",
    "video_from_entry",
]

#: 旧版 JSON 索引的文件名。它只在"建库成功后删掉"这一处出现
#: (见 :func:`discard_legacy_index`)。
LEGACY_INDEX_FILE_NAME = "index.json"

#: 旧版索引写入用的临时后缀(与 ``update``/``lookup`` 同一套纪律的残留),一并清掉。
_LEGACY_TMP_SUFFIX = ".part"

#: 一条记录的身份:``(bvid, cid, 音质档位, codec)``,与音频文件的缓存键同构。
CacheKey = tuple[str, int, int, str]

#: 写入一条索引记录的 UPSERT 语句。
#:
#: 冲突目标必须是主键 ``file_name``:音频文件名由缓存键推导而来,同一个键永远对应同一个
#: 文件名,所以"同一个键重复记录"在这里天然收敛成一行。
#: ``DO UPDATE``(而不是删了再插)保住了 ``rowid``,而"最近缓存的排在前面"正是靠它。
_UPSERT = """
INSERT INTO cached_tracks
    (file_name, bvid, cid, quality_id, codec, bandwidth, title, author, page_index,
     page_title, multipart, duration, cover_url, size_bytes, cached_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(file_name) DO UPDATE SET
    bvid       = excluded.bvid,
    cid        = excluded.cid,
    quality_id = excluded.quality_id,
    codec      = excluded.codec,
    bandwidth  = excluded.bandwidth,
    title      = excluded.title,
    author     = excluded.author,
    page_index = excluded.page_index,
    page_title = excluded.page_title,
    multipart  = excluded.multipart,
    duration   = excluded.duration,
    cover_url  = excluded.cover_url,
    size_bytes = excluded.size_bytes,
    cached_at  = excluded.cached_at
"""


def _as_int(value: object, default: int = 0) -> int:
    """把不可信的值转成整数;转不了就用 ``default``。

    库文件可能被手改坏(sqlite 的列是动态类型的),直接 ``int()`` 会抛
    ``TypeError`` / ``ValueError`` 把整次读取炸掉,而这里的目标是"尽量救回来"。

    Args:
        value: 待转换的原始值。
        default: 转换失败时的兜底值。

    Returns:
        转换后的整数,或 ``default``。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    """把不可信的值转成浮点数;转不了就用 ``default``。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_str(value: object) -> str:
    """把不可信的值转成字符串;``None`` 变成空串。"""
    return "" if value is None else str(value)


def _is_plain_file_name(name: str) -> bool:
    """判断一个字符串是不是"纯文件名"(不含目录、盘符、上跳)。

    见模块 docstring:索引里的 ``file_name`` 会被用来拼路径(删除、查文件),而库文件
    本身是可被手改的,所以进内存前就得把 ``..`` / 目录分隔符挡在外面。

    Args:
        name: 待检查的字符串。

    Returns:
        是纯文件名返回 ``True``;空串、带目录或带盘符都返回 ``False``。
    """
    if not name or name in (".", ".."):
        return False
    # 反斜杠也得自己挡:它是 Windows 的目录分隔符,但 POSIX 上 ``Path`` 只认 ``/``,
    # 于是 ``Path(r"..\\..\\x.m4a").name`` 会原样返回整个串。库文件是可移植的
    # (会被同步、备份到别的机器),这条判据不能因平台而变松。
    if "\\" in name:
        return False
    return Path(name).name == name


def discard_legacy_index(root: Path) -> bool:
    """删掉缓存目录里的旧版 JSON 索引(不迁移,见模块 docstring)。

    调用点只有一个(``AudioCache.__init__``),而且必须**在建库成功之后**才调用:
    删了旧索引而新表没建成的话,用户就凭空少了一份还能用的元数据。

    删不掉(文件被占用、无权限)不算错误:留着它只是多个几百字节的陌生文件,不影响任何
    功能 —— 下一次启动还会再试一遍。

    Args:
        root: 缓存根目录(旧索引与音频文件同目录)。

    Returns:
        真的删掉了 ``index.json`` 返回 ``True``;本来就没有或删不掉返回 ``False``。
    """
    directory = Path(root)
    legacy = directory / LEGACY_INDEX_FILE_NAME
    removed = False
    for path in (legacy, legacy.with_suffix(legacy.suffix + _LEGACY_TMP_SUFFIX)):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed = True
    return removed


@dataclass(slots=True)
class CachedTrack:
    """索引里的一条已缓存音轨。

    字段覆盖三件事:**键**(怎么找到那个文件)、**展示**(本地缓存页上显示什么)、
    **回放**(怎么把它拼回可播放的模型)。刻意不存 ``Path``:音频文件名是相对名,
    缓存目录整体搬家之后记录仍然成立。

    Attributes:
        file_name: 音频文件名(形如 ``<20位摘要>.m4a``),在缓存根目录下。
        bvid: 视频 BV 号。
        cid: **分P**的 cid(领域铁律:视频级 cid 只是第 1P,多P会串歌)。
        quality_id: 音质档位 id,如 ``30280``。
        codec: 编码串(``mp4a.40.2`` / ``fLaC`` 等)。
        bandwidth: 接口给的真实码率(bit/s);``0`` 表示只能按档位取标称值。
        title: 展示名(多P合集里是分P标题,见
            :func:`~bilibili_music.core.models.track_title`)。
        author: UP主名。
        page_index: 分P序号(从 1 开始)。
        page_title: 分P标题原文(与 :attr:`title` 的区别在单P视频上才看得出来)。
        multipart: 缓存时它是不是多P合集。用来决定副标题要不要带 ``P3`` ——
            单P视频不显示 ``P1``(与 :func:`~bilibili_music.core.models.track_subtitle` 同一口径)。
        duration: 这一首的时长(秒);**分P自己的时长**,不是视频级的总时长。
        cover_url: 封面地址(已升级成 https)。
        size_bytes: 落盘时的文件字节数,供界面显示"这一首占多大"。
        cached_at: 首次缓存的时间戳(秒);重复播放**不刷新**它。
    """

    file_name: str
    bvid: str
    cid: int
    quality_id: int
    codec: str = ""
    bandwidth: int = 0
    title: str = ""
    author: str = ""
    page_index: int = 1
    page_title: str = ""
    multipart: bool = False
    duration: int = 0
    cover_url: str = ""
    size_bytes: int = 0
    cached_at: float = 0.0

    @property
    def key(self) -> CacheKey:
        """这一条的身份标识,与音频缓存键同构(含 ``cid``,多P不会串)。"""
        return (self.bvid, self.cid, self.quality_id, self.codec)


def _track_from_row(row: sqlite3.Row) -> CachedTrack | None:
    """把一行查询结果转成 :class:`CachedTrack`。

    缺 ``file_name`` / ``bvid`` / ``cid`` 的记录**直接丢弃**:这三样缺一个,这条记录既找
    不到文件也拼不出播放目标,留着只会在界面上显示一条点了没反应的歌。

    Args:
        row: ``cached_tracks`` 的一行。

    Returns:
        解析好的记录;结构不对、关键字段缺失或 ``file_name`` 不合法时返回 ``None``。
    """
    file_name = _as_str(row["file_name"])
    bvid = _as_str(row["bvid"])
    cid = _as_int(row["cid"])
    if not _is_plain_file_name(file_name) or not bvid or cid <= 0:
        return None
    return CachedTrack(
        file_name=file_name,
        bvid=bvid,
        cid=cid,
        quality_id=_as_int(row["quality_id"]),
        codec=_as_str(row["codec"]),
        bandwidth=max(0, _as_int(row["bandwidth"])),
        title=_as_str(row["title"]),
        author=_as_str(row["author"]),
        page_index=max(1, _as_int(row["page_index"], 1)),
        page_title=_as_str(row["page_title"]),
        multipart=bool(_as_int(row["multipart"])),
        duration=max(0, _as_int(row["duration"])),
        cover_url=_as_str(row["cover_url"]),
        size_bytes=max(0, _as_int(row["size_bytes"])),
        cached_at=max(0.0, _as_float(row["cached_at"])),
    )


class CacheIndex:
    """缓存索引的读写(库不可用时全部退化成"没有数据")。

    典型用法是"解析成功后记一笔、界面展示时读出来"::

        index = CacheIndex(db)
        index.remember(entry)
        for entry in index.entries():
            ...

    **没有内存副本**:每次调用都直接查库。原先那套"惰性读一次盘、之后只改内存"的写法
    在多了一份播放历史之后不再划算 —— 查询就是一次极便宜的索引扫描,而省掉副本就没有
    "内存与磁盘谁是对的"这种问题。

    Args:
        db: 本地库(与播放历史共用同一个实例;``LibraryDb`` 惰性连接)。
    """

    def __init__(self, db: LibraryDb) -> None:
        """绑定本地库;此时不建表、不查询。

        Args:
            db: 本地库。
        """
        self.db = db

    # ------------------------------------------------------------ 读

    def entries(self) -> tuple[CachedTrack, ...]:
        """全部已缓存音轨,**最近缓存的排在前面**。

        顺序取 ``rowid`` 倒序,即**写入顺序的倒序**,而不是按 ``cached_at`` 排序:
        更新一条已有记录(比如换了音质重下)不会改变它的位置,与"最新缓存的在最上面"
        这条直觉一致。用时间戳排会不确定 —— Windows 上 ``time.time()`` 的分辨率约
        15.6 ms,连着一批下载的记录时间戳会完全相同,那时顺序就交给偶然因素了。

        Returns:
            索引记录的元组;库不可用或一条都没有时是空元组。
        """
        rows = self.db.query("SELECT * FROM cached_tracks ORDER BY rowid DESC")
        found: list[CachedTrack] = []
        for row in rows:
            track = _track_from_row(row)
            if track is not None:
                found.append(track)
        return tuple(found)

    def find(self, bvid: str, cid: int) -> CachedTrack | None:
        """按 ``(bvid, cid)`` 找**音质最高**的那一条。

        同一个分P可能在不同时间缓存过多个档位(用户手动换过音质),播放时取最高的那一份
        最符合直觉。比较以码率为主、档位为辅:``bandwidth`` 未知(0)时才看 ``quality_id``,
        否则杜比/Hi-Res 这类档位会排错。

        Args:
            bvid: 视频 BV 号。
            cid: 分P的 cid。

        Returns:
            命中的记录;没有这个分P时返回 ``None``。
        """
        rows = self.db.query(
            "SELECT * FROM cached_tracks WHERE bvid = ? AND cid = ?", (bvid, int(cid))
        )
        best: CachedTrack | None = None
        for row in rows:
            track = _track_from_row(row)
            if track is None:
                continue
            if best is None or (track.bandwidth, track.quality_id) > (
                best.bandwidth,
                best.quality_id,
            ):
                best = track
        return best

    def reload(self) -> tuple[CachedTrack, ...]:
        """重新读一遍并返回内容。

        保留这个方法是给调用方的语义用的:界面每次切回"本地缓存"页时都会调用它,
        表达的是"重新对一次账"(用户可能在应用之外删过文件、或用 sqlite 工具动过库)。
        实现上每次都直接查库,所以这里只是 :meth:`entries` 的别名。

        Returns:
            当前库里的索引记录(与 :meth:`entries` 同一顺序)。
        """
        return self.entries()

    # ------------------------------------------------------------ 写

    def remember(self, track: CachedTrack) -> bool:
        """新增或更新一条记录。

        **重复播放不会刷新 ``cached_at``**:调用方每次解析成功都会调用本方法,若每次都把
        时间戳写成"现在",那么"缓存时间"这个字段就永远显示成今天,而且每次都真的写一次库。

        Args:
            track: 要记录的音轨;``file_name`` 不是纯文件名时直接忽略。

        Returns:
            内容真的变了**且写成功**返回 ``True``;内容与已有记录完全相同、入参非法
            或库不可用时返回 ``False``。
        """
        if not _is_plain_file_name(track.file_name):
            return False
        previous = self._by_key(track.key)
        if previous is not None:
            # 保留首次缓存时间:它表达的是"这首歌什么时候存下来的",不是"最后一次播放"
            track.cached_at = previous.cached_at
            if not track.size_bytes:
                track.size_bytes = previous.size_bytes
        elif track.cached_at <= 0:
            track.cached_at = time.time()
        if previous == track:
            return False
        written = self.db.execute(
            _UPSERT,
            (
                track.file_name,
                track.bvid,
                int(track.cid),
                int(track.quality_id),
                track.codec,
                max(0, int(track.bandwidth)),
                track.title,
                track.author,
                max(1, int(track.page_index)),
                track.page_title,
                1 if track.multipart else 0,
                max(0, int(track.duration)),
                track.cover_url,
                max(0, int(track.size_bytes)),
                float(track.cached_at),
            ),
        )
        return written is not None

    def forget(self, key: CacheKey) -> bool:
        """删掉一条记录。

        Args:
            key: :attr:`CachedTrack.key` 给出的身份标识。

        Returns:
            确实删掉了返回 ``True``;本来就没有这一条(或库不可用)返回 ``False``。
        """
        bvid, cid, quality_id, codec = key
        deleted = self.db.execute(
            "DELETE FROM cached_tracks "
            "WHERE bvid = ? AND cid = ? AND quality_id = ? AND codec = ?",
            (bvid, int(cid), int(quality_id), codec),
        )
        return bool(deleted)

    def retain_files(self, file_names: Iterable[str]) -> int:
        """只保留这些音频文件对应的记录(其余视为文件已不在)。

        一次性收口"清空缓存"与"剪掉文件已被手删的记录"两件事:调用方把**当前磁盘上真实
        存在的**文件名列表给进来即可,不必逐条判断。

        Args:
            file_names: 仍然存在的音频文件名集合。

        Returns:
            被删掉的记录条数;库不可用时是 ``0``。
        """
        alive = tuple(dict.fromkeys(_as_str(name) for name in file_names))
        if not alive:
            # 空集合用不上 IN,直接清表(缓存目录里一个音频都没有,索引自然也该是空的)
            deleted = self.db.execute("DELETE FROM cached_tracks")
        else:
            placeholders = ", ".join("?" * len(alive))
            deleted = self.db.execute(
                f"DELETE FROM cached_tracks WHERE file_name NOT IN ({placeholders})",
                alive,
            )
        return deleted if deleted is not None else 0

    def clear(self) -> int:
        """清空索引(不动播放历史,历史在另一张表里)。

        Returns:
            被清掉的记录条数;库不可用时是 ``0``。
        """
        deleted = self.db.execute("DELETE FROM cached_tracks")
        return deleted if deleted is not None else 0

    # ------------------------------------------------------------ 内部

    def _by_key(self, key: CacheKey) -> CachedTrack | None:
        """按缓存键取已有记录(只为 :meth:`remember` 的比较与时间戳保留服务)。

        Args:
            key: 缓存键。

        Returns:
            命中的记录;没有时返回 ``None``。
        """
        bvid, cid, quality_id, codec = key
        rows = self.db.query(
            "SELECT * FROM cached_tracks "
            "WHERE bvid = ? AND cid = ? AND quality_id = ? AND codec = ?",
            (bvid, int(cid), int(quality_id), codec),
        )
        if not rows:
            return None
        return _track_from_row(rows[0])


def entry_for(
    video: Video,
    page: Page,
    track: AudioTrack,
    path: Path,
    *,
    size_bytes: int = 0,
    cached_at: float = 0.0,
) -> CachedTrack:
    """按一次成功的解析结果造一条索引记录(纯函数)。

    放在 ``core`` 里而不是让解析器自己拼:字段取自哪个模型(时长要用**分P自己**的、
    展示名要用 :func:`~bilibili_music.core.models.track_title`)正是领域铁律所在,
    集中一处才不会在别处又写错一遍。

    Args:
        video: 目标视频。
        page: 目标分P。
        track: 实际落盘的音轨(带接口给的真实码率)。
        path: 音频文件路径,只取文件名入库。
        size_bytes: 文件字节数;``0`` 表示调用方没量过。
        cached_at: 缓存时间戳;``0`` 表示由 :meth:`CacheIndex.remember` 填当前时间。

    Returns:
        可直接交给 :meth:`CacheIndex.remember` 的记录。
    """
    return CachedTrack(
        file_name=path.name,
        bvid=video.bvid,
        cid=page.cid,
        quality_id=track.quality_id,
        codec=track.codec,
        bandwidth=track.bandwidth,
        title=track_title(video, page),
        author=video.author,
        page_index=page.index,
        page_title=page.title,
        multipart=video.is_multipart,
        duration=page.duration,
        cover_url=video.cover_https,
        size_bytes=max(0, size_bytes),
        cached_at=max(0.0, cached_at),
    )


def video_from_entry(entry: PlayableEntry) -> Video:
    """把一条记录(索引的或历史的)还原成可播放的 ``Video``(离线点播用)。

    **只放被缓存的那一个分P**,而且它的 ``index`` 就是记录里的分P序号:于是

    * 解析器看到 ``pages`` 非空,不会再去请求 ``view`` 详情(离线也能播);
    * ``video.page(entry.page_index)`` 能取到它,``cid`` 因此是正确的那个分P
      (领域铁律:用视频级 cid 会串歌);
    * 这个 ``Video`` 不是多P合集(只有一个分P),所以
      :func:`~bilibili_music.core.models.track_title` 会直接返回 ``title`` ——
      记录里存的就是当时算好的展示名,不必在这里重算一遍。

    参数类型是 :class:`~bilibili_music.core.models.PlayableEntry` 而不是某一个具体的
    记录类:缓存索引与播放历史的记录都满足它,于是"点一行就播"这条路径只有一份实现。

    代价是"这样播的这一首在界面上只显示它自己",拿不到同合集其它分P的列表 ——
    那些分P本来也可能没缓存,列出来只会让人觉得点了没反应。

    Args:
        entry: 索引记录或历史记录。

    Returns:
        可直接入队播放的 :class:`~bilibili_music.core.models.Video`。
    """
    return Video(
        bvid=entry.bvid,
        title=entry.title or entry.bvid,
        author=entry.author,
        duration=entry.duration,
        cover_url=entry.cover_url,
        cid=entry.cid,
        pages=[
            Page(
                index=entry.page_index,
                cid=entry.cid,
                title=entry.page_title,
                duration=entry.duration,
            )
        ],
    )
