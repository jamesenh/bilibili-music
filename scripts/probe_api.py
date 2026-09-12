"""B站接口可用性快速探测。

用途:接口风控策略、字段名、音质档位都可能随时变化。怀疑"昨天还能用今天不行"
时,先跑这个脚本,它能一次告诉你:会话预热是否拿到 Cookie、搜索是否被限速、
playurl 给了哪些音轨、音频 CDN 是否还能直连下载。

用法::

    uv run python scripts/probe_api.py                  # Qt 后端
    uv run python scripts/probe_api.py --backend urllib  # urllib 后端
    uv run python scripts/probe_api.py --bvid BV1GJ411x7h7
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _helpers import ensure_app, make_backend, wait_for, wait_until  # noqa: E402

from bilibili_music.api.bilibili import BilibiliClient  # noqa: E402
from bilibili_music.core.errors import BiliMusicError  # noqa: E402

#: 默认探测用的 BV 号。选它是因为它是一个已知的多P视频:
#: 单P视频验不出"视频级 cid 与分P cid 不是一回事"这个最容易踩的坑。
DEFAULT_BVID = "BV1GJ411x7h7"


def report(label: str, ok: bool, detail: str = "") -> bool:
    """打印一条 ``[OK]`` / ``[FAIL]`` 并原样返回断言结果。

    返回 ``ok`` 是为了让调用点能写成 ``if not report(...): failures += 1``,
    避免"打印了失败却忘了计数"。

    Args:
        label: 断言名。
        ok: 断言结果。
        detail: 可选的补充信息。

    Returns:
        传入的 ``ok``。
    """
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    """按顺序探测预热 / 搜索 / 详情 / 音轨 / CDN 下载五项能力。

    除"预热"外的每一项失败都只累加计数、不中断,这样一次运行能拿到完整的
    "哪几项坏了"清单 —— 接口变动时这正是最需要的信息。

    Returns:
        进程退出码:``0`` 全部正常;``1`` 有任一环节异常
        (详情取不到时提前返回,因为后面三项都以它为输入)。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--bvid", default=DEFAULT_BVID)
    parser.add_argument("--keyword", default="音乐")
    parser.add_argument("--backend", default="qt", choices=["qt", "urllib"])
    args = parser.parse_args()

    ensure_app()
    backend = make_backend(args.backend)
    client = BilibiliClient(backend)
    failures = 0
    print(f"后端: {args.backend}")

    print("\n=== 1. 会话预热(取 buvid3 / b_nut)===")
    backend.warm_up(force=True)
    # 预热是异步的,给它一点时间把 Cookie 写进 jar
    wait_until(lambda: bool(backend.cookie_names), timeout=10.0)
    names = backend.cookie_names
    if not report("主页预热", bool(names), f"cookies={names}"):
        failures += 1

    print("\n=== 2. 搜索接口(需注意 412 风控)===")
    attempts_ok = 0
    for i in range(3):
        try:
            result = wait_for(
                lambda ok, err: client.search_video(args.keyword, on_success=ok, on_error=err),
                timeout=60,
                what="搜索",
            )
            attempts_ok += 1
            print(f"  第 {i + 1} 次:code=0,返回 {len(result.videos)} 条")
        except BiliMusicError as exc:
            print(f"  第 {i + 1} 次:失败 {exc}")
        time.sleep(1.0)
    if not report("搜索", attempts_ok > 0, f"3 次中成功 {attempts_ok} 次"):
        failures += 1

    print(f"\n=== 3. 视频详情 {args.bvid} ===")
    try:
        video = wait_for(
            lambda ok, err: client.fetch_video(args.bvid, on_success=ok, on_error=err),
            timeout=40,
            what="详情",
        )
        if not report(
            "view 接口",
            bool(video.cid),
            f"cid={video.cid} 分P={video.part_count} 视频级时长={video.duration_text}",
        ):
            failures += 1
    except BiliMusicError as exc:
        report("view 接口", False, str(exc))
        print("\n详情取不到,后续步骤跳过。")
        client.close()
        return 1 if failures else 0

    print("\n=== 4. DASH 音轨 ===")
    try:
        tracks = wait_for(
            lambda ok, err: client.fetch_audio_tracks(
                video.bvid, video.cid, on_success=ok, on_error=err
            ),
            timeout=40,
            what="音轨",
        )
        for track in tracks:
            print(f"  - id={track.quality_id} {track.label} {track.kbps}kbps codec={track.codec}")
        if not report("playurl 接口", bool(tracks), f"共 {len(tracks)} 条音轨"):
            failures += 1
    except BiliMusicError as exc:
        report("playurl 接口", False, str(exc))
        client.close()
        return 1

    print("\n=== 5. 音频 CDN 直连(流式下载)===")
    from bilibili_music.api.bilibili import pick_best
    from bilibili_music.core.cache import DownloadSink

    best = pick_best(tracks)
    sink = DownloadSink(Path(__file__).parent.parent / "probe_download_tmp.m4a")
    try:
        result = wait_for(
            lambda ok, err: backend.download(
                best.url, sink=sink, on_success=ok, on_error=err
            ),
            timeout=60,
            what="CDN 下载",
        )
        size = Path(result).stat().st_size
        magic = Path(result).read_bytes()[:12]
        report("CDN 下载", size > 0, f"{size} 字节,魔数 {magic!r}")
        Path(result).unlink(missing_ok=True)
    except Exception as exc:
        report("CDN 下载", False, str(exc))
        failures += 1

    client.close()
    print()
    if failures:
        print(f"[FAIL] 有 {failures} 项异常,接口可能已变动或被限速。")
        return 1
    print("[PASS] 全部接口正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
