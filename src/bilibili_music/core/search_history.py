"""搜索历史:记下用户搜过哪些词,给搜索框的历史下拉框当数据源。

为什么和「最近播放」分表
------------------------

两者都叫"历史",但**写入频率与用途完全不同**:播放历史每开始播一首就写一条,还要按
``history_limit`` 裁剪;搜索历史只在用户**提交**一次搜索时写一条,量级小得多。放同一张表
就得给记录加一个类型字段,查询、去重与裁剪都得先按类型过滤 —— 分开两张表,各自的语义与
上限互不干扰,"清空播放历史"也就不会顺手把用户搜过的词清掉。

为什么不落 ``config.json``
--------------------------

配置的写入是"整份重写 + 原子替换"(见 :mod:`.config`),而这里每搜一次就要写一条。
不是不能做,只是白担了"写一半被杀,把用户的音量、播放模式一起弄丢"的风险。于是与缓存
索引、播放历史共用 ``library.db``(路线图 M3.4 于 2026-09-14 拍板:再加一张表,不另起文件)。

存多少、显示多少
----------------

库里保留 :data:`SEARCH_HISTORY_LIMIT` 条(50),**下拉框显示**最多
:data:`SEARCH_HISTORY_VISIBLE` 条(10)。存得比显示多是有意的:过滤是按"输入内容包含于
历史词"做的,只留 10 条的话,第 11 条以前的词在有机会被过滤命中之前就已经被裁掉了。

容错:搜索历史同样是**可再生的**
--------------------------------

库打不开、行被手改坏(``keyword`` 是空串、``null`` 或非文本):一律当作"没有这一条"。
它只服务于搜索框的提示,失败绝不该影响搜索本身 —— 与 :mod:`.history` 同一口径。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from .library_db import LibraryDb

__all__ = [
    "SEARCH_HISTORY_LIMIT",
    "SEARCH_HISTORY_VISIBLE",
    "SearchHistory",
    "filter_terms",
    "normalize_keyword",
]

#: 库里最多保留多少条搜索词。
#:
#: 50 是"够用来找回关键词"与"每次写入后裁剪的代价"之间的折中:一条只有几十字节,
#: 50 条的库文件增量可以忽略。
SEARCH_HISTORY_LIMIT = 50

#: 搜索框的历史下拉框最多显示多少条。
SEARCH_HISTORY_VISIBLE = 10

#: 写入一条搜索词的 UPSERT 语句。
#:
#: ``ON CONFLICT(keyword) DO UPDATE`` 就是"同一个词只留最近一次":命中主键只刷新时间
#: (``rowid`` 保持不变),否则插入新行。**不删再插**:那样会换掉 ``rowid``,而同一次搜索
#: 之内时间戳相同时的先后顺序要靠 ``rowid`` 兜底(见 :meth:`SearchHistory.entries`)。
_UPSERT = """
INSERT INTO search_history (keyword, searched_at)
VALUES (?, ?)
ON CONFLICT(keyword) DO UPDATE SET searched_at = excluded.searched_at
"""

#: 按搜索时间倒序取记录;时间戳相同时用 ``rowid`` 兜底,保证顺序确定(而不是随机)。
_ORDER_BY = "ORDER BY searched_at DESC, rowid DESC"

#: 裁剪时保留的"最新若干条"子查询。
_KEEP_NEWEST = f"SELECT rowid FROM search_history {_ORDER_BY} LIMIT ?"


def normalize_keyword(raw: object) -> str:
    """把输入框里的、或库里的原始值收敛成一个可用的搜索词。

    只做 ``strip()``,**不压缩词内空白、不改大小写**:历史词要能原样填回搜索框,而提交
    搜索时(``MainWindow.on_search``)用的也正是 ``strip()`` —— 两处必须同一个口径,否则
    "搜过一次的那个词"会在历史里变成另一个样子,用户点了之后搜出来的东西也不一样。

    Args:
        raw: 原始值。可能来自输入框,也可能来自被手改过的库,因此**不假定**它是 ``str``。

    Returns:
        去掉首尾空白后的文本;不是文本、或去空白后为空时返回空串(调用方据此丢弃)。
    """
    return raw.strip() if isinstance(raw, str) else ""


def filter_terms(
    terms: Sequence[str],
    query: str,
    *,
    limit: int = SEARCH_HISTORY_VISIBLE,
) -> tuple[str, ...]:
    """按输入内容过滤历史词(**包含匹配**),并保持原有的先后顺序。

    两条规则:

    * ``query`` 去掉首尾空白后为空(刚点开搜索框、还没打字):返回前 ``limit`` 条 ——
      "点一下搜索框就给出最近搜过什么"正是这个功能的基本形态;
    * 匹配用 ``casefold()`` 做**大小写不敏感**:B站的关键字里混着拉丁字母
      (``MV`` / ``live`` / ``4K``),用户不会记得当初敲的是大写还是小写。

    Args:
        terms: 历史词,已按"最近搜索的在最前"排好序。
        query: 输入框当前内容。
        limit: 最多返回几条;``<= 0`` 表示不限制。

    Returns:
        匹配到的历史词(顺序与 ``terms`` 一致);一条都匹配不上时是空元组。
    """
    needle = normalize_keyword(query).casefold()
    matched = [
        term
        for term in terms
        if not needle or needle in normalize_keyword(term).casefold()
    ]
    if limit > 0:
        matched = matched[:limit]
    return tuple(matched)


class SearchHistory:
    """搜索历史的读写(表 ``search_history``)。

    与 :class:`~bilibili_music.core.history.PlayHistory` 同一套路:实例只认
    :class:`~bilibili_music.core.library_db.LibraryDb`,库不可用时**静默降级**成"没有历史"。

    Args:
        db: 本地库句柄。
        limit: 库里最多保留多少条;默认 :data:`SEARCH_HISTORY_LIMIT`。
        now: 取当前时刻的回调(秒,浮点)。**只为让"顺序"可被测试注入一个受控时钟**;
            生产路径永远用 :func:`time.time`。
    """

    def __init__(
        self,
        db: LibraryDb,
        *,
        limit: int = SEARCH_HISTORY_LIMIT,
        now: Callable[[], float] = time.time,
    ) -> None:
        """记录库句柄、保留条数与时钟(不碰磁盘)。

        Args:
            db: 本地库句柄。
            limit: 保留条数上限;非正数按 1 处理(留 0 条等于功能失效,更像配置写错了)。
            now: 取当前时刻的回调。
        """
        self._db = db
        self._limit = max(1, int(limit))
        self._now = now

    # ------------------------------------------------------------ 写

    def record(self, keyword: str) -> bool:
        """记一条搜索词:已有的提到最前、新的插到最前,并把总数裁到上限。

        Args:
            keyword: 用户提交搜索时用的关键字(内部先 :func:`normalize_keyword`)。

        Returns:
            是否真的写进了库;关键字为空、或库不可用时返回 ``False``。
        """
        text = normalize_keyword(keyword)
        if not text:
            return False
        written = self._db.execute(_UPSERT, (text, self._now())) is not None
        if written:
            self.trim()
        return written

    def trim(self, limit: int | None = None) -> int:
        """把表裁到上限(超出部分按搜索时间从旧到新丢掉)。

        Args:
            limit: 保留多少条;``None`` 表示用构造时给的 ``limit``。

        Returns:
            删掉的行数;库不可用时返回 ``0``。
        """
        keep = self._limit if limit is None else max(1, int(limit))
        deleted = self._db.execute(
            f"DELETE FROM search_history WHERE rowid NOT IN ({_KEEP_NEWEST})",
            (keep,),
        )
        return 0 if deleted is None else deleted

    # ------------------------------------------------------------ 读

    def entries(self, limit: int | None = None) -> tuple[str, ...]:
        """按"最近搜索的在最前"取历史词。

        手改坏的行走这里被丢掉:``keyword`` 是空串、``null`` 或非文本时
        :func:`normalize_keyword` 返回空串,这里不把它当成一条历史。

        Args:
            limit: 最多取几条;``None`` 表示全部(库里最多 :data:`SEARCH_HISTORY_LIMIT` 条)。

        Returns:
            历史词元组;库不可用或表里没有数据时是空元组。
        """
        sql = f"SELECT keyword FROM search_history {_ORDER_BY}"
        params: tuple[object, ...] = ()
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params = (int(limit),)
        rows = self._db.query(sql, params)
        return tuple(
            text
            for text in (normalize_keyword(row["keyword"]) for row in rows)
            if text
        )

    def suggest(
        self, query: str, limit: int = SEARCH_HISTORY_VISIBLE
    ) -> tuple[str, ...]:
        """给出搜索框的历史下拉框该显示哪些词(先过滤、再截断)。

        **先把整份历史读出来、再在内存里过滤**,而不是让 SQL 用 ``LIKE`` 去筛:上限只有
        50 条,而 ``LIKE`` 还得处理通配符转义(``%`` / ``_`` 在关键字里很常见)与大小写,
        换来的一点查询效率并不值这个复杂度。

        Args:
            query: 输入框当前内容;空串表示"还没打字",此时给最近的前 ``limit`` 条。
            limit: 最多给几条。

        Returns:
            过滤后的历史词;没有可显示的内容时是空元组。
        """
        return filter_terms(self.entries(), query, limit=limit)
