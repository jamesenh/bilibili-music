"""本地结构化数据的 sqlite 库(缓存索引、播放历史与下载任务共用的落点)。

为什么是 sqlite 而不是 JSON
--------------------------

缓存索引原来是一份 ``index.json``(每次增删都要整份重写),播放历史如果照抄这个做法,
就得再维护第二份"每次开始播放都整份重写"的文件。改用 sqlite 之后:

* **增删是一行的事**:``INSERT`` / ``DELETE`` 不再重写整份数据,历史这种"高频写"的数据
  才敢每次开始播放都落一条。
* **一个文件多张表**:缓存索引、播放历史与下载任务同库,想做"这首在不在本地"这种
  跨表查询时不必再对几份文件。它们**互不牵连**:"清空缓存"只删 ``cached_tracks``,
  "清空历史"只删 ``play_history``,"清空已完成的任务"只删 ``download_tasks``。
* **崩溃安全由数据库保证**:原先那套"先写 ``.part`` 再 ``os.replace``"的纪律不再需要,
  sqlite 自己的日志(这里开 WAL)就是同一件事。

``sqlite3`` 是**标准库**,不新增任何依赖(``AGENTS.md`` 第 1.1 节的技术栈表已按此更新)。

这个模块只负责"连接 + 建表 + 一句 SQL",不认识各张表字段的含义 —— 那三件事分别由
:mod:`.cache_index`、:mod:`.history` 与 :mod:`.download_task` 负责。

设计取舍
--------

* **库文件放配置目录**(``~/Library/Application Support/BiliMusic/library.db``),
  与 ``config.json`` 同级:两者都是"用户数据",该跟着漫游;缓存目录按 ``%LOCALAPPDATA%``
  语义是"随时可以删掉的临时文件",把历史放进去会让"清空缓存"顺手带走它。
* **不 import Qt**(``core`` 红线),路径由 :func:`library_db_path` 或调用方注入 ——
  测试因此可以指到沙箱目录,不会碰到用户真实的库。
* **失败一律降级,绝不抛异常给调用方**:磁盘满、目录不可写、文件被别的程序锁住时,
  :meth:`LibraryDb.execute` / :meth:`LibraryDb.query` 静默返回"没有结果"。上层于是
  自然退回"缓存盲查 + 历史空列表"。索引与历史都是**可再生/非关键**数据,它们出问题
  不该让应用起不来,更不该让一次已经成功的播放变成失败。
* **失败后不再重试**:第一次连不上就在本进程内记住,后续调用直接返回。
  否则每次切歌、每次重绘界面都要去撞一次同样的磁盘错误。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import config_root

__all__ = [
    "DB_FILE_NAME",
    "SCHEMA_VERSION",
    "LibraryDb",
    "library_db_path",
]

#: 库文件名(放在配置目录下,与 ``config.json`` 同级)。
DB_FILE_NAME = "library.db"

#: 库结构版本,写进 sqlite 的 ``user_version``。
#:
#: 与旧的 ``index.json`` 一样:当前只写不校验,留着它是为了以后真要改表结构时能一眼看出
#: 这份库是哪一代建的,而不是为将来的迁移提前写一堆没人走的代码。
#:
#: 版本 2(2026-09-14):新增 ``download_tasks`` 表。建表语句全部是
#: ``CREATE TABLE IF NOT EXISTS``、**不做任何 ALTER**,所以老库直接打开就能多出这张新表,
#: 不需要迁移代码 —— 这也正是当初选"每次打开都重跑一遍建表"而非"探测版本再升级"的原因。
SCHEMA_VERSION = 2

#: 建表语句。``IF NOT EXISTS`` 让"每次打开都执行一遍"变成幂等操作,不必先探测文件是否存在。
#:
#: ``cached_tracks`` 以 ``file_name`` 为主键(音频文件名由缓存键推导,天然唯一);
#: ``(bvid, cid)`` 上的索引对应"按视频与分P反查"这条最常用的查询。
#: ``play_history`` 以 ``(bvid, cid)`` 为主键 —— 同一首只留最近一次(去重由主键 + UPSERT
#: 完成,不靠应用层判断)。
#: ``download_tasks`` 以 ``bvid`` 为主键 —— "同一个视频不能有两个任务"这条规则因此由
#: 主键而不是应用层保证,重启后重新入队也会自然收敛成一行。
_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS cached_tracks (
        file_name   TEXT PRIMARY KEY,
        bvid        TEXT NOT NULL DEFAULT '',
        cid         INTEGER NOT NULL DEFAULT 0,
        quality_id  INTEGER NOT NULL DEFAULT 0,
        codec       TEXT NOT NULL DEFAULT '',
        bandwidth   INTEGER NOT NULL DEFAULT 0,
        title       TEXT NOT NULL DEFAULT '',
        author      TEXT NOT NULL DEFAULT '',
        page_index  INTEGER NOT NULL DEFAULT 1,
        page_title  TEXT NOT NULL DEFAULT '',
        multipart   INTEGER NOT NULL DEFAULT 0,
        duration    INTEGER NOT NULL DEFAULT 0,
        cover_url   TEXT NOT NULL DEFAULT '',
        size_bytes  INTEGER NOT NULL DEFAULT 0,
        cached_at   REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cached_tracks_video ON cached_tracks (bvid, cid)",
    """
    CREATE TABLE IF NOT EXISTS download_tasks (
        bvid        TEXT PRIMARY KEY,
        title       TEXT NOT NULL DEFAULT '',
        author      TEXT NOT NULL DEFAULT '',
        cover_url   TEXT NOT NULL DEFAULT '',
        total_pages INTEGER NOT NULL DEFAULT 0,
        done_pages  INTEGER NOT NULL DEFAULT 0,
        bytes_done  INTEGER NOT NULL DEFAULT 0,
        page_index  INTEGER NOT NULL DEFAULT 0,
        state       TEXT NOT NULL DEFAULT 'paused',
        error       TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL DEFAULT 0,
        updated_at  REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS play_history (
        bvid        TEXT NOT NULL,
        cid         INTEGER NOT NULL,
        page_index  INTEGER NOT NULL DEFAULT 1,
        title       TEXT NOT NULL DEFAULT '',
        author      TEXT NOT NULL DEFAULT '',
        page_title  TEXT NOT NULL DEFAULT '',
        multipart   INTEGER NOT NULL DEFAULT 0,
        duration    INTEGER NOT NULL DEFAULT 0,
        cover_url   TEXT NOT NULL DEFAULT '',
        played_at   REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (bvid, cid)
    )
    """,
)


def library_db_path() -> Path:
    """本地库的默认路径(配置目录下的 ``library.db``)。

    与缓存目录**故意不同**:缓存放 ``%LOCALAPPDATA%``(可以随时删),库文件放配置目录
    ``%APPDATA%`` / ``~/Library/Application Support``(用户数据该跟着漫游) ——
    这与 ``config_root`` 的选择理由完全一致。

    Returns:
        库文件路径;目录本身可能还不存在,由 :class:`LibraryDb` 负责创建。
    """
    return config_root() / DB_FILE_NAME


class LibraryDb:
    """本地库的连接与建表。

    典型用法是"谁需要数据谁拿着它"::

        db = LibraryDb()
        index = CacheIndex(db)
        history = PlayHistory(db)

    实例**惰性连接**:构造函数不碰磁盘,第一次真正查询时才建目录、开库、建表。
    于是"用户根本没用过缓存与历史"时不会凭空多出一个库文件。

    Args:
        path: 库文件路径;``None`` 表示 :func:`library_db_path` 的平台默认值。
    """

    def __init__(self, path: Path | None = None) -> None:
        """记录库文件路径(不建目录、不开库)。

        Args:
            path: 库文件路径;``None`` 表示平台默认配置目录下的 ``library.db``。
        """
        self.path: Path = Path(path) if path is not None else library_db_path()
        #: 已建立的连接;``None`` 表示还没连过(或连接失败过,见 :attr:`_broken`)
        self._conn: sqlite3.Connection | None = None
        #: 是否已经失败过。失败后不再重试,避免每次调用都去撞同一个磁盘错误。
        self._broken = False

    # ------------------------------------------------------------ 连接

    @property
    def available(self) -> bool:
        """库当前可不可用(能否建起连接并完成建表)。

        上层用它决定"要不要走降级路径",而不是自己去捕获异常 ——
        这个模块对外**不抛异常**。
        """
        return self._connection() is not None

    def close(self) -> None:
        """关掉连接(应用退出时调用)。

        关闭失败不做任何事:进程马上就要结束了,为一个关不掉的连接报错毫无意义。
        """
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _connection(self) -> sqlite3.Connection | None:
        """取连接,必要时开库建表。

        Returns:
            可用的连接;开不了库(目录不可写、文件被占用、磁盘满)时返回 ``None``。
        """
        if self._conn is not None:
            return self._conn
        if self._broken:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            # WAL:写入不再阻塞读取,进程被杀也不会留下半截事务(见模块 docstring)。
            conn.execute("PRAGMA journal_mode = WAL")
            # 被别的进程(比如用户开着的 sqlite 工具)短暂锁住时等一会儿,而不是立刻失败
            conn.execute("PRAGMA busy_timeout = 3000")
            # NORMAL:WAL 下已经足够安全(掉电最多丢最后几条事务),换来明显更少的 fsync
            conn.execute("PRAGMA synchronous = NORMAL")
            self._create_schema(conn)
            conn.commit()
        except (sqlite3.Error, OSError):
            self._broken = True
            return None
        self._conn = conn
        return conn

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        """建表并把结构版本写进 ``user_version``。

        Args:
            conn: 已打开的连接。

        Raises:
            sqlite3.Error: 建表失败(原样向上抛,由 :meth:`_connection` 统一降级)。
        """
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")

    # ------------------------------------------------------------ 读写

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int | None:
        """执行一条写语句并提交。

        Args:
            sql: 语句文本;占位符一律用 ``?``(不要自己拼字符串)。
            params: 与占位符对应的参数。

        Returns:
            受影响的行数;库不可用、语句出错或提交失败时返回 ``None``。

        Note:
            失败**不回滚也不重试**:索引与历史都允许"这次没记上",
            下一条记录照样会尝试写入。
        """
        conn = self._connection()
        if conn is None:
            return None
        try:
            cursor = conn.execute(sql, tuple(params))
            conn.commit()
        except sqlite3.Error:
            return None
        return max(0, cursor.rowcount)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """执行一条查询并取回全部结果。

        Args:
            sql: 语句文本。
            params: 与占位符对应的参数。

        Returns:
            结果行列表;库不可用或语句出错时返回**空列表**(调用方于是自然拿到"没有数据")。
        """
        conn = self._connection()
        if conn is None:
            return []
        try:
            return list(conn.execute(sql, tuple(params)))
        except sqlite3.Error:
            return []
