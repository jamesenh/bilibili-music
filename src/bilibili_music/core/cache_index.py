"""已缓存音频的索引(sidecar JSON,让缓存**可枚举**)。

为什么需要它
------------

``core/cache.py`` 的文件名是 ``(bvid, cid, 音质, codec)`` 的 SHA-1 摘要,只能"知道键
再问文件在不在"(盲查);反过来"磁盘上现在缓存了哪些歌"是拼不出来的 —— 而"本地缓存"页
要显示曲名、UP主、音质、体积,还要能离线点播,光有文件路径拿不到这些。

所以解析成功时顺手把这一条音轨的元数据写进索引。索引是**可再生的**:它只是"磁盘上的
一次快照",坏了、丢了都不影响播放 —— :func:`~bilibili_music.audio.resolver.pick_best_cached`
在索引缺失时会退回按档位盲查(见 ``AGENTS.md`` 第 1.2 节的主线不变)。

设计取舍
--------

* **单个 ``index.json``,而不是每个音频文件配一个 ``<key>.json``**:枚举只读一次盘、
  增删也只写一次盘(原子替换),缓存目录里也不会散出成百上千个小文件。代价是每新增一首
  就整体重写一次索引 —— 按每条记录约 200 字节估,几百首也只有几十 KB。
* **写入"先 ``.part`` 再 ``os.replace``"**,与缓存 / 配置同一条纪律:进程被杀不会留下
  半截 JSON。
* **写盘失败不抛异常**:索引只服务于界面展示与缓存快路径,磁盘满或没权限时放弃这次更新
  即可。它的调用方是播放解析流程,**绝不能让写索引失败导致播不出来**。
* **``file_name`` 只接受纯文件名**:索引文件在用户目录里、可以被手改,只认裸文件名就能
  挡住 ``..\\..\\x.m4a`` 这类路径穿越 —— 否则一次"删除缓存"会删到缓存目录之外。
* **读取尽量救**:坏 JSON、字段类型不对、缺关键字段的单条记录一律跳过,不做"整份作废"。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .models import AudioTrack, Page, Video, track_title

__all__ = [
    "CachedTrack",
    "CacheIndex",
    "INDEX_FILE_NAME",
    "INDEX_VERSION",
    "entry_for",
    "video_from_entry",
]

#: 索引文件名(与音频文件同放在缓存根目录下,``*.m4a`` / ``*.part`` 的通配不会碰到它)。
INDEX_FILE_NAME = "index.json"

#: 索引格式版本。当前只在写盘时记录、读盘时不校验:留着它是为了以后真要改结构时,
#: 能一眼看出这份文件是哪一代写的(而不是为将来的迁移提前写一堆没人走的代码)。
INDEX_VERSION = 1

#: 一条记录的身份:``(bvid, cid, 音质档位, codec)``,与音频文件的缓存键同构。
CacheKey = tuple[str, int, int, str]

#: 原子写入用的临时后缀;与音频缓存同一条纪律,临时文件与正式文件同目录才保证原子。
_TMP_SUFFIX = ".part"


def _as_int(value: object, default: int = 0) -> int:
    """把不可信的值转成整数;转不了就用 ``default``。

    索引文件可能被手改坏,直接 ``int()`` 会抛 ``TypeError`` / ``ValueError`` 把整次读取
    炸掉,而这里的目标是"尽量救回来"。

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

    见模块 docstring:索引里的 ``file_name`` 会被用来拼路径(删除、查文件),而索引文件
    本身是可被手改的,所以进内存前就得把 ``..`` / 目录分隔符挡在外面。

    Args:
        name: 待检查的字符串。

    Returns:
        是纯文件名返回 ``True``;空串、带目录或带盘符都返回 ``False``。
    """
    if not name or name in (".", ".."):
        return False
    return Path(name).name == name


@dataclass(slots=True)
class CachedTrack:
    """索引里的一条已缓存音轨。

    字段覆盖三件事:**键**(怎么找到那个文件)、**展示**(本地缓存页上显示什么)、
    **回放**(怎么把它拼回可播放的模型)。刻意不存 ``Path``:索引文件是可移植的,
    存相对文件名才能在缓存目录整体搬家后继续成立。

    Attributes:
        file_name: 音频文件名(形如 ``<20位摘要>.m4a``),与索引同目录。
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


#: 记录里认识的全部字段名(读盘时用它忽略陌生键,兼容旧版本/新版本写的文件)。
#: 必须放在 :class:`CachedTrack` **之后**:``fields()`` 只对已定义的 dataclass 有效。
_TRACK_FIELDS = frozenset(f.name for f in fields(CachedTrack))


class CacheIndex:
    """缓存索引的读写(纯 JSON,原子替换,容错读取)。

    典型用法是"解析成功后记一笔、界面展示时读出来"::

        index = CacheIndex(cache_root())
        index.remember(entry)
        for entry in index.entries():
            ...

    实例内部保留一份读盘结果,只在首次访问与显式 :meth:`reload` 时读文件 ——
    切歌时会连续调用 :meth:`remember`,每次都读一遍盘没有意义。

    Args:
        root: 索引所在目录(与音频缓存同一个目录);``None`` 表示平台默认缓存根目录。
    """

    def __init__(self, root: Path | None = None) -> None:
        """绑定索引目录;此时不读盘、不建目录。

        Args:
            root: 索引所在目录;``None`` 表示 :func:`~bilibili_music.core.cache.cache_root`。
        """
        if root is None:
            # 延迟导入:cache 模块要构造本类,顶层互相 import 会成环
            from .cache import cache_root

            root = cache_root()
        self.root = Path(root)
        self.path = self.root / INDEX_FILE_NAME
        #: 惰性读盘的内存副本(``key -> CachedTrack``);``None`` 表示还没读过
        self._tracks: dict[CacheKey, CachedTrack] | None = None

    # ------------------------------------------------------------ 读

    def entries(self) -> tuple[CachedTrack, ...]:
        """全部已缓存音轨,**最近缓存的排在前面**。

        顺序取**写入顺序的倒序**,而不是按 ``cached_at`` 排序:索引里的字典本身保留
        插入顺序(更新已有条目不会改变它的位置),倒过来就是"最新缓存的在最上面"。
        用时间戳排会不确定 —— Windows 上 ``time.time()`` 的分辨率约 15.6 ms,连着一批
        下载的记录时间戳会完全相同,那时顺序就交给时间戳之外的偶然因素了。

        Returns:
            索引记录的元组;索引不存在、为空或整份读不出来时是空元组。
        """
        tracks = self._tracks if self._tracks is not None else self._read()
        return tuple(reversed(list(tracks.values())))

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
        tracks = self._tracks if self._tracks is not None else self._read()
        matches = [t for t in tracks.values() if t.bvid == bvid and t.cid == cid]
        if not matches:
            return None
        return max(matches, key=lambda t: (t.bandwidth, t.quality_id))

    def reload(self) -> tuple[CachedTrack, ...]:
        """丢掉内存副本、重新读盘,并返回新的内容。

        界面每次切回"本地缓存"页时调用它:用户在应用之外删过文件、或用别的方式动过
        缓存目录时,只有重读才看得见。

        Returns:
            重读后的索引记录(与 :meth:`entries` 同一顺序)。
        """
        self._tracks = None
        return self.entries()

    # ------------------------------------------------------------ 写

    def remember(self, track: CachedTrack) -> bool:
        """新增或更新一条记录。

        **重复播放不会刷新 ``cached_at``**:调用方每次解析成功都会调用本方法,若每次都把
        时间戳写成"现在",那么"缓存时间"这个字段就永远显示成今天,而且每次都真的落一次盘。

        Args:
            track: 要记录的音轨;``file_name`` 不是纯文件名时直接忽略。

        Returns:
            内容真的变了**且写盘成功**返回 ``True``;内容与已有记录完全相同、入参非法
            或写盘失败时返回 ``False``。写盘失败时内存副本仍按最新内容保留(见 ``_write``)。
        """
        if not _is_plain_file_name(track.file_name):
            return False
        tracks = self._ensure()
        previous = tracks.get(track.key)
        if previous is not None:
            # 保留首次缓存时间:它表达的是"这首歌什么时候存下来的",不是"最后一次播放"
            track.cached_at = previous.cached_at
            if not track.size_bytes:
                track.size_bytes = previous.size_bytes
        elif track.cached_at <= 0:
            track.cached_at = time.time()
        if previous == track:
            return False
        tracks[track.key] = track
        return self._write(tracks)

    def forget(self, key: CacheKey) -> bool:
        """删掉一条记录。

        Args:
            key: :attr:`CachedTrack.key` 给出的身份标识。

        Returns:
            确实删掉了返回 ``True``;本来就没有这一条返回 ``False``。
        """
        tracks = self._ensure()
        if tracks.pop(key, None) is None:
            return False
        self._write(tracks)
        return True

    def retain_files(self, file_names: Iterable[str]) -> int:
        """只保留这些音频文件对应的记录(其余视为文件已不在)。

        一次性收口"清空缓存"与"剪掉文件已被手删的记录"两件事:调用方把**当前磁盘上真实
        存在的**文件名列表给进来即可,不必逐条判断。

        Args:
            file_names: 仍然存在的音频文件名集合。

        Returns:
            被删掉的记录条数。
        """
        alive = set(file_names)
        tracks = self._ensure()
        dropped = [key for key, track in tracks.items() if track.file_name not in alive]
        if not dropped:
            return 0
        for key in dropped:
            tracks.pop(key, None)
        self._write(tracks)
        return len(dropped)

    def clear(self) -> int:
        """清空索引。

        Returns:
            被清掉的记录条数。
        """
        tracks = self._ensure()
        count = len(tracks)
        if not count:
            return 0
        tracks.clear()
        self._write(tracks)
        return count

    # ------------------------------------------------------------ 内部:读写

    def _ensure(self) -> dict[CacheKey, CachedTrack]:
        """取内存副本,必要时先读一次盘。"""
        if self._tracks is None:
            self._tracks = self._read()
        return self._tracks

    def _read(self) -> dict[CacheKey, CachedTrack]:
        """读盘并把能救的记录都救回来。

        任何一步失败都当"索引不可用"处理(空索引),**不抛异常**:索引只是缓存的一份
        快照,读不出来时上层会退回盲查,照样能播。

        Returns:
            ``key -> CachedTrack`` 的字典;文件不存在、坏 JSON、结构不对时是空字典。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        records = raw.get("tracks")
        if not isinstance(records, list):
            return {}
        tracks: dict[CacheKey, CachedTrack] = {}
        for record in records:
            track = _parse_record(record)
            if track is not None:
                tracks[track.key] = track
        return tracks

    def _write(self, tracks: dict[CacheKey, CachedTrack]) -> bool:
        """原子地把索引写盘。

        先写同目录的 ``.part`` 再 ``os.replace`` —— 同一文件系统内的 ``replace`` 是原子的,
        "索引文件存在"因此等价于"它是一份完整的索引"。

        Args:
            tracks: 要落盘的完整内容。

        Returns:
            写成功返回 ``True``;写不进去(磁盘满 / 无权限)返回 ``False``。

        Note:
            写失败**不回滚内存副本**:本次运行里界面看到的就是最新状态,磁盘上的旧索引
            下次启动会被读回来,那时大不了退回盲查。
        """
        payload = {
            "version": INDEX_VERSION,
            "tracks": [asdict(track) for track in tracks.values()],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + _TMP_SUFFIX)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # 失败必须清掉 .part,否则会在用户目录里留下一个永远不会被复用的残文件
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True


def _parse_record(raw: object) -> CachedTrack | None:
    """把 JSON 里的一条记录解析成 :class:`CachedTrack`。

    缺 ``file_name`` / ``bvid`` / ``cid`` 的记录**直接丢弃**:这三样缺一个,这条记录既找
    不到文件也拼不出播放目标,留着只会在界面上显示一条点了没反应的歌。

    Args:
        raw: JSON 数组里的一项(类型不可信)。

    Returns:
        解析好的记录;结构不对、关键字段缺失或 ``file_name`` 不合法时返回 ``None``。
    """
    if not isinstance(raw, dict):
        return None
    file_name = _as_str(raw.get("file_name"))
    bvid = _as_str(raw.get("bvid"))
    cid = _as_int(raw.get("cid"))
    if not _is_plain_file_name(file_name) or not bvid or cid <= 0:
        return None
    # 只取认识的键:旧版本写的记录可能带已经没有的字段,新版本写的也可能带将来才有的
    known = {key: value for key, value in raw.items() if key in _TRACK_FIELDS}
    return CachedTrack(
        file_name=file_name,
        bvid=bvid,
        cid=cid,
        quality_id=_as_int(known.get("quality_id")),
        codec=_as_str(known.get("codec")),
        bandwidth=max(0, _as_int(known.get("bandwidth"))),
        title=_as_str(known.get("title")),
        author=_as_str(known.get("author")),
        page_index=max(1, _as_int(known.get("page_index"), 1)),
        page_title=_as_str(known.get("page_title")),
        multipart=bool(known.get("multipart")),
        duration=max(0, _as_int(known.get("duration"))),
        cover_url=_as_str(known.get("cover_url")),
        size_bytes=max(0, _as_int(known.get("size_bytes"))),
        cached_at=max(0.0, _as_float(known.get("cached_at"))),
    )


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


def video_from_entry(entry: CachedTrack) -> Video:
    """把一条索引记录还原成可播放的 ``Video``(离线点播用)。

    **只放被缓存的那一个分P**,而且它的 ``index`` 就是记录里的分P序号:于是

    * 解析器看到 ``pages`` 非空,不会再去请求 ``view`` 详情(离线也能播);
    * ``video.page(entry.page_index)`` 能取到它,``cid`` 因此是正确的那个分P
      (领域铁律:用视频级 cid 会串歌);
    * 这个 ``Video`` 不是多P合集(只有一个分P),所以
      :func:`~bilibili_music.core.models.track_title` 会直接返回 ``title`` ——
      记录里存的就是当时算好的展示名,不必在这里重算一遍。

    代价是"离线播的这一首在界面上只显示它自己",拿不到同合集其它分P的列表 ——
    那些分P本来也可能没缓存,列出来只会让人觉得点了没反应。

    Args:
        entry: 索引记录。

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
