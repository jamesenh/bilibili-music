"""播放队列的纯逻辑单元测试:四种模式、边界与增删。

不触网、不碰 Qt、不写磁盘,所以这个文件在 DSH 沙箱里也能整份跑通。

随机模式一律注入**固定 seed** 的 ``random.Random``:不注入就没有可断言的顺序,
这正是 :class:`~bilibili_music.core.queue.PlayQueue` 收 ``rng`` 参数的原因。
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.models import Page, Video  # noqa: E402
from bilibili_music.core.queue import PlayMode, PlayQueue, QueueItem  # noqa: E402


def _video(bvid: str, *, pages: int = 1, author: str = "某UP") -> Video:
    """造一个带分P的视频样本(纯内存,不碰网络)。"""
    return Video(
        bvid=bvid,
        title=f"视频{bvid}",
        author=author,
        pages=[
            Page(index=i + 1, cid=1000 + i, title=f"第{i + 1}首", duration=180)
            for i in range(pages)
        ],
    )


def _queue(*bvids: str, mode: PlayMode = PlayMode.SEQUENCE, seed: int = 42) -> PlayQueue:
    """造一个队列,内容为给定的 bvid 序列(每项取第 1P)。

    用 ``replace`` 而不是逐个 ``append``:双击搜索结果走的正是 ``replace``,
    而且随机模式只有经过 ``replace``/``set_mode`` 才会真正洗牌。
    """
    queue = PlayQueue(mode=mode, rng=random.Random(seed))
    queue.replace([QueueItem(_video(bvid)) for bvid in bvids])
    return queue


def _ids(queue: PlayQueue) -> list[str]:
    """按**插入顺序**取 bvid 列表(即界面列表显示的顺序)。"""
    return [item.video.bvid for item in queue.items]


def _play_ids(queue: PlayQueue) -> list[str]:
    """按**播放顺序**取 bvid 列表。"""
    return [item.video.bvid for item in queue.order]


class TestQueueItem(unittest.TestCase):
    """队列项的展示名与分P解析 —— 队列列表和播放条都依赖它。"""

    def test_single_page_uses_video_title(self) -> None:
        """单P视频没有独立歌名,显示名必须是视频标题(哪怕它有一个 P1)。"""
        item = QueueItem(_video("BW1"))
        self.assertEqual(item.title, "视频BW1")

    def test_multipart_uses_page_title(self) -> None:
        """多P合集里每个分P是一首独立的歌,显示名必须用分P标题。"""
        item = QueueItem(_video("BW2", pages=3), page_index=2)
        self.assertEqual(item.title, "第2首")

    def test_unknown_page_falls_back_to_video_title(self) -> None:
        """分P序号越界时退回视频标题,而不是抛异常或显示空串。"""
        item = QueueItem(_video("BW3", pages=2), page_index=99)
        self.assertIsNone(item.page)
        self.assertEqual(item.title, "视频BW3")

    def test_blank_page_title_falls_back_to_video_title(self) -> None:
        """部分老视频的 part 是空串,不能因此显示出空白条目。"""
        video = _video("BW4", pages=2)
        video.pages[1].title = ""
        item = QueueItem(video, page_index=2)
        self.assertEqual(item.title, "视频BW4")

    def test_subtitle_hides_page_number_for_single_page(self) -> None:
        """单P视频不显示 P1:列表里每行挂个无信息量的 P1 只是噪声。"""
        item = QueueItem(_video("BW5", author="阿婆主"))
        self.assertEqual(item.subtitle, "阿婆主")

    def test_subtitle_shows_page_number_for_multipart(self) -> None:
        """多P视频的副标题要带分P序号,否则同名分P无法区分。"""
        item = QueueItem(_video("BW6", pages=3, author="阿婆主"), page_index=2)
        self.assertEqual(item.subtitle, "阿婆主 · P2")

    def test_key_uses_bvid_and_page_index(self) -> None:
        """缩略标识必须同时含 bvid 与分P序号,否则多P会串歌。"""
        item = QueueItem(_video("BW7", pages=3), page_index=3)
        self.assertEqual(item.key, ("BW7", 3))


class TestQueueBasics(unittest.TestCase):
    """空队列、整体替换、跳转与统计。"""

    def test_empty_queue_has_no_current(self) -> None:
        """空队列没有当前项,前进/后退都返回 None 而不是抛异常。"""
        queue = PlayQueue(rng=random.Random(0))
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.current)
        self.assertEqual(queue.current_index, -1)
        self.assertIsNone(queue.next_item())
        self.assertIsNone(queue.previous_item())

    def test_append_to_empty_queue_becomes_current(self) -> None:
        """给空队列加歌后,新项要直接成为当前项,否则会出现"有歌但没在播"。"""
        queue = PlayQueue(rng=random.Random(0))
        item = QueueItem(_video("A"))
        queue.append(item)
        self.assertIs(queue.current, item)

    def test_replace_starts_at_given_index(self) -> None:
        """双击搜索结果:整个结果列表成为队列,从双击的那一行开始播。"""
        queue = PlayQueue(rng=random.Random(0))
        items = [QueueItem(_video(name)) for name in ("A", "B", "C")]
        self.assertIs(queue.replace(items, start=2), items[2])
        self.assertEqual(_ids(queue), ["A", "B", "C"])
        self.assertEqual(queue.current.video.bvid, "C")

    def test_replace_clamps_out_of_range_start(self) -> None:
        """start 越界要被夹到两端,而不是让游标指到队列外面。"""
        queue = PlayQueue(rng=random.Random(0))
        items = [QueueItem(_video(name)) for name in ("A", "B")]
        self.assertIs(queue.replace(items, start=99), items[-1])
        self.assertIs(queue.replace(items, start=-5), items[0])

    def test_replace_with_nothing_empties_queue(self) -> None:
        """用空列表替换等于清空,当前项必须复位。"""
        queue = _queue("A", "B")
        self.assertIsNone(queue.replace([]))
        self.assertIsNone(queue.current)
        self.assertEqual(queue.current_index, -1)

    def test_replace_resets_previous_content(self) -> None:
        """替换是整体换内容,旧队列不能有残留。"""
        queue = _queue("A", "B", "C")
        queue.jump_to(2)
        queue.replace([QueueItem(_video("X")), QueueItem(_video("Y"))], start=1)
        self.assertEqual(_ids(queue), ["X", "Y"])
        self.assertEqual(queue.current.video.bvid, "Y")

    def test_jump_to_out_of_range_keeps_cursor(self) -> None:
        """越界跳转返回 None 且不改动游标(界面点到空白行不该改变播放对象)。"""
        queue = _queue("A", "B")
        before = queue.current
        self.assertIsNone(queue.jump_to(5))
        self.assertIsNone(queue.jump_to(-1))
        self.assertIs(queue.current, before)

    def test_jump_to_updates_current_index(self) -> None:
        """跳转后界面高亮位置要跟着走。"""
        queue = _queue("A", "B", "C")
        queue.jump_to(2)
        self.assertEqual(queue.current_index, 2)

    def test_clear_empties_everything(self) -> None:
        """清空后队列为空且游标复位。"""
        queue = _queue("A", "B")
        queue.clear()
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.current)
        self.assertIsNone(queue.next_item())

    def test_iteration_follows_insertion_order(self) -> None:
        """遍历队列按插入顺序,界面列表直接照它渲染。"""
        queue = _queue("A", "B", "C")
        self.assertEqual([item.video.bvid for item in queue], ["A", "B", "C"])


class TestSequenceMode(unittest.TestCase):
    """顺序播放:走到末项就停。"""

    def test_walks_to_the_end_then_stops(self) -> None:
        """末项之后手动与自动前进都返回 None,调用方据此停止播放。"""
        queue = _queue("A", "B", "C")
        self.assertEqual(queue.current.video.bvid, "A")
        self.assertEqual(queue.next_item().video.bvid, "B")
        self.assertEqual(queue.next_item().video.bvid, "C")
        self.assertIsNone(queue.next_item())
        self.assertIsNone(queue.next_item(auto=True))

    def test_previous_at_first_item_returns_none(self) -> None:
        """不循环时第一项没有上一首。"""
        queue = _queue("A", "B")
        self.assertIsNone(queue.previous_item())

    def test_previous_walks_back(self) -> None:
        """后退要能回到上一项。"""
        queue = _queue("A", "B", "C")
        queue.jump_to(2)
        self.assertEqual(queue.previous_item().video.bvid, "B")


class TestRepeatAllMode(unittest.TestCase):
    """列表循环:末项之后回到第一项。"""

    def test_wraps_at_the_end(self) -> None:
        """末项之后回到第一项。"""
        queue = _queue("A", "B", mode=PlayMode.REPEAT_ALL)
        self.assertEqual(queue.next_item().video.bvid, "B")
        self.assertEqual(queue.next_item().video.bvid, "A")

    def test_previous_wraps_to_last(self) -> None:
        """第一项再后退要绕到末项,而不是停住。"""
        queue = _queue("A", "B", mode=PlayMode.REPEAT_ALL)
        self.assertEqual(queue.previous_item().video.bvid, "B")


class TestRepeatOneMode(unittest.TestCase):
    """单曲循环:只影响自动前进,不影响手动切歌。"""

    def test_auto_advance_repeats_current(self) -> None:
        """自然播完要重播同一项(这就是单曲循环的定义)。"""
        queue = _queue("A", "B", mode=PlayMode.REPEAT_ONE)
        current = queue.current
        self.assertIs(queue.next_item(auto=True), current)
        self.assertIs(queue.current, current)

    def test_manual_next_still_changes_track(self) -> None:
        """手点"下一首"必须真的换歌,否则按钮看起来像坏了。"""
        queue = _queue("A", "B", mode=PlayMode.REPEAT_ONE)
        self.assertEqual(queue.next_item().video.bvid, "B")

    def test_manual_previous_still_changes_track(self) -> None:
        """手点"上一首"同理,不受单曲循环影响。"""
        queue = _queue("A", "B", mode=PlayMode.REPEAT_ONE)
        queue.jump_to(1)
        self.assertEqual(queue.previous_item().video.bvid, "A")


class TestShuffleMode(unittest.TestCase):
    """随机播放:播放顺序是插入顺序的一个排列,且循环不断。"""

    def test_order_is_a_permutation_of_items(self) -> None:
        """播放顺序不能凭空多出或漏掉队列项,只是次序不同。"""
        queue = _queue("A", "B", "C", "D", "E", mode=PlayMode.SHUFFLE)
        self.assertEqual(len(queue.order), 5)
        self.assertEqual({id(item) for item in queue.order}, {id(item) for item in queue.items})
        self.assertEqual(sorted(_play_ids(queue)), ["A", "B", "C", "D", "E"])

    def test_order_is_reproducible_with_injected_seed(self) -> None:
        """注入固定 seed 后顺序必须完全可复现,否则这个模式的用例无从断言。"""
        expected = list(range(5))
        random.Random(42).shuffle(expected)
        self.assertNotEqual(expected, list(range(5)))  # 顺带确认这个 seed 真的打乱了

        queue = _queue("A", "B", "C", "D", "E", mode=PlayMode.SHUFFLE)
        self.assertEqual([queue.items.index(item) for item in queue.order], expected)

    def test_never_runs_out(self) -> None:
        """随机模式是循环的:连续前进两轮也不会返回 None。"""
        queue = _queue("A", "B", "C", mode=PlayMode.SHUFFLE)
        for _ in range(len(queue) * 2):
            self.assertIsNotNone(queue.next_item(auto=True))

    def test_items_keep_insertion_order(self) -> None:
        """随机只乱播放顺序,队列内容(界面列表)保持插入顺序。"""
        queue = _queue("A", "B", "C", mode=PlayMode.SHUFFLE)
        self.assertEqual(_ids(queue), ["A", "B", "C"])

    def test_append_in_shuffle_mode_plays_last(self) -> None:
        """"加入队列"在随机模式下也应可预期:排到播放顺序末尾,而不是随机插队。"""
        queue = _queue("A", "B", mode=PlayMode.SHUFFLE)
        queue.append(QueueItem(_video("C")))
        self.assertEqual(_ids(queue), ["A", "B", "C"])
        self.assertIs(queue.order[-1], queue.items[-1])


class TestModeSwitching(unittest.TestCase):
    """切模式不能换歌,离开随机要能恢复原顺序。"""

    def test_entering_shuffle_keeps_current_item(self) -> None:
        """切到随机时必须还在同一首歌上,否则用户一按随机就被切走。"""
        queue = _queue("A", "B", "C", "D")
        queue.jump_to(1)
        current = queue.current
        queue.set_mode(PlayMode.SHUFFLE)
        self.assertIs(queue.current, current)

    def test_leaving_shuffle_restores_insertion_order(self) -> None:
        """关掉随机要恢复插入顺序,并且当前项不变。"""
        queue = _queue("A", "B", "C", "D", mode=PlayMode.SHUFFLE)
        current = queue.current
        queue.set_mode(PlayMode.SEQUENCE)
        self.assertEqual(_play_ids(queue), ["A", "B", "C", "D"])
        self.assertIs(queue.current, current)

    def test_setting_same_mode_is_a_noop(self) -> None:
        """重复设置同一模式不能重新洗牌,否则按一下随机就换一次顺序。"""
        queue = _queue("A", "B", "C", mode=PlayMode.SHUFFLE)
        before = _play_ids(queue)
        queue.set_mode(PlayMode.SHUFFLE)
        self.assertEqual(_play_ids(queue), before)

    def test_unknown_mode_raises(self) -> None:
        """非法模式要立刻报错,而不是被默默接受成某个默认值。"""
        queue = _queue("A")
        with self.assertRaises(ValueError):
            queue.set_mode("not-a-mode")


class TestQueueMutation(unittest.TestCase):
    """增删项时游标与两份顺序的落点。"""

    def test_insert_next_becomes_the_next_item(self) -> None:
        """插队项只改播放顺序,插入顺序里它仍在末尾。"""
        queue = _queue("A", "B", "C")
        extra = QueueItem(_video("X"))
        queue.insert_next(extra)
        self.assertEqual(_ids(queue), ["A", "B", "C", "X"])
        self.assertEqual(_play_ids(queue), ["A", "X", "B", "C"])
        self.assertIs(queue.next_item(), extra)

    def test_insert_next_on_empty_queue_enqueues_once(self) -> None:
        """空队列没有当前项,此时必须退化成 append 且**只能入队一次**。"""
        queue = PlayQueue(rng=random.Random(0))
        item = QueueItem(_video("A"))
        queue.insert_next(item)
        self.assertEqual(len(queue), 1)
        self.assertIs(queue.current, item)

    def test_append_keeps_current(self) -> None:
        """追加不能影响正在播的那一首。"""
        queue = _queue("A", "B")
        queue.append(QueueItem(_video("C")))
        self.assertEqual(queue.current.video.bvid, "A")
        self.assertEqual(_ids(queue), ["A", "B", "C"])

    def test_remove_before_current_keeps_current(self) -> None:
        """删掉当前项之前的项,当前项必须还是原来那首。"""
        queue = _queue("A", "B", "C")
        queue.jump_to(2)
        self.assertEqual(queue.remove_at(0).video.bvid, "A")
        self.assertEqual(queue.current.video.bvid, "C")
        self.assertEqual(queue.current_index, 1)

    def test_remove_current_promotes_next(self) -> None:
        """删掉正在播的项时,原本的下一项自动顶上成为当前项。"""
        queue = _queue("A", "B", "C")
        queue.jump_to(1)
        queue.remove_at(1)
        self.assertEqual(queue.current.video.bvid, "C")

    def test_remove_last_current_item_moves_cursor_back(self) -> None:
        """删掉作为末项的当前项时,游标要退到新的末项,不能越界。"""
        queue = _queue("A", "B", "C")
        queue.jump_to(2)
        queue.remove_at(2)
        self.assertEqual(queue.current.video.bvid, "B")
        self.assertEqual(queue.current_index, 1)
        # B 现在是末项,顺序模式下没有下一首 —— 游标越界的话这里会抛 IndexError
        self.assertIsNone(queue.next_item())

    def test_remove_only_item_empties_queue(self) -> None:
        """删光所有项后回到空队列状态。"""
        queue = _queue("A")
        queue.remove_at(0)
        self.assertIsNone(queue.current)
        self.assertEqual(queue.current_index, -1)
        self.assertIsNone(queue.next_item())

    def test_remove_out_of_range_returns_none(self) -> None:
        """越界删除返回 None 且不改动队列。"""
        queue = _queue("A", "B")
        self.assertIsNone(queue.remove_at(2))
        self.assertIsNone(queue.remove_at(-1))
        self.assertEqual(_ids(queue), ["A", "B"])

    def test_remove_in_shuffle_mode_keeps_both_orders_in_sync(self) -> None:
        """随机模式下两份顺序必须同步增减,否则播放顺序会残留已删除的项。"""
        queue = _queue("A", "B", "C", "D", mode=PlayMode.SHUFFLE)
        removed = queue.items[1]
        queue.remove_at(1)
        self.assertNotIn(removed, queue.items)
        self.assertNotIn(removed, queue.order)
        self.assertEqual(len(queue.order), 3)


if __name__ == "__main__":
    unittest.main()
