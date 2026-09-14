"""最近播放:按开始播放的时刻记录"放过哪一首"(列表页的数据源)。

这条功能要回答的是"我刚才听的是什么",所以记的粒度是**分P**(音乐区一个分P就是一首歌),
而不是视频。

记录时机(A1)
-------------

写入挂在**解析成功、音频真正就绪**那一刻(``ui/main_window.py::_on_audio_ready``),
不是"队列切到这一项":后者在解析失败、或用户快速切歌时都会留下一堆其实没听过的记录。
一次播放只写一条,`position_changed` 那种每几百毫秒来一次的信号**不写**(会刷爆磁盘)。

同一首只留最近一次(B1)
----------------------

``(bvid, cid)`` 是主键,重复播放走 UPSERT:位置被提到最前、时间被刷新,而不是多出一行。
条数上限由 ``AppConfig.history_limit``(默认 200,可手改 ``config.json``)决定,
每次写入后按 ``played_at`` 从旧到新裁剪。

不用 JSON 的理由
----------------

"每次开始播放都落一条"意味着高频写入,而 JSON 方案每写一条都要整份重写 + 原子替换;
这里与缓存索引共用 ``library.db``(见 :mod:`.library_db`)。

容错:历史是**可再生的**,坏了不许影响播放
----------------------------------------

库打不开、字段类型被手改坏、单行缺关键字段:一律"当作没有这一条/没有这条记录",
不抛异常、不弹窗。历史只服务于界面展示,它失败绝不该让用户听不了歌。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .config import DEFAULT_HISTORY_LIMIT
from .library_db import LibraryDb
from .models import Page, Video, track_title

__all__ = [
    "HistoryEntry",
    "PlayHistory",
    "history_entry_for",
]

#: 写入一条记录时的 UPSERT 语句。
#:
#: ``ON CONFLICT(bvid, cid) DO UPDATE`` 是"同一首只留最近一次"的实现:命中主键就更新
#: 时间与展示字段,否则插入。**不删再插**:那样会换掉 ``rowid``,而 ``played_at`` 相同时
#: 的先后顺序要靠 ``rowid`` 兜底(见 :meth:`PlayHistory.entries`)。
_UPSERT = """
INSERT INTO play_history
    (bvid, cid, page_index, title, author, page_title, multipart, duration,
     cover_url, played_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(bvid, cid) DO UPDATE SET
    page_index = excluded.page_index,
    title      = excluded.title,
    author     = excluded.author,
    page_title = excluded.page_title,
    multipart  = excluded.multipart,
    duration   = excluded.duration,
    cover_url  = excluded.cover_url,
    played_at  = excluded.played_at
"""

#: 按播放时间倒序取记录;时间戳相同时用 ``rowid`` 兜底,保证顺序确定(而不是随机)。
_ORDER_BY = "ORDER BY played_at DESC, rowid DESC"

#: 裁剪时保留的"最新若干条"子查询。
_KEEP_NEWEST = f"SELECT rowid FROM play_history {_ORDER_BY} LIMIT ?"


def _as_int(value: object, default: int = 0) -> int:
    """把不可信的值转成整数;转不了就用 ``default``。

    sqlite 的列是**动态类型**的:表结构声明了 INTEGER,手改过的库里照样可能存着
    ``"abc"`` 或 ``NULL``。这里的目标是"尽量救回来",不是"报告用户乱改数据"。

    Args:
        value: 待转换的原始值。
        default: 转换失败时的兜底值。

    Returns:
        转换后的整数,或 ``default``。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            return int(float(value))  # type: ignore[arg-type]
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


@dataclass(slots=True)
class HistoryEntry:
    """一条播放记录。

    字段与 :class:`~bilibili_music.core.cache_index.CachedTrack` 有意保持同构(少了"文件
    与体积",多了"播放时间"):两者都能还原成"只含该分P的可播放 ``Video``"
    (见 :func:`~bilibili_music.core.cache_index.video_from_entry`,它只要求这些公共字段)。

    Attributes:
        bvid: 视频 BV 号。
        cid: **分P**的 cid(领域铁律:视频级 cid 只是第 1P,多P会串歌)。
        page_index: 分P序号(从 1 开始)。
        title: 展示名(多P合集里是分P标题,见
            :func:`~bilibili_music.core.models.track_title`)。
        author: UP主名。
        page_title: 分P标题原文。
        multipart: 播放时它是不是多P合集。用来决定副标题要不要带 ``P3`` ——
            单P视频不显示 ``P1``(与 :func:`~bilibili_music.core.models.track_subtitle` 同一口径)。
        duration: 这一首的时长(秒);**分P自己的时长**,不是视频级的总时长。
        cover_url: 封面地址(已升级成 https)。
        played_at: 播放时间戳(UTC 秒);``0`` 表示由 :meth:`PlayHistory.record` 填当前时间。
    """

    bvid: str
    cid: int
    page_index: int = 1
    title: str = ""
    author: str = ""
    page_title: str = ""
    multipart: bool = False
    duration: int = 0
    cover_url: str = ""
    played_at: float = 0.0

    @property
    def key(self) -> tuple[str, int]:
        """这条记录的身份标识:``(bvid, cid)``。

        不含音质与 codec:换音质重播同一首歌在历史里仍然是同一首,不该多出一条。
        """
        return (self.bvid, self.cid)


def history_entry_for(
    video: Video, page: Page, *, played_at: float = 0.0
) -> HistoryEntry:
    """按一次成功的解析结果造一条播放记录(纯函数)。

    与 :func:`~bilibili_music.core.cache_index.entry_for` 同一个理由:字段该取自哪个模型
    (时长要用**分P自己**的、展示名要用 :func:`~bilibili_music.core.models.track_title`)
    正是领域铁律所在,集中一处才不会在别处又写错一遍。

    Args:
        video: 正在播放的视频(解析成功后详情已补全)。
        page: 正在播放的分P。
        played_at: 播放时间戳;``0`` 表示由 :meth:`PlayHistory.record` 填当前时间。

    Returns:
        可直接交给 :meth:`PlayHistory.record` 的记录。
    """
    return HistoryEntry(
        bvid=video.bvid,
        cid=page.cid,
        page_index=page.index,
        title=track_title(video, page),
        author=video.author,
        page_title=page.title,
        multipart=video.is_multipart,
        duration=page.duration,
        cover_url=video.cover_https,
        played_at=max(0.0, played_at),
    )


def _entry_from_row(row: sqlite3.Row) -> HistoryEntry | None:
    """把一行查询结果转成 :class:`HistoryEntry`。

    缺 ``bvid`` / ``cid`` 的行**直接丢弃**:两者缺一个就既播不了、也删不掉,
    在列表里显示出来只会让用户点了没反应(与缓存索引同一条判据)。

    Args:
        row: ``play_history`` 的一行。

    Returns:
        解析好的记录;关键字段缺失或非法时返回 ``None``。
    """
    bvid = _as_str(row["bvid"])
    cid = _as_int(row["cid"])
    if not bvid or cid <= 0:
        return None
    return HistoryEntry(
        bvid=bvid,
        cid=cid,
        page_index=max(1, _as_int(row["page_index"], 1)),
        title=_as_str(row["title"]),
        author=_as_str(row["author"]),
        page_title=_as_str(row["page_title"]),
        multipart=bool(_as_int(row["multipart"])),
        duration=max(0, _as_int(row["duration"])),
        cover_url=_as_str(row["cover_url"]),
        played_at=max(0.0, _as_float(row["played_at"])),
    )


class PlayHistory:
    """最近播放的读写(去重、倒序、按上限裁剪)。

    Args:
        db: 本地库(与缓存索引同一个实例;``LibraryDb`` 惰性连接,
            所以这里不会凭空建库)。
        limit: 最多保留多少条;非正数表示不裁剪。默认取
            :data:`~bilibili_music.core.config.DEFAULT_HISTORY_LIMIT`。
    """

    def __init__(self, db: LibraryDb, *, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        """绑定库与条数上限。

        Args:
            db: 本地库。
            limit: 最多保留的记录条数(0 或负数表示不限)。
        """
        self.db = db
        self.limit = int(limit)

    # ------------------------------------------------------------ 写

    def record(self, entry: HistoryEntry) -> bool:
        """记下"这一首刚开始播"(同一首只留最近一次)。

        Args:
            entry: 要记录的条目;``played_at <= 0`` 时填当前时间。
                ``bvid`` 为空或 ``cid`` 非正时直接忽略(这种条目既播不了也删不掉)。

        Returns:
            写成功返回 ``True``;条目非法或库不可用时返回 ``False``。
        """
        if not entry.bvid or entry.cid <= 0:
            return False
        if entry.played_at <= 0:
            entry.played_at = time.time()
        written = self.db.execute(
            _UPSERT,
            (
                entry.bvid,
                int(entry.cid),
                max(1, int(entry.page_index)),
                entry.title,
                entry.author,
                entry.page_title,
                1 if entry.multipart else 0,
                max(0, int(entry.duration)),
                entry.cover_url,
                float(entry.played_at),
            ),
        )
        self.trim()
        return written is not None

    def trim(self, limit: int | None = None) -> int:
        """按上限裁掉最旧的记录。

        Args:
            limit: 要保留的条数;``None`` 表示用构造时给的 :attr:`limit`。
                非正数表示不裁剪。

        Returns:
            被删掉的条数;库不可用或没超限时是 ``0``。
        """
        keep = self.limit if limit is None else int(limit)
        if keep <= 0:
            return 0
        deleted = self.db.execute(
            f"DELETE FROM play_history WHERE rowid NOT IN ({_KEEP_NEWEST})",
            (keep,),
        )
        return deleted if deleted is not None else 0

    def remove(self, bvid: str, cid: int) -> bool:
        """删掉一条记录(**不动缓存文件**)。

        Args:
            bvid: 视频 BV 号。
            cid: 分P的 cid。

        Returns:
            确实删掉了返回 ``True``;本来就没有这一条(或库不可用)返回 ``False``。
        """
        deleted = self.db.execute(
            "DELETE FROM play_history WHERE bvid = ? AND cid = ?", (bvid, int(cid))
        )
        return bool(deleted)

    def clear(self) -> int:
        """清空历史(**不动缓存文件**)。

        Returns:
            被清掉的条数;库不可用时是 ``0``。
        """
        deleted = self.db.execute("DELETE FROM play_history")
        return deleted if deleted is not None else 0

    # ------------------------------------------------------------ 读

    def entries(self) -> tuple[HistoryEntry, ...]:
        """全部记录,**最近播放的排在前面**。

        时间戳相同时用插入顺序(``rowid``)兜底:Windows 上 ``time.time()`` 的分辨率约
        15.6 ms,连着开始播几首的记录时间戳可能完全相同,没有兜底的话顺序就不确定了。

        Returns:
            记录的元组;库不可用或一条都没有时是空元组。
        """
        rows = self.db.query(f"SELECT * FROM play_history {_ORDER_BY}")
        found: list[HistoryEntry] = []
        for row in rows:
            entry = _entry_from_row(row)
            if entry is not None:
                found.append(entry)
        return tuple(found)

    def find(self, bvid: str, cid: int) -> HistoryEntry | None:
        """取某一条记录(界面用它判断"这一行是不是正在播的那一首")。

        Args:
            bvid: 视频 BV 号。
            cid: 分P的 cid。

        Returns:
            命中的记录;没有时返回 ``None``。
        """
        rows = self.db.query(
            "SELECT * FROM play_history WHERE bvid = ? AND cid = ?",
            (bvid, int(cid)),
        )
        if not rows:
            return None
        return _entry_from_row(rows[0])
