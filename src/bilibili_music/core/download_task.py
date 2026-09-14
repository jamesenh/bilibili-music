"""下载任务:"一键缓存整个合集"的任务模型与持久化。

一条任务 = **一个视频的全部分P**(音乐区里一个分P就是一首歌,见 ``AGENTS.md`` 1.3),
不是"一个分P一条任务":用户在搜索结果里右键一行,期望的是"把这个合集存下来",
把它拆成 200 条任务只会让对话框长到没法看。

为什么任务要落盘(而不是只活在内存里)
------------------------------------

一个 200P 的合集按限速串行下要几十分钟,用户很可能中途关掉应用。任务只存内存的话,
下次启动就只剩一堆不知道下到哪儿了的音频文件,既接不上、也说不清进度。所以任务写进
``library.db``(见 :mod:`.library_db` 的 ``download_tasks`` 表),重启后**仍然是暂停的**,
由用户点"继续"接着下 —— 不自动开跑,否则开一次应用就暗中发几百个请求。

**不落盘分P级明细**:表里只记 `total_pages` / `done_pages` / `bytes_done` 这几个数。
"到底还差哪几P"在恢复时用**缓存索引**现算(已经在本地的分P直接跳过)——
缓存索引才是"哪些歌真的在磁盘上"的唯一事实来源,再维护一份分P状态迟早会与它打架
(比如用户在应用之外删了缓存文件,或者某一P是播放时顺手缓存下来的)。

这个模块不 import Qt(``core`` 红线),也不认识网络:它只管"任务长什么样、怎么存"。
真正的下载调度在 :mod:`bilibili_music.audio.downloader`。
"""

from __future__ import annotations

import enum
import sqlite3
import time
from dataclasses import dataclass, replace

from .library_db import LibraryDb
from .models import Video

__all__ = [
    "DownloadTask",
    "DownloadTaskStore",
    "TaskState",
    "task_for_video",
    "task_percent",
    "task_progress_text",
]


class TaskState(enum.Enum):
    """一个下载任务的状态机取值。

    取值本身就是**存进库里的字符串**(``state`` 列),所以 :attr:`value` 不许随便改:
    改名等于让老库里的任务变成未知状态。

    ============== ==================================================
    状态            含义
    ============== ==================================================
    ``PENDING``    排队中:前面还有任务在跑,轮到它就会自动开始
    ``RUNNING``    正在缓存(同一时刻整个应用只有一个任务处于这个状态)
    ``PAUSED``     用户暂停,或重启后未完成的任务被规整成这个状态
    ``FAILED``     某个分P失败,整个任务挂起等用户处理
    ``DONE``       全部可缓存的分P都已在本地
    ============== ==================================================
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    DONE = "done"

    @property
    def text(self) -> str:
        """界面上显示的中文状态文案。"""
        return {
            TaskState.PENDING: "排队中",
            TaskState.RUNNING: "缓存中",
            TaskState.PAUSED: "已暂停",
            TaskState.FAILED: "已暂停(出错)",
            TaskState.DONE: "已完成",
        }[self]

    @property
    def is_finished(self) -> bool:
        """是否已经结束(不会再被调度)。"""
        return self is TaskState.DONE

    @property
    def is_active(self) -> bool:
        """是否属于"还没干完、队列可能去调度它"的状态。

        ``FAILED`` **不算**活动:它挂在那里等用户决定继续还是移除,
        自动把它捡起来重试就等于在风控里反复撞墙(见 ``audio/downloader.py``)。
        """
        return self in (TaskState.PENDING, TaskState.RUNNING)

    @classmethod
    def parse(cls, raw: object, default: TaskState | None = None) -> TaskState:
        """把库里读到的字符串转成状态;**认不出来就当暂停**。

        库文件是可以被用户拿 sqlite 工具手改的,``state`` 里出现任何东西都不奇怪。
        兜底选 ``PAUSED`` 而不是 ``PENDING``:暂停绝不会自己跑起来,
        而一个"认不出来的状态"被当成排队中,就会在下一次调度时莫名其妙开始下载。

        Args:
            raw: 库里的原始值。
            default: 认不出来时返回的状态;``None`` 表示 :data:`TaskState.PAUSED`。

        Returns:
            解析出的状态。
        """
        fallback = TaskState.PAUSED if default is None else default
        if isinstance(raw, TaskState):
            return raw
        try:
            return TaskState(str(raw))
        except ValueError:
            return fallback


def _as_int(value: object, default: int = 0) -> int:
    """把不可信的值转成整数;转不了就用 ``default``。

    sqlite 的列是**动态类型**的(与 ``cache_index`` / ``history`` 同一套理由):
    表结构声明了 INTEGER,手改过的库里照样可能存着 ``"abc"`` 或 ``NULL``。

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
class DownloadTask:
    """一条"缓存这个合集"的任务。

    字段分三组:**标识**(``bvid`` + 展示用标题)、**进度**(分P计数、字节数、当前分P)、
    **状态**(状态机取值与失败原因)。刻意不存分P列表 —— 那份数据在恢复时重新取详情,
    而"还剩哪些分P"由缓存索引现算(见模块 docstring)。

    Attributes:
        bvid: 视频 BV 号,同时也是这条任务的主键(同一视频只能有一个任务)。
        title: 视频标题(详情里的原标题,不是分P标题)。
        author: UP主名,列表里显示用。
        cover_url: 封面地址(已升级成 https),留给以后在任务行里显示封面。
        total_pages: 这个视频一共有多少个分P;0 表示还没取到详情。
        done_pages: 已在本地缓存的分P数(含任务开始前就已经缓存的)。
        bytes_done: 已完成部分占用的字节数(按索引记录累加,用于显示"已下载"总量)。
        page_index: 当前正在下的分P序号;0 表示还没开始。失败时它是**失败的那一P**,
            所以继续时会从这一P重新下(不做字节级续传,见 ``audio/downloader.py``)。
        state: 状态机取值。
        error: 失败原因(给用户看的文本);非失败状态为空串。
        created_at: 任务创建时间戳(秒);它同时是队列顺序。
        updated_at: 最后一次变化的时间戳(秒)。
    """

    bvid: str
    title: str = ""
    author: str = ""
    cover_url: str = ""
    total_pages: int = 0
    done_pages: int = 0
    bytes_done: int = 0
    page_index: int = 0
    state: TaskState = TaskState.PAUSED
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def key(self) -> str:
        """这条任务的身份标识(就是 ``bvid``)。"""
        return self.bvid

    @property
    def display_title(self) -> str:
        """列表里显示的标题;标题为空时退回 BV 号(总比一行空白强)。"""
        return self.title or self.bvid

    @property
    def progress_text(self) -> str:
        """进度文本,如 ``已完成 3 / 共 200 个分P``(纯格式化的转发)。"""
        return task_progress_text(self)

    @property
    def percent(self) -> int:
        """按分P计数算出的百分比(0~100)。"""
        return task_percent(self)

    def with_state(self, state: TaskState, *, error: str = "") -> "DownloadTask":
        """返回一个只改了状态(与失败原因)的副本,并刷新 :attr:`updated_at`。

        用副本而不是就地改:状态变化要**同时**落库,而"就地把内存里的对象改掉、
        再尽量记得写库"正是最容易漏写一处的地方。

        Args:
            state: 目标状态。
            error: 失败原因;非失败状态传空串(调用方不必自己清,默认就清了)。

        Returns:
            新的 :class:`DownloadTask`。
        """
        return replace(self, state=state, error=error, updated_at=time.time())


def task_percent(task: DownloadTask) -> int:
    """按分P计数算进度百分比(纯函数)。

    以**分P个数**而不是字节数算:每个分P的体积差得很远,按字节算会出现"进度条卡在
    99% 十几分钟"(最后几首是长曲),而按分P数走是均匀的。字节数只用来显示"已下载多少"。

    Args:
        task: 任务。

    Returns:
        0~100 的整数;``total_pages`` 为 0(详情还没取到)时是 ``0``。
    """
    if task.total_pages <= 0:
        return 0
    ratio = max(0, task.done_pages) / task.total_pages
    return max(0, min(100, int(ratio * 100)))


def task_progress_text(task: DownloadTask) -> str:
    """把任务的进度摊成一行中文(纯函数)。

    Args:
        task: 任务。

    Returns:
        形如 ``"已完成 3 / 共 200 个分P"`` 的文本;没有分P数据时退回
        ``"还没有取到分P列表"``;已完成但有分P没缓存(那一P正在播放、被跳过)时
        在末尾补一句,免得用户以为任务漏下了。
    """
    total = max(0, task.total_pages)
    done = max(0, task.done_pages)
    if total <= 0:
        return "还没有取到分P列表"
    if done >= total:
        return f"已完成全部 {total} 个分P"
    text = f"已完成 {done} / 共 {total} 个分P"
    if task.state is TaskState.DONE:
        # 走到这里说明"任务结束但没下全":只可能是那些当时正在播放的分P被跳过了
        text += f"(还有 {total - done} 个未缓存)"
    return text


def task_for_video(video: Video, *, now: float = 0.0) -> DownloadTask:
    """按一个视频建一条新任务(纯函数)。

    分P总数在这里就地取:调用方(主窗口)是**先取到详情、再让用户确认**的,
    所以创建这一刻 `pages` 一定是全的。

    Args:
        video: 要缓存的目标视频(详情已补全)。
        now: 创建时间戳;``0`` 表示由 :meth:`DownloadTaskStore.upsert` 填当前时间。

    Returns:
        一条 ``PENDING`` 状态的新任务。
    """
    return DownloadTask(
        bvid=video.bvid,
        title=video.title,
        author=video.author,
        cover_url=video.cover_https,
        total_pages=len(video.pages),
        state=TaskState.PENDING,
        created_at=max(0.0, now),
    )


#: 写入一条任务(新增或整行覆盖)。
#:
#: 冲突目标是主键 ``bvid``:同一个视频的任务**只有一条**,重复入队会覆盖旧行而不是多出
#: 一行 —— "同一视频不能有两个任务"这条规则因此由主键保证(与 ``play_history`` 的去重
#: 是同一个手法)。``created_at`` 也一起写:重新入队的任务本该排到队尾。
_UPSERT = """
INSERT INTO download_tasks
    (bvid, title, author, cover_url, total_pages, done_pages, bytes_done, page_index,
     state, error, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(bvid) DO UPDATE SET
    title       = excluded.title,
    author      = excluded.author,
    cover_url   = excluded.cover_url,
    total_pages = excluded.total_pages,
    done_pages  = excluded.done_pages,
    bytes_done  = excluded.bytes_done,
    page_index  = excluded.page_index,
    state       = excluded.state,
    error       = excluded.error,
    created_at  = excluded.created_at,
    updated_at  = excluded.updated_at
"""

#: 队列顺序 = 创建顺序。时间戳相同时用 ``rowid`` 兜底,免得顺序随机
#: (Windows 上 ``time.time()`` 的分辨率约 15.6 ms,连着建几条任务时间戳会一样)。
_ORDER_BY = "ORDER BY created_at ASC, rowid ASC"


def _task_from_row(row: sqlite3.Row) -> DownloadTask | None:
    """把一行查询结果转成 :class:`DownloadTask`。

    缺 ``bvid`` 的行**直接丢弃**:没有它这条任务既恢复不了、也删不掉,留在列表里
    只会是个点了没反应的死行(与缓存索引、播放历史同一条判据)。

    Args:
        row: ``download_tasks`` 的一行。

    Returns:
        解析好的任务;``bvid`` 为空时返回 ``None``。
    """
    bvid = _as_str(row["bvid"])
    if not bvid:
        return None
    return DownloadTask(
        bvid=bvid,
        title=_as_str(row["title"]),
        author=_as_str(row["author"]),
        cover_url=_as_str(row["cover_url"]),
        total_pages=max(0, _as_int(row["total_pages"])),
        done_pages=max(0, _as_int(row["done_pages"])),
        bytes_done=max(0, _as_int(row["bytes_done"])),
        page_index=max(0, _as_int(row["page_index"])),
        state=TaskState.parse(row["state"]),
        error=_as_str(row["error"]),
        created_at=max(0.0, _as_float(row["created_at"])),
        updated_at=max(0.0, _as_float(row["updated_at"])),
    )


class DownloadTaskStore:
    """下载任务的持久化(库不可用时退化成"没有任务")。

    与 :class:`~bilibili_music.core.history.PlayHistory` 同样是"薄薄一层 SQL":
    它不知道分P、不知道网络,只负责把 :class:`DownloadTask` 存下来、读回来。
    调度与进度推进在 :mod:`bilibili_music.audio.downloader` 里。

    Args:
        db: 本地库(与缓存索引、播放历史共用同一个实例;``LibraryDb`` 惰性连接)。
    """

    def __init__(self, db: LibraryDb) -> None:
        """绑定本地库;此时不建表、不查询。

        Args:
            db: 本地库。
        """
        self.db = db

    # ------------------------------------------------------------ 写

    def upsert(self, task: DownloadTask) -> bool:
        """新增或整行覆盖一条任务。

        ``created_at`` / ``updated_at`` 为 0 时填当前时间:让调用方(下载器)不必自己
        到处记得打时间戳。``bvid`` 为空的任务直接忽略 —— 它在库里没有身份。

        Args:
            task: 要写入的任务(**会被就地补时间戳**,与 ``CacheIndex.remember``
                补 ``cached_at`` 是同一套做法)。

        Returns:
            写成功返回 ``True``;``bvid`` 为空或库不可用时返回 ``False``。
        """
        if not task.bvid:
            return False
        now = time.time()
        if task.created_at <= 0:
            task.created_at = now
        task.updated_at = max(task.updated_at, now)
        written = self.db.execute(
            _UPSERT,
            (
                task.bvid,
                task.title,
                task.author,
                task.cover_url,
                max(0, int(task.total_pages)),
                max(0, int(task.done_pages)),
                max(0, int(task.bytes_done)),
                max(0, int(task.page_index)),
                task.state.value,
                task.error,
                float(task.created_at),
                float(task.updated_at),
            ),
        )
        return written is not None

    def remove(self, bvid: str) -> bool:
        """删掉一条任务(**不动已经下载下来的音频文件**)。

        Args:
            bvid: 视频 BV 号。

        Returns:
            确实删掉了返回 ``True``;本来就没有这一条(或库不可用)返回 ``False``。
        """
        deleted = self.db.execute("DELETE FROM download_tasks WHERE bvid = ?", (bvid,))
        return bool(deleted)

    def clear_finished(self) -> int:
        """删掉所有已完成的任务(**不动音频文件**)。

        只清"已完成":暂停与失败的任务还留着用户的意图,清掉它们等于替用户放弃。

        Returns:
            被删掉的条数;库不可用时是 ``0``。
        """
        deleted = self.db.execute(
            "DELETE FROM download_tasks WHERE state = ?", (TaskState.DONE.value,)
        )
        return deleted if deleted is not None else 0

    def clear(self) -> int:
        """清空全部任务(不区分状态)。主要给测试与"重置"用。

        Returns:
            被删掉的条数;库不可用时是 ``0``。
        """
        deleted = self.db.execute("DELETE FROM download_tasks")
        return deleted if deleted is not None else 0

    # ------------------------------------------------------------ 读

    def entries(self) -> tuple[DownloadTask, ...]:
        """全部任务,**先建的排在前面**(即队列顺序)。

        Returns:
            任务的元组;库不可用或一条都没有时是空元组。
        """
        rows = self.db.query(f"SELECT * FROM download_tasks {_ORDER_BY}")
        found: list[DownloadTask] = []
        for row in rows:
            task = _task_from_row(row)
            if task is not None:
                found.append(task)
        return tuple(found)

    def find(self, bvid: str) -> DownloadTask | None:
        """按 ``bvid`` 取一条任务。

        Args:
            bvid: 视频 BV 号。

        Returns:
            命中的任务;没有时返回 ``None``。
        """
        rows = self.db.query("SELECT * FROM download_tasks WHERE bvid = ?", (bvid,))
        if not rows:
            return None
        return _task_from_row(rows[0])
