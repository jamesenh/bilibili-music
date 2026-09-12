"""播放队列的纯逻辑:决定"下一首放什么"。

这个模块**不碰 Qt、不碰网络、不发请求**,只维护一份待播列表和一个游标,
所以四种播放模式的边界行为(空队列、末项、单曲循环、随机回绕)都能脱离
事件循环直接单测(见 ``tests/test_queue.py``)。

放 ``core`` 而不是 ``audio`` 的原因就在于此:排序与模式判定是纯逻辑,
而"把它和解析器、播放器串起来"那部分属于 :mod:`bilibili_music.audio.playback`。

设计取舍
--------

**队列项持有 ``Video`` 对象,而不是 ``bvid`` 字符串。**
``BilibiliClient`` 的 ``_video_cache`` 本来就持有同一批 ``Video`` 实例,队列引用
它们几乎不占用额外内存;反过来只存 ``bvid`` 的话,队列列表要显示标题 / UP主 /
封面就必须回查详情,未命中时那几行只能是占位符。

**两份顺序,不要混淆:**

* **插入顺序**(:attr:`PlayQueue.items`)—— 队列内容,界面列表显示这一份
* **播放顺序**(:attr:`PlayQueue.order`)—— 游标实际走的顺序

只有 :attr:`PlayMode.SHUFFLE` 下两者不同。``_order`` 里存的是 ``_items`` 中的
**同一批对象引用**而不是下标:增删时按身份移除即可,不必重映射下标 ——
下标重映射正是队列最容易写出 bug 的地方。
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from .models import Page, Video, track_subtitle, track_title

__all__ = [
    "PlayMode",
    "PlayQueue",
    "QueueItem",
]


class PlayMode(StrEnum):
    """播放模式。

    用 ``StrEnum`` 而不是普通 ``Enum``:配置层要把它直接写进 JSON,字符串枚举
    省掉一次显式 ``.value`` 转换,从配置读回来时也能直接用 ``PlayMode(raw)`` 校验。
    """

    SEQUENCE = "sequence"
    """顺序播放:走到末项就停,不前进。"""

    REPEAT_ALL = "repeat_all"
    """列表循环:末项之后回到第一项。"""

    REPEAT_ONE = "repeat_one"
    """单曲循环:只在**自然播完**时重播当前项;手动点"下一首"仍然换歌。"""

    SHUFFLE = "shuffle"
    """随机播放:打乱播放顺序,走完一轮才回绕(回绕时重新洗牌)。"""


@dataclass(slots=True, eq=False)
class QueueItem:
    """队列里的一项:某个视频的某个分P。

    存 ``page_index`` 而不是 ``Page`` 对象,是因为搜索阶段拿不到分P详情,
    而序号任何时候都有意义(详情补全后再靠 :attr:`page` 取)。

    用 ``eq=False`` 按**身份**比较而不是按值比较:用户可能把同一首歌重复加入队列,
    按值比较会让"移除这一项"和"高亮当前项"选错对象。
    """

    video: Video
    page_index: int = 1

    @property
    def key(self) -> tuple[str, int]:
        """唯一标识 ``(bvid, 分P序号)``,供判重与测试断言使用。"""
        return (self.video.bvid, self.page_index)

    @property
    def page(self) -> Page | None:
        """对应的分P;详情未补全或序号越界时为 ``None``。"""
        return self.video.page(self.page_index)

    @property
    def title(self) -> str:
        """队列 / 播放条上显示的名字(多P时是分P标题)。"""
        return track_title(self.video, self.page)

    @property
    def subtitle(self) -> str:
        """副标题:UP主,多P时附上分P序号。"""
        return track_subtitle(self.video, self.page)


class PlayQueue:
    """待播列表与游标。

    典型用法::

        queue = PlayQueue(rng=random.Random())
        queue.replace([QueueItem(video, 1) for video in results], start=2)
        item = queue.next_item(auto=True)   # 播完之后调用

    Args:
        mode: 初始播放模式。
        rng: 随机源。**测试必须注入固定 seed 的** ``random.Random``,
            否则 :attr:`PlayMode.SHUFFLE` 的顺序无从断言。

    Raises:
        ValueError: ``mode`` 不是合法的 :class:`PlayMode` 取值。
    """

    def __init__(
        self,
        *,
        mode: PlayMode = PlayMode.SEQUENCE,
        rng: random.Random | None = None,
    ) -> None:
        self._items: list[QueueItem] = []
        self._order: list[QueueItem] = []
        #: 当前项在 ``_order`` 里的下标;空队列为 -1(与"第 0 项"区分开)
        self._cursor = -1
        self._mode = PlayMode(mode)
        self._rng = rng if rng is not None else random.Random()

    # ------------------------------------------------------------ 只读视图

    @property
    def mode(self) -> PlayMode:
        """当前播放模式。"""
        return self._mode

    @property
    def items(self) -> tuple[QueueItem, ...]:
        """队列内容(**插入顺序**),界面列表显示这一份。"""
        return tuple(self._items)

    @property
    def order(self) -> tuple[QueueItem, ...]:
        """实际播放顺序;只有随机模式下与 :attr:`items` 不同。"""
        return tuple(self._order)

    @property
    def current(self) -> QueueItem | None:
        """当前项;队列为空时为 ``None``。"""
        if 0 <= self._cursor < len(self._order):
            return self._order[self._cursor]
        return None

    @property
    def current_index(self) -> int:
        """当前项在 :attr:`items` 里的下标,供界面高亮;没有当前项时为 ``-1``。"""
        current = self.current
        if current is None:
            return -1
        return self._items.index(current)

    def __len__(self) -> int:
        """队列长度(按插入顺序计)。"""
        return len(self._items)

    def __iter__(self) -> Iterator[QueueItem]:
        """按插入顺序遍历队列项。"""
        return iter(self._items)

    # ------------------------------------------------------------ 内容维护

    def replace(self, items: Iterable[QueueItem], *, start: int = 0) -> QueueItem | None:
        """用新内容整体替换队列,并从第 ``start`` 项开始。

        双击搜索结果走的就是这条路:整个结果列表成为队列,从双击那一行开始播。

        Args:
            items: 新的队列内容(插入顺序)。
            start: 从第几项开始播,取值是 ``items`` 的下标;越界会被夹到两端。

        Returns:
            新的当前项;``items`` 为空时返回 ``None``。
        """
        self._items = list(items)
        self._rebuild_order()
        if not self._items:
            self._cursor = -1
            return None
        # start 是"插入顺序"里的下标,而游标走的是播放顺序,随机模式下两者不同,
        # 所以必须按对象去播放顺序里定位,不能直接把 start 当游标用
        target = self._items[max(0, min(start, len(self._items) - 1))]
        self._cursor = self._order.index(target)
        return target

    def append(self, item: QueueItem) -> None:
        """把一项加到队列末尾。

        队列原本为空时,新项直接成为当前项 —— 否则会出现"加了歌但没有当前项"
        这种自相矛盾的状态。

        随机模式下新项排在播放顺序的**末尾**(而不是随机插队):"加入队列"
        应当是可预期的,想插队请用 :meth:`insert_next`。
        """
        self._items.append(item)
        self._order.append(item)
        if self._cursor < 0:
            self._cursor = self._order.index(item)

    def extend(self, items: Iterable[QueueItem]) -> None:
        """批量追加(逐项走 :meth:`append` 的规则)。"""
        for item in items:
            self.append(item)

    def add_video(self, video: Video, page_index: int = 1) -> QueueItem:
        """按"视频 + 分P序号"追加一项。

        Args:
            video: 目标视频。
            page_index: 分P序号,从 1 开始。

        Returns:
            新入队的项,便于调用方继续引用。
        """
        item = QueueItem(video=video, page_index=page_index)
        self.append(item)
        return item

    def insert_next(self, item: QueueItem) -> None:
        """插到当前项的**后面**,即播放顺序上的下一首。

        队列为空或还没有当前项时等价于 :meth:`append`。
        """
        current = self.current
        if current is None:
            self.append(item)
            return
        # 正常情况下 _items 只记插入顺序、_order 记播放顺序,两者各加一次;
        # 上面的空队列分支交给 append() 统一处理,避免同一项进队两次
        self._items.append(item)
        self._order.insert(self._order.index(current) + 1, item)

    def remove_at(self, index: int) -> QueueItem | None:
        """按下标(插入顺序)移除一项。

        游标规则:移除当前项之前的项时游标前移,保证**当前项不变**;移除当前项
        本身时游标停在原地,于是原本的"下一项"自动顶上来成为当前项。

        Args:
            index: 要移除的项在 :attr:`items` 里的下标。

        Returns:
            被移除的项;下标越界时返回 ``None``,且不改动队列。
        """
        if not 0 <= index < len(self._items):
            return None
        item = self._items.pop(index)
        position = self._order.index(item)
        self._order.pop(position)

        if not self._order:
            self._cursor = -1
        elif position < self._cursor:
            self._cursor -= 1
        elif position == self._cursor:
            # 移除的正是当前项:游标不动(下一项顶上);若它本来就是末项,
            # 游标要退到新的末项,否则会指到越界位置
            self._cursor = min(self._cursor, len(self._order) - 1)
        return item

    def clear(self) -> None:
        """清空队列并把游标复位(空队列没有当前项)。"""
        self._items.clear()
        self._order.clear()
        self._cursor = -1

    def jump_to(self, index: int) -> QueueItem | None:
        """跳到插入顺序里的第 ``index`` 项(用户点队列列表时用)。

        Args:
            index: 目标项在 :attr:`items` 里的下标。

        Returns:
            新的当前项;下标越界时返回 ``None``,且**不改动游标**。
        """
        if not 0 <= index < len(self._items):
            return None
        target = self._items[index]
        self._cursor = self._order.index(target)
        return target

    # ------------------------------------------------------------ 前进与后退

    def next_item(self, *, auto: bool = False) -> QueueItem | None:
        """前进到下一项。

        Args:
            auto: 是否为"自然播完"触发的自动前进。**只有它为真时**
                :attr:`PlayMode.REPEAT_ONE` 才重播当前项 —— 用户手点"下一首"
                时应当真的换歌,否则单曲循环下按钮会像坏了一样。

        Returns:
            新的当前项;队列为空、或顺序模式下已经在末项时返回 ``None``
            (调用方据此决定是否停止播放)。
        """
        if not self._order:
            return None
        if auto and self._mode is PlayMode.REPEAT_ONE:
            return self.current
        if self._cursor + 1 < len(self._order):
            self._cursor += 1
            return self._order[self._cursor]
        if self._mode is PlayMode.REPEAT_ALL:
            self._cursor = 0
            return self._order[self._cursor]
        if self._mode is PlayMode.SHUFFLE:
            # 回绕时重新洗牌:否则每一轮的顺序都一样,随机的意义就没了
            self._rebuild_order()
            self._cursor = 0
            return self._order[0] if self._order else None
        return None

    def previous_item(self) -> QueueItem | None:
        """后退到上一项。

        与 :meth:`next_item` 不对称的地方:**单曲循环不影响手动后退**,
        用户点"上一首"就是想换歌。

        Returns:
            新的当前项;队列为空、或非循环模式下已经在第一项时返回 ``None``。
        """
        if not self._order:
            return None
        if self._cursor - 1 >= 0:
            self._cursor -= 1
            return self._order[self._cursor]
        if self._mode in (PlayMode.REPEAT_ALL, PlayMode.SHUFFLE):
            self._cursor = len(self._order) - 1
            return self._order[self._cursor]
        return None

    def set_mode(self, mode: PlayMode) -> None:
        """切换播放模式,**当前项不变**。

        进随机模式时重排播放顺序;离开随机模式时恢复插入顺序。前提是
        :attr:`items` 始终保存着插入顺序 —— 这也是随机模式只动 ``_order``
        而不动 ``_items`` 的原因。

        Args:
            mode: 目标模式。

        Raises:
            ValueError: ``mode`` 不是合法的 :class:`PlayMode` 取值。
        """
        mode = PlayMode(mode)
        if mode == self._mode:
            return
        self._mode = mode
        current = self.current
        self._rebuild_order()
        # 放在重建之后:重排后必须把游标对回原来那一项,否则一切换模式就会跳歌
        self._cursor = self._order.index(current) if current is not None else -1

    # ------------------------------------------------------------ 内部

    def _rebuild_order(self) -> None:
        """按当前模式重建播放顺序(随机模式下是 :attr:`items` 的一个排列)。"""
        self._order = list(self._items)
        if self._mode is PlayMode.SHUFFLE:
            self._rng.shuffle(self._order)
