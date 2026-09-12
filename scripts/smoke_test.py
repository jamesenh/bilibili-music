"""端到端冒烟测试:搜索 -> 分P -> 音轨 -> 下载缓存 -> 真实播放。

用法::

    uv run python scripts/smoke_test.py                    # 默认 Qt 后端
    uv run python scripts/smoke_test.py --backend urllib    # 换 urllib 后端
    uv run python scripts/smoke_test.py --no-play           # 跳过播放验证
    uv run python scripts/smoke_test.py --both              # 两个后端各跑一遍

这个脚本不进单元测试套件,它是"整条链路是否真的通"的手工验证工具。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _helpers import ensure_app, make_backend, wait_for, wait_until  # noqa: E402

from bilibili_music.api.bilibili import BilibiliClient  # noqa: E402
from bilibili_music.audio.resolver import AudioResolver  # noqa: E402
from bilibili_music.core.cache import AudioCache  # noqa: E402
from bilibili_music.core.models import format_count  # noqa: E402

#: 默认搜索关键字。选周杰伦是因为结果里几乎必然含多P合集 —— 而多P才是
#: B站音乐区的典型形态,单P视频验证不到"分P切换 / 自动下一首"这条关键路径。
DEFAULT_KEYWORD = "周杰伦 MV"


def step(label: str) -> None:
    """打印一个步骤小标题,让长输出在终端里可扫读。

    Args:
        label: 步骤名,如 ``"1. 搜索「周杰伦 MV」"``。
    """
    print(f"\n=== {label} ===", flush=True)


class Reporter:
    """收集并汇总一次冒烟测试的断言结果。

    刻意不抛异常而是**记下来继续跑**:冒烟测试的价值在于一次运行就暴露出
    "哪几步坏了",中途抛异常会让后面的步骤全部跑不到。
    """

    def __init__(self) -> None:
        """建一个空的失败列表。"""
        self.failures: list[str] = []

    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        """打印一条 ``[OK]`` / ``[FAIL]`` 并记录失败项。

        Args:
            label: 断言名,同时作为失败列表里的条目。
            condition: 断言结果。
            detail: 可选的补充信息(耗时、数量等),非空时跟在横线后面打印。
        """
        mark = "OK  " if condition else "FAIL"
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)


def run_backend(kind: str, args) -> int:
    """对指定后端完整跑一遍:搜索 -> 分P -> 音轨 -> 下载 -> 播放。

    Args:
        kind: 后端名(``"qt"`` / ``"urllib"``)。
        args: ``argparse`` 解析出的参数,用到 ``keyword`` / ``keep_cache`` / ``no_play``。

    Returns:
        进程退出码:``0`` 表示全部断言通过,``1`` 表示有失败项
        (搜索无结果、详情取不到 cid、音轨为空时也会提前返回 ``1``)。
    """
    print(f"\n{'#' * 62}")
    print(f"# 后端: {kind}")
    print(f"{'#' * 62}")

    app = ensure_app()
    backend = make_backend(kind)
    client = BilibiliClient(backend)
    cache = AudioCache()
    resolver = AudioResolver(client, cache)
    report = Reporter()
    print(f"缓存目录: {cache.root}")

    if not args.keep_cache:
        # 清空缓存,确保下面确实走了一遍"下载 -> 落盘 -> 命中缓存"的完整路径。
        # 否则缓存早就存在,下载与进度回调这两件事根本没被验证到。
        removed = cache.clear()
        print(f"已清空缓存({removed} 个文件),本次将真实下载")

    # ---------------------------------------------------------- 1. 搜索
    step(f"1. 搜索「{args.keyword}」")
    result = wait_for(
        lambda ok, err: client.search_video(args.keyword, on_success=ok, on_error=err),
        timeout=60,
        what="搜索",
    )
    print(f"命中 {result.total} 条,本页返回 {len(result.videos)} 条")
    report.ok("搜索返回结果", bool(result.videos))
    if not result.videos:
        return 1
    for video in result.videos[:5]:
        print(
            f"  [{video.bvid}] {video.title[:40]:<40} "
            f"{video.author[:10]:<10} {video.duration_text:>9} "
            f"{format_count(video.play_count):>9}"
        )

    # 优先挑一个多P合集,它才是 B站音乐区的典型形态
    target = next((v for v in result.videos if "P】" in v.title or "合集" in v.title), result.videos[0])
    print(f"\n选中: {target.title} / {target.bvid}")

    # ---------------------------------------------------------- 2. 详情
    step("2. 补全视频详情(含分P)")
    detail = wait_for(
        lambda ok, err: client.fetch_video(target.bvid, on_success=ok, on_error=err),
        timeout=40,
        what="详情",
    )
    print(f"aid={detail.aid} cid={detail.cid}")
    print(f"视频级 duration={detail.duration}s  (= 所有分P之和,不能当单曲时长)")
    print(f"分P数量: {detail.part_count}  是合集: {detail.is_multipart}")
    for page in detail.pages[:3]:
        print(f"    P{page.index} cid={page.cid} {page.duration_text:>9}  {page.title[:38]}")
    if detail.is_multipart and detail.part_count > 3:
        print(f"    … 还有 {detail.part_count - 3} 个分P")
    report.ok("拿到 cid", bool(detail.cid))
    report.ok("拿到分P列表", bool(detail.pages))

    # 缓存命中验证:第二次应当不发请求直接返回。
    # 注意缓存命中是"同步回调",所以计时放在调用前后、不经过事件循环。
    t0 = time.monotonic()
    again_detail = wait_for(
        lambda ok, err: client.fetch_video(target.bvid, on_success=ok, on_error=err),
        timeout=10,
        what="详情(缓存)",
    )
    cache_ms = (time.monotonic() - t0) * 1000
    report.ok("详情命中缓存(瞬时)", cache_ms < 100, f"{cache_ms:.1f} ms")
    report.ok("缓存返回同一对象", again_detail is detail)

    # ---------------------------------------------------------- 3. 音轨
    step("3. 解析 DASH 音轨(用第 1P 的 cid)")
    first = detail.first_page()
    assert first is not None
    print(f"  目标分P: P{first.index} {first.title[:38]} ({first.duration_text})")
    tracks = wait_for(
        lambda ok, err: client.fetch_audio_tracks(
            detail.bvid, first.cid, on_success=ok, on_error=err
        ),
        timeout=40,
        what="音轨",
    )
    for track in tracks:
        host = track.url.split("/")[2]
        print(
            f"  id={track.quality_id} {track.label:>10} {track.kbps:>5} kbps "
            f"codec={track.codec:<12} host={host}"
        )
    report.ok("解析出音轨", bool(tracks), f"{len(tracks)} 条")

    # ---------------------------------------------------------- 4. 下载
    step("4. 下载第 1P 到缓存")
    started = time.monotonic()
    progress_seen = {"count": 0, "max": 0}

    def on_progress(done: int, total: int) -> None:
        """记录进度回调次数与最大已收字节数。

        这两个量是后面断言"确实下过东西"的依据:缓存命中时回调一次都不该触发。
        """
        progress_seen["count"] += 1
        progress_seen["max"] = max(progress_seen["max"], done)

    def start_resolve(ok, err):
        """``wait_for`` 需要的 ``start`` 函数:发起一次解析。

        同一个闭包被用两次(首次下载与二次缓存命中),这样两次的入参完全一致,
        差异只可能来自缓存状态。
        """
        resolver.resolve(
            detail,
            page_index=1,
            on_success=ok,
            on_error=err,
            on_progress=on_progress,
        )

    audio = wait_for(start_resolve, timeout=180, what="下载")
    elapsed = time.monotonic() - started
    path = audio.path
    size = path.stat().st_size
    print(f"  完成: {path.name}  {size / 1048576:.2f} MB  用时 {elapsed:.1f}s")
    if elapsed > 0.05:
        print(f"  平均速度: {size / elapsed / 1048576:.2f} MB/s")
    print(f"  分P时长 {audio.page.duration_text} · 音质 {audio.track.label}")
    report.ok("文件已落盘", path.exists() and size > 0)
    report.ok("收到进度回调", progress_seen["count"] > 0, f"{progress_seen['count']} 次")
    report.ok("进度推进到完整大小", progress_seen["max"] > 0)
    report.ok(
        "分P时长不等于视频总时长",
        audio.page.duration < detail.duration or not detail.is_multipart,
    )

    # 二次解析应命中缓存(同步回调,无需等事件循环)
    t0 = time.monotonic()
    before_progress = progress_seen["count"]
    again = wait_for(start_resolve, timeout=30, what="下载(缓存)")
    cache_ms = (time.monotonic() - t0) * 1000
    report.ok("音频命中缓存", again.path == path, f"{cache_ms:.0f} ms")
    report.ok(
        "缓存命中未再次下载",
        progress_seen["count"] == before_progress,
        f"进度回调 {before_progress} -> {progress_seen['count']}",
    )

    # ---------------------------------------------------------- 5. 播放
    if args.no_play:
        step("5. 播放验证(已跳过)")
    else:
        step("5. 真实播放验证(QMediaPlayer)")
        from PySide6.QtMultimedia import QMediaDevices

        from bilibili_music.audio.player import PlayerController

        device = QMediaDevices.defaultAudioOutput()
        print(f"  默认音频输出: {device.description() or '(无设备)'}")
        player = PlayerController()
        observed = {"max_position": 0, "playing": False, "error": None}
        player.state_changed.connect(lambda p: observed.__setitem__("playing", p))
        player.error_occurred.connect(lambda m: observed.__setitem__("error", m))
        player.position_changed.connect(
            lambda pos, _d: observed.__setitem__(
                "max_position", max(observed["max_position"], pos)
            )
        )
        player.load(path, autoplay=True)
        wait_until(lambda: observed["max_position"] > 800, timeout=8.0)
        duration_ms = player.duration_ms
        print(f"  进入播放状态: {observed['playing']}")
        print(f"  播放位置推进到: {observed['max_position']} ms")
        print(f"  播放器识别时长: {duration_ms} ms ({player.format_ms(duration_ms)})")
        print(f"  分P元数据时长: {audio.page.duration}s ({audio.page.duration_text})")
        if observed["error"]:
            print(f"  !! 播放器报错: {observed['error']}")
        player.stop()
        report.ok("播放位置推进", observed["max_position"] > 800)
        report.ok(
            "播放时长与分P元数据一致(±2s)",
            abs(duration_ms / 1000 - audio.page.duration) <= 2,
        )

    # ---------------------------------------------------------- 清理
    client.close()

    if report.failures:
        print(f"\n[FAIL] {kind} 后端有 {len(report.failures)} 项失败: {report.failures}")
        return 1
    print(f"\n[PASS] {kind} 后端端到端链路全部打通:搜索 -> 分P -> 音轨 -> 缓存 -> 播放")
    return 0


def main() -> int:
    """解析命令行参数,按 ``--backend`` / ``--both`` 逐个跑后端。

    Returns:
        进程退出码:所有后端都通过才是 ``0``。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-play", action="store_true", help="跳过播放器验证")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD)
    parser.add_argument("--backend", default="qt", choices=["qt", "urllib"])
    parser.add_argument("--both", action="store_true", help="两个后端各跑一遍")
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="保留已有缓存(默认会清空,以确保真实走一遍下载路径)",
    )
    args = parser.parse_args()

    # 播放验证需要真实音频设备;不设 offscreen,以贴近真实运行环境
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    backends = ["qt", "urllib"] if args.both else [args.backend]
    results = {kind: run_backend(kind, args) for kind in backends}
    print("\n" + "=" * 62)
    for kind, code in results.items():
        print(f"  {kind:<8} -> {'PASS' if code == 0 else 'FAIL'}")
    return 0 if all(code == 0 for code in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
