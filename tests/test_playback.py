"""播放编排的单元测试:自动前进、解析回调、换音质续播与模式切换。

**不触网、不构造真实 ``QMediaPlayer``**。编排层收的是注入进来的解析器与播放器,
用例塞进两个替身,于是"播完自动前进""切歌时旧回调不许污染新状态""换音质保留进度"
这些逻辑都能在沙箱里直接断言。``QObject`` + 信号在没有 ``QApplication`` 时可用,
所以连 Qt 应用实例都不需要创建。
"""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QObject, Signal  # noqa: E402

from bilibili_music.api.bilibili import track_from_quality  # noqa: E402
from bilibili_music.audio.playback import (  # noqa: E402
    RESUME_TAIL_MARGIN_MS,
    PlaybackController,
)
from bilibili_music.audio.resolver import ResolvedAudio  # noqa: E402
from bilibili_music.core.models import Page, Video  # noqa: E402
from bilibili_music.core.queue import PlayMode, QueueItem  # noqa: E402


# ====================================================================== 替身


@dataclass(slots=True)
class _Pending:
    """一次解析请求的回调组,交给用例自己决定什么时候触发。"""

    on_success: Callable[[ResolvedAudio], None]
    on_error: Callable[[Exception], None]
    on_progress: Callable[[int, int], None] | None


class _FakeResolver:
    """解析器替身:记录每次请求,并把回调攒下来供用例手动触发。"""

    def __init__(self) -> None:
        """建一个没有任何在飞请求的替身。"""
        self.calls: list[dict] = []
        self.cancel_count = 0
        self._pending: _Pending | None = None

    def resolve(
        self,
        video: Video,
        *,
        page_index: int = 1,
        quality_id: int | None = None,
        on_success: Callable[[ResolvedAudio], None],
        on_error: Callable[[Exception], None],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """记录一次解析请求(不发任何网络动作)。"""
        self.calls.append(
            {"video": video, "page_index": page_index, "quality_id": quality_id}
        )
        self._pending = _Pending(on_success, on_error, on_progress)

    def cancel(self) -> None:
        """记录一次取消,并丢弃待触发的回调。"""
        self.cancel_count += 1
        self._pending = None

    # ---------------------------------------------------------- 用例驱动口

    def succeed(self, resolved: ResolvedAudio) -> None:
        """触发最近一次请求的成功回调。"""
        assert self._pending is not None, "没有在飞的解析请求"
        self._pending.on_success(resolved)

    def fail(self, exc: Exception) -> None:
        """触发最近一次请求的失败回调。"""
        assert self._pending is not None, "没有在飞的解析请求"
        self._pending.on_error(exc)

    def report_progress(self, done: int, total: int) -> None:
        """触发最近一次请求的进度回调(没注册回调时是空操作)。"""
        assert self._pending is not None, "没有在飞的解析请求"
        if self._pending.on_progress is not None:
            self._pending.on_progress(done, total)

    @property
    def last_call(self) -> dict:
        """最近一次解析请求的参数。"""
        assert self.calls, "还没有任何解析请求"
        return self.calls[-1]


class _FakePlayer(QObject):
    """播放器替身:提供与 ``PlayerController`` 同名的那四个信号。"""

    state_changed = Signal(bool)
    position_changed = Signal(int, int)
    track_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self) -> None:
        """建一个"什么都没加载"的播放器替身。"""
        super().__init__()
        self.loaded: list[Path] = []
        self.autoplay_flags: list[bool] = []
        self.seeks: list[int] = []
        self.volumes: list[float] = []
        self.stop_count = 0
        self.play_count = 0
        self.pause_count = 0
        self.toggle_count = 0
        self._position_ms = 0
        self._is_playing = False

    @property
    def is_playing(self) -> bool:
        """是否处于播放状态。"""
        return self._is_playing

    @property
    def position_ms(self) -> int:
        """当前播放位置;用例直接改这个字段来模拟"播到一半"。"""
        return self._position_ms

    def load(self, path: Path, *, autoplay: bool = True) -> None:
        """记录一次加载。"""
        self.loaded.append(Path(path))
        self.autoplay_flags.append(autoplay)
        self._is_playing = autoplay

    def play(self) -> None:
        """记录一次继续播放。"""
        self.play_count += 1
        self._is_playing = True

    def pause(self) -> None:
        """记录一次暂停。"""
        self.pause_count += 1
        self._is_playing = False

    def toggle(self) -> None:
        """记录一次播放/暂停切换。"""
        self.toggle_count += 1
        self._is_playing = not self._is_playing

    def stop(self) -> None:
        """记录一次停止。"""
        self.stop_count += 1
        self._is_playing = False

    def seek(self, position_ms: int) -> None:
        """记录一次跳转。"""
        self.seeks.append(int(position_ms))

    def set_volume(self, volume: float) -> None:
        """记录一次音量设置。"""
        self.volumes.append(float(volume))


# ====================================================================== 工具


def _video(bvid: str, *, pages: int = 1) -> Video:
    """造一个带分P的视频样本(纯内存)。"""
    return Video(
        bvid=bvid,
        title=f"视频{bvid}",
        author="某UP",
        pages=[
            Page(index=i + 1, cid=1000 + i, title=f"第{i + 1}首", duration=180)
            for i in range(pages)
        ],
    )


def _items(*bvids: str) -> list[QueueItem]:
    """按 bvid 序列造队列项(每项取第 1P)。"""
    return [QueueItem(_video(bvid)) for bvid in bvids]


def _resolved(item: QueueItem, *, quality_id: int = 30280) -> ResolvedAudio:
    """按队列项造一份解析结果,路径是假的(不会被真正读取)。"""
    page = item.video.page(item.page_index)
    assert page is not None
    return ResolvedAudio(
        video=item.video,
        page=page,
        track=track_from_quality(quality_id),
        path=Path(f"C:/fake/{item.video.bvid}.m4a"),
    )


def _record(signal) -> list[tuple]:
    """把信号参数按元组收集进列表,便于断言。"""
    got: list[tuple] = []
    signal.connect(lambda *args: got.append(args))
    return got


class _PlaybackCase(unittest.TestCase):
    """装好替身与编排器的公共基类(基类本身没有用例)。"""

    def setUp(self) -> None:
        """建一份干净的替身 + 编排器。"""
        self.resolver = _FakeResolver()
        self.player = _FakePlayer()
        self.playback = PlaybackController(
            self.resolver,  # type: ignore[arg-type]
            self.player,  # type: ignore[arg-type]
        )


# ====================================================================== 用例


class TestQueueDrivenPlayback(_PlaybackCase):
    """队列操作如何驱动播放。"""

    def test_play_queue_starts_from_given_row(self) -> None:
        """双击搜索结果:整个列表成为队列,从双击那一行开始解析。"""
        items = _items("A", "B", "C")
        self.playback.play_queue(items, start=1)
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")
        self.assertIs(self.playback.current, items[1])

    def test_play_queue_emits_queue_changed_and_track_changed(self) -> None:
        """替换队列要同时通知"列表变了"和"当前项变了"。"""
        queue_events = _record(self.playback.queue_changed)
        track_events = _record(self.playback.track_changed)
        items = _items("A")
        self.playback.play_queue(items)
        self.assertEqual(len(queue_events), 1)
        self.assertEqual(track_events, [(items[0],)])

    def test_play_queue_with_nothing_stops_and_cancels(self) -> None:
        """空列表等于清空:必须取消在飞解析,否则它回来后会自己响起来。"""
        self.playback.play_queue([])
        self.assertEqual(self.resolver.cancel_count, 1)
        self.assertEqual(self.player.stop_count, 1)
        self.assertIsNone(self.playback.current)

    def test_enqueue_on_empty_queue_starts_playing(self) -> None:
        """"加入队列"在空队列上应当直接开播,而不是毫无反应。"""
        item = _items("A")[0]
        self.playback.enqueue(item)
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertIs(self.playback.current, item)

    def test_enqueue_on_non_empty_queue_keeps_current(self) -> None:
        """队列里已经有歌时,追加不能打断当前播放。"""
        self.playback.play_queue(_items("A"))
        self.playback.enqueue(_items("B")[0])
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertEqual(self.playback.current.video.bvid, "A")

    def test_enqueue_next_plays_after_current(self) -> None:
        """"下一首播放"插进来的项要在当前项播完后立刻接上。"""
        self.playback.play_queue(_items("A", "B"))
        self.playback.enqueue_next(_items("X")[0])
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["video"].bvid, "X")

    def test_jump_to_out_of_range_returns_false(self) -> None:
        """越界跳转不动当前播放。"""
        self.playback.play_queue(_items("A", "B"))
        self.assertFalse(self.playback.jump_to(9))
        self.assertEqual(len(self.resolver.calls), 1)

    def test_jump_to_plays_target(self) -> None:
        """跳转要真的换歌。"""
        items = _items("A", "B")
        self.playback.play_queue(items)
        self.assertTrue(self.playback.jump_to(1))
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")

    def test_remove_current_starts_promoted_item(self) -> None:
        """删掉正在播的那一项后,接上顶上来的那一首 —— 否则界面与实际出声不一致。"""
        self.playback.play_queue(_items("A", "B", "C"))
        self.playback.remove_at(0)
        self.assertEqual(self.playback.current.video.bvid, "B")
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")

    def test_remove_other_item_keeps_playing(self) -> None:
        """删掉非当前项不能打断播放。"""
        self.playback.play_queue(_items("A", "B"))
        self.playback.remove_at(1)
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertEqual(self.playback.current.video.bvid, "A")

    def test_remove_last_remaining_item_stops(self) -> None:
        """把队列删空时停止播放并通知界面。"""
        stopped = _record(self.playback.stopped)
        self.playback.play_queue(_items("A"))
        self.playback.remove_at(0)
        self.assertEqual(self.player.stop_count, 1)
        self.assertEqual(len(stopped), 1)

    def test_clear_cancels_and_stops(self) -> None:
        """清空要同时取消在飞解析、停播放器、通知界面。"""
        stopped = _record(self.playback.stopped)
        self.playback.play_queue(_items("A", "B"))
        self.playback.clear()
        self.assertEqual(self.resolver.cancel_count, 1)
        self.assertEqual(self.player.stop_count, 1)
        self.assertEqual(len(stopped), 1)
        self.assertIsNone(self.playback.current)

    def test_stop_keeps_queue(self) -> None:
        """停止播放只停声音,队列与当前项要留着。"""
        items = _items("A", "B")
        self.playback.play_queue(items)
        self.playback.stop()
        self.assertEqual(self.player.stop_count, 1)
        self.assertEqual(self.resolver.cancel_count, 1)
        self.assertIs(self.playback.current, items[0])


class TestResolutionFlow(_PlaybackCase):
    """解析结果的落地方式。"""

    def test_success_loads_file_and_reports_audio(self) -> None:
        """解析成功要立刻加载并自动播放,同时把结果交给界面。"""
        ready = _record(self.playback.audio_ready)
        item = _items("A")[0]
        self.playback.play_queue([item])
        self.playback.resolver.succeed(_resolved(item))  # type: ignore[attr-defined]
        self.assertEqual(self.player.loaded, [Path("C:/fake/A.m4a")])
        self.assertEqual(self.player.autoplay_flags, [True])
        self.assertEqual(len(ready), 1)

    def test_failure_reports_error_without_skipping(self) -> None:
        """解析失败要报错,**不**自动跳下一首:静默跳歌会让用户莫名其妙。"""
        errors = _record(self.playback.error_occurred)
        self.playback.play_queue(_items("A", "B"))
        self.playback.resolver.fail(RuntimeError("音源没了"))  # type: ignore[attr-defined]
        self.assertEqual(len(errors), 1)
        self.assertIn("音源没了", errors[0][0])
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertEqual(self.player.loaded, [])

    def test_progress_is_forwarded(self) -> None:
        """下载进度要转发给界面(进度条靠它)。"""
        events = _record(self.playback.progress)
        self.playback.play_queue(_items("A"))
        self.playback.resolver.report_progress(1024, 4096)  # type: ignore[attr-defined]
        self.assertEqual(events, [(1024, 4096)])


class TestAutoAdvance(_PlaybackCase):
    """播完自动前进(原先散在 UI 里的逻辑)。"""

    def test_finish_advances_to_next_item(self) -> None:
        """顺序模式下自然播完要接着解析下一首。"""
        self.playback.play_queue(_items("A", "B"))
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")

    def test_finish_at_end_stops_and_notifies(self) -> None:
        """顺序模式播完末项要停下并通知界面,而不是报错或循环。"""
        stopped = _record(self.playback.stopped)
        self.playback.play_queue(_items("A"))
        self.playback._on_track_finished()
        self.assertEqual(len(stopped), 1)
        self.assertEqual(len(self.resolver.calls), 1)

    def test_repeat_one_replays_same_item(self) -> None:
        """单曲循环在自动前进时重播同一首(手动下一首仍然换歌)。"""
        items = _items("A", "B")
        self.playback.play_queue(items)
        self.playback.set_mode(PlayMode.REPEAT_ONE)
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["video"].bvid, "A")

    def test_repeat_all_wraps_to_first(self) -> None:
        """列表循环在末项之后回到第一项。"""
        self.playback.play_queue(_items("A", "B"))
        self.playback.set_mode(PlayMode.REPEAT_ALL)
        self.playback._on_track_finished()  # -> B
        self.playback._on_track_finished()  # -> 回绕到 A
        self.assertEqual(self.resolver.last_call["video"].bvid, "A")

    def test_manual_next_at_end_returns_false(self) -> None:
        """顺序模式末项手动下一首要返回 False,界面据此不做事。"""
        self.playback.play_queue(_items("A"))
        self.assertFalse(self.playback.next())
        self.assertEqual(len(self.resolver.calls), 1)

    def test_manual_previous_returns_false_at_first(self) -> None:
        """第一项手动上一首同理。"""
        self.playback.play_queue(_items("A", "B"))
        self.assertFalse(self.playback.previous())

    def test_manual_next_advances(self) -> None:
        """手动下一首要真的换歌。"""
        self.playback.play_queue(_items("A", "B"))
        self.assertTrue(self.playback.next())
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")


class TestQualitySwitch(_PlaybackCase):
    """换音质与续播位置。"""

    def test_set_quality_replays_current_with_new_quality(self) -> None:
        """换音质要带着新档位重新解析当前这一首。"""
        self.playback.play_queue(_items("A", "B"))
        self.playback.set_quality(30216)
        self.assertEqual(self.resolver.last_call["quality_id"], 30216)
        self.assertEqual(self.resolver.last_call["video"].bvid, "A")

    def test_set_quality_twice_is_a_noop(self) -> None:
        """重复设置同一档位不该重新解析(否则界面点两下就重下一遍)。"""
        self.playback.play_queue(_items("A"))
        self.playback.set_quality(30216)
        calls = len(self.resolver.calls)
        self.playback.set_quality(30216)
        self.assertEqual(len(self.resolver.calls), calls)

    def test_quality_persists_for_later_tracks(self) -> None:
        """选定档位应当对后续曲目继续生效。"""
        self.playback.set_quality(30216)
        self.playback.play_queue(_items("A"))
        self.assertEqual(self.resolver.last_call["quality_id"], 30216)

    def test_resume_position_is_seeked_after_duration_is_known(self) -> None:
        """续播必须等新媒体时长就绪后再 seek —— 加载完立刻 seek 会被忽略。"""
        self.playback.play_queue(_items("A"))
        self.player._position_ms = 30_000
        self.playback.set_quality(30216)
        self.assertEqual(self.player.seeks, [])  # 时长未知时不许乱 seek
        self.player.position_changed.emit(0, 200_000)
        self.assertEqual(self.player.seeks, [30_000])

    def test_resume_position_is_clamped_to_new_duration(self) -> None:
        """新档位更短时,续播位置要钳到时长之内,否则会瞬间播完并跳歌。"""
        self.playback.play_queue(_items("A"))
        self.player._position_ms = 300_000
        self.playback.set_quality(30216)
        self.player.position_changed.emit(0, 100_000)
        self.assertEqual(self.player.seeks, [100_000 - RESUME_TAIL_MARGIN_MS])

    def test_no_resume_when_starting_from_zero(self) -> None:
        """正常换歌是"从头播",不能残留上一首的续播位置。"""
        self.playback.play_queue(_items("A", "B"))
        self.playback.next()
        self.player.position_changed.emit(0, 200_000)
        self.assertEqual(self.player.seeks, [])

    def test_resume_happens_once_only(self) -> None:
        """续播只做一次,否则每次位置信号回来都会把用户拖回去。"""
        self.playback.play_queue(_items("A"))
        self.player._position_ms = 30_000
        self.playback.set_quality(30216)
        self.player.position_changed.emit(0, 200_000)
        self.player.position_changed.emit(35_000, 200_000)
        self.assertEqual(self.player.seeks, [30_000])

    def test_manual_seek_discards_pending_resume(self) -> None:
        """用户在续播生效前手动拖了进度,就不该再被拽回去。"""
        self.playback.play_queue(_items("A"))
        self.player._position_ms = 30_000
        self.playback.set_quality(30216)
        self.playback.seek(5_000)
        self.player.position_changed.emit(5_000, 200_000)
        self.assertEqual(self.player.seeks, [5_000])


class TestModesAndForwarding(_PlaybackCase):
    """模式切换与信号转发。"""

    def test_set_mode_notifies_and_keeps_current(self) -> None:
        """切模式只影响下一次前进,不能把正在播的换掉。"""
        modes = _record(self.playback.mode_changed)
        self.playback.play_queue(_items("A", "B"))
        self.playback.set_mode(PlayMode.SHUFFLE)
        self.assertEqual(modes, [(PlayMode.SHUFFLE,)])
        self.assertEqual(len(self.resolver.calls), 1)
        self.assertEqual(self.playback.current.video.bvid, "A")

    def test_set_mode_with_unknown_value_raises(self) -> None:
        """非法模式要立刻报错,而不是默默接受。"""
        with self.assertRaises(ValueError):
            self.playback.set_mode("not-a-mode")

    def test_state_changed_is_forwarded(self) -> None:
        """播放状态要转发给界面(播放按钮图标靠它)。"""
        states = _record(self.playback.state_changed)
        self.player.state_changed.emit(True)
        self.assertEqual(states, [(True,)])

    def test_player_error_is_forwarded(self) -> None:
        """播放器错误要转发给界面。"""
        errors = _record(self.playback.error_occurred)
        self.player.error_occurred.emit("解码失败")
        self.assertEqual(errors, [("解码失败",)])

    def test_position_changed_is_forwarded(self) -> None:
        """位置要原样转发,界面进度条靠它。"""
        events = _record(self.playback.position_changed)
        self.player.position_changed.emit(1_000, 200_000)
        self.assertEqual(events, [(1_000, 200_000)])

    def test_pause_and_resume_delegate_to_player(self) -> None:
        """暂停/继续直接转给播放器,中间层不加戏。"""
        self.playback.pause()
        self.playback.resume()
        self.playback.toggle()
        self.assertEqual(self.player.pause_count, 1)
        self.assertEqual(self.player.play_count, 1)
        self.assertEqual(self.player.toggle_count, 1)

    def test_set_volume_delegates_to_player(self) -> None:
        """音量设置直接转给播放器。"""
        self.playback.set_volume(0.5)
        self.assertEqual(self.player.volumes, [0.5])


class TestMultipartPages(_PlaybackCase):
    """多P合集内部的自动前进:合集在队列里只占一行,内部却有 N 首歌。"""

    def test_finish_advances_within_the_same_video(self) -> None:
        """合集播完一P要自动接下一P,而不是跳到队列的下一行。"""
        self.playback.play_queue([QueueItem(_video("A", pages=3))])
        self.assertEqual(self.resolver.last_call["page_index"], 1)
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["video"].bvid, "A")
        self.assertEqual(self.resolver.last_call["page_index"], 2)

    def test_finish_walks_all_pages_then_moves_on(self) -> None:
        """把合集内部走完,才轮到队列的下一行。"""
        self.playback.play_queue(
            [QueueItem(_video("A", pages=2)), QueueItem(_video("B"))]
        )
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["page_index"], 2)
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")
        self.assertEqual(self.resolver.last_call["page_index"], 1)

    def test_finish_at_last_page_stops_when_queue_ends(self) -> None:
        """合集最后一P且队列没有下一行时要停下并通知界面。"""
        stopped = _record(self.playback.stopped)
        self.playback.play_queue([QueueItem(_video("A", pages=2))])
        self.playback._on_track_finished()
        self.playback._on_track_finished()
        self.assertEqual(len(stopped), 1)

    def test_repeat_one_repeats_the_current_page(self) -> None:
        """单曲循环要重播当前**分P**,不能被"合集里还有下一P"顶掉、也不能退回 P1。"""
        self.playback.play_queue([QueueItem(_video("A", pages=3))])
        self.playback._on_track_finished()
        self.playback.set_mode(PlayMode.REPEAT_ONE)
        self.playback._on_track_finished()
        self.assertEqual(self.resolver.last_call["video"].bvid, "A")
        self.assertEqual(self.resolver.last_call["page_index"], 2)

    def test_manual_next_skips_remaining_pages(self) -> None:
        """手动"下一首"是换歌,不该在合集内部一P一P地挪。"""
        self.playback.play_queue(
            [QueueItem(_video("A", pages=3)), QueueItem(_video("B"))]
        )
        self.assertTrue(self.playback.next())
        self.assertEqual(self.resolver.last_call["video"].bvid, "B")

    def test_jump_back_resets_the_page(self) -> None:
        """跳回某个队列项要从它自己的分P重新开始,不能残留上一轮的分P。"""
        self.playback.play_queue([QueueItem(_video("A", pages=3))])
        self.playback._on_track_finished()
        self.playback.jump_to(0)
        self.assertEqual(self.resolver.last_call["page_index"], 1)

    def test_quality_switch_keeps_the_current_page(self) -> None:
        """合集播到第 2P 时换音质,必须还在第 2P(这里曾把用户踢回 P1)。"""
        self.playback.play_queue([QueueItem(_video("A", pages=3))])
        self.playback._on_track_finished()
        self.playback.set_quality(30216)
        self.assertEqual(self.resolver.last_call["quality_id"], 30216)
        self.assertEqual(self.resolver.last_call["page_index"], 2)

    def test_page_index_follows_progress_without_touching_the_item(self) -> None:
        """page_index 反映真正在播的那一P,而队列项记的入队分P保持不动。"""
        item = QueueItem(_video("A", pages=3))
        self.playback.play_queue([item])
        self.assertEqual(self.playback.page_index, 1)
        self.playback._on_track_finished()
        self.assertEqual(self.playback.page_index, 2)
        self.assertEqual(item.page_index, 1)


if __name__ == "__main__":
    unittest.main()
