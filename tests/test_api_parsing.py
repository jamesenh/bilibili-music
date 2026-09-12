"""接口解析的单元测试:不触网,用手写的响应样本验证解析逻辑。

重构后解析与请求被拆开,解析变成纯函数,于是可以用固定样本把 B站的
各种边界情况钉死 —— 尤其是多P合集的 duration 语义。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.api.bilibili import (  # noqa: E402
    parse_audio_tracks,
    parse_search_result,
    parse_video,
    pick_best,
)
from bilibili_music.core.errors import ApiError, NoAudioSourceError, NetworkError  # noqa: E402
from bilibili_music.net.base import check_payload  # noqa: E402

# --------------------------------------------------------------------- 样本

SEARCH_SAMPLE = {
    "result": [
        {
            "type": "video",
            "bvid": "BV1aa",
            "title": "<em class=\"keyword\">周杰伦</em> 晴天 MV",
            "author": "某UP",
            "duration": "4:29",
            "pic": "//i0.hdslb.com/bfs/a.jpg",
            "play": 12345,
            "pubdate": 1700000000,
            "description": "描述 &amp; 实体",
        },
        {
            "type": "video",
            "bvid": "BV1bb",
            "title": "长合集",
            "author": "UP2",
            "duration": "14:05:22",
            "pic": "http://i1.hdslb.com/b.jpg",
            "play": 76883000,
        },
        # 非视频类型应被过滤
        {"type": "bangumi", "bvid": "BV1cc", "title": "番剧"},
        # 缺 bvid 应被跳过
        {"type": "video", "title": "没有bvid"},
        # 超长视频时长可能给 "--:--"
        {"type": "video", "bvid": "BV1dd", "title": "未知时长", "duration": "--:--"},
    ],
    "page": 1,
    "numPages": 34,
    "numResults": 1000,
}

VIEW_MULTIPART = {
    "bvid": "BV1fx411N7bU",
    "aid": 1415480,
    "cid": 2154848,  # 第 1P
    "title": "【经典】周杰伦全MV 【200P】",
    "duration": 50722,  # 所有分P之和 = 14 小时
    "pic": "//i1.hdslb.com/bfs/archive/x.jpg",
    "pubdate": 1500000000,
    "desc": "合集说明",
    "owner": {"name": "Ta酱-Tatsu"},
    "stat": {"view": 76883000},
    "pages": [
        {"page": 1, "cid": 2154848, "part": "可爱女人【1st JAY】", "duration": 238},
        {"page": 2, "cid": 2154060, "part": "完美主义【1st JAY】", "duration": 242},
        {"page": 3, "cid": 2154061, "part": "星晴【1st JAY】", "duration": 256},
    ],
}

PLAYURL_SAMPLE = {
    "dash": {
        "duration": 238,
        "audio": [
            {"id": 30280, "baseUrl": "https://cdn/192.m4s", "bandwidth": 203786, "codecs": "mp4a.40.2"},
            {"id": 30216, "baseUrl": "https://cdn/64.m4s", "bandwidth": 43962, "codecs": "mp4a.40.5"},
            {"id": 30232, "baseUrl": "https://cdn/132.m4s", "bandwidth": 102931, "codecs": "mp4a.40.2"},
        ],
        "flac": {"audio": {"id": 30251, "baseUrl": "https://cdn/flac.m4s", "bandwidth": 1000000, "codecs": "fLaC"}},
        "dolby": {"audio": [{"id": 30250, "baseUrl": "https://cdn/dolby.m4s", "bandwidth": 256000, "codecs": "ec-3"}]},
    }
}


class TestCheckPayload(unittest.TestCase):
    """验证 check_payload 对 B站响应封装的统一校验:只有 code=0 才放行,否则转成 ApiError 或 NetworkError。"""
    def test_ok_returns_dict(self) -> None:
        """code=0 时必须原样返回 data 字段,这是所有 parse_* 拿数据的前提。"""
        data = check_payload(b'{"code":0,"data":{"x":1}}', "http://x")
        self.assertEqual(data["data"], {"x": 1})

    def test_nonzero_code_raises_api_error(self) -> None:
        """接口返回非 0 code 时必须抛 ApiError 并带上原始 code 与 message,否则界面只能报一个含糊的失败。"""
        with self.assertRaises(ApiError) as ctx:
            check_payload(b'{"code":-404,"message":"not found"}', "http://x")
        self.assertEqual(ctx.exception.code, -404)
        self.assertEqual(ctx.exception.message, "not found")

    def test_invalid_json_raises_network_error(self) -> None:
        """响应体不是合法 JSON 时要转成 NetworkError,不能把 JSONDecodeError 泄漏给上层调用方。"""
        with self.assertRaises(NetworkError):
            check_payload(b"\x1f\x8b\x08garbage", "http://x")

    def test_non_object_toplevel_raises(self) -> None:
        """顶层是数组等非对象结构时同样按 NetworkError 处理,避免后续按 dict 取字段时崩溃。"""
        with self.assertRaises(NetworkError):
            check_payload(b'["a","b"]', "http://x")


class TestParseSearchResult(unittest.TestCase):
    """验证搜索响应解析:过滤非视频与缺 bvid 的条目,并清洗标题高亮标签与 HTML 实体。"""
    def test_filters_and_cleans(self) -> None:
        """非视频类型与缺 bvid 的条目必须被过滤,同时保留 total 与 total_pages,否则结果数和翻页都会算错。"""
        result = parse_search_result(SEARCH_SAMPLE)
        # 番剧与缺 bvid 的条目被过滤,剩 3 条
        self.assertEqual(len(result.videos), 3)
        self.assertEqual(result.total, 1000)
        self.assertEqual(result.total_pages, 34)

    def test_strips_highlight_and_entities(self) -> None:
        """标题里的 <em> 高亮标签与 &amp; 实体都要还原成纯文本,否则列表会直接显示 HTML 源码。"""
        video = parse_search_result(SEARCH_SAMPLE).videos[0]
        self.assertEqual(video.title, "周杰伦 晴天 MV")
        self.assertEqual(video.description, "描述 & 实体")

    def test_durations(self) -> None:
        """三种时长写法都要正确换算:MM:SS 与 HH:MM:SS 换算成秒,而 "--:--" 这类未知时长退化为 0。"""
        videos = parse_search_result(SEARCH_SAMPLE).videos
        self.assertEqual(videos[0].duration, 269)  # 4:29
        self.assertEqual(videos[1].duration, 50722)  # 14:05:22
        self.assertEqual(videos[2].duration, 0)  # "--:--" 退化为 0

    def test_cover_scheme_normalised(self) -> None:
        """封面地址要统一补全并升级为 https,否则在安全上下文里会被 Qt 当作混合内容拒绝加载。"""
        videos = parse_search_result(SEARCH_SAMPLE).videos
        self.assertTrue(videos[0].cover_https.startswith("https://"))
        self.assertTrue(videos[1].cover_https.startswith("https://i1.hdslb.com"))

    def test_empty_payload(self) -> None:
        """空 payload 必须得到空列表而不是异常,且 total_pages 至少为 1,保证界面不会出现 0 页。"""
        result = parse_search_result({})
        self.assertEqual(len(result), 0)
        self.assertEqual(result.total_pages, 1)


class TestParseVideo(unittest.TestCase):
    """验证视频详情解析:分P列表的建立、视频级与分P级 duration 的区别,以及字段缺失时的兜底。"""
    def test_multipart_pages(self) -> None:
        """多P合集要按 pages 逐条建出分P并保留各自的 cid、part 与 duration,因为每个分P才是一首歌。"""
        video = parse_video(VIEW_MULTIPART)
        self.assertEqual(video.part_count, 3)
        self.assertTrue(video.is_multipart)
        self.assertEqual(video.page(1).cid, 2154848)
        self.assertEqual(video.page(2).title, "完美主义【1st JAY】")
        self.assertEqual(video.page(3).duration, 256)

    def test_video_level_duration_is_sum_not_single_track(self) -> None:
        """这是最容易写错的地方:video.duration 是所有分P之和。"""
        video = parse_video(VIEW_MULTIPART)
        self.assertEqual(video.duration, 50722)
        self.assertEqual(video.page(1).duration, 238)
        self.assertNotEqual(video.duration, video.page(1).duration)

    def test_stats_and_owner(self) -> None:
        """owner.name、stat.view、aid 与顶层 cid 都要映射到数据模型,界面的作者与播放量直接取这些字段。"""
        video = parse_video(VIEW_MULTIPART)
        self.assertEqual(video.author, "Ta酱-Tatsu")
        self.assertEqual(video.play_count, 76883000)
        self.assertEqual(video.aid, 1415480)
        self.assertEqual(video.cid, 2154848)

    def test_single_part(self) -> None:
        """只有一P时 is_multipart 必须为假,否则界面会显示一个毫无意义的分P选择列表。"""
        payload = {
            "bvid": "BV1x",
            "cid": 137649199,
            "duration": 213,
            "title": "单曲",
            "pages": [{"page": 1, "cid": 137649199, "part": "", "duration": 213}],
        }
        video = parse_video(payload)
        self.assertFalse(video.is_multipart)
        self.assertEqual(video.page(1).duration, 213)

    def test_missing_pages_falls_back_to_toplevel_cid(self) -> None:
        """响应缺 pages 时退回顶层 cid(即第 1P),保证没有分P信息的视频仍能取到音轨。"""
        video = parse_video({"bvid": "BV1y", "cid": 999, "duration": 100})
        self.assertEqual(video.cid, 999)
        self.assertEqual(video.pages, [])
        self.assertFalse(video.is_multipart)

    def test_pages_without_cid_are_skipped(self) -> None:
        """cid 为 0 的分P条目要丢弃,否则会拿无效 cid 去请求 playurl,缓存键也会跟着串。"""
        payload = {
            "bvid": "BV1z",
            "cid": 5,
            "pages": [{"page": 1, "cid": 0}, {"page": 2, "cid": 7, "duration": 10}],
        }
        video = parse_video(payload)
        self.assertEqual(video.part_count, 1)
        self.assertEqual(video.pages[0].cid, 7)

    def test_fallback_bvid_used_when_absent(self) -> None:
        """响应里没有 bvid 时要采用调用方传入的 fallback_bvid,后续请求与缓存键都依赖它。"""
        video = parse_video({"cid": 1}, fallback_bvid="BVfrom_call")
        self.assertEqual(video.bvid, "BVfrom_call")


class TestParseAudioTracks(unittest.TestCase):
    """验证 playurl 音轨解析:排序、音质标签、无损与杜比识别,以及没有可用音源时的报错。"""
    def test_sorted_ascending_by_bandwidth(self) -> None:
        """音轨要按 bandwidth 升序返回,界面音质列表与 pick_best 的挑选都建立在这个顺序上。"""
        tracks = parse_audio_tracks(PLAYURL_SAMPLE, bvid="BV1")
        bandwidths = [t.bandwidth for t in tracks]
        self.assertEqual(bandwidths, sorted(bandwidths))

    def test_includes_flac_and_dolby(self) -> None:
        """dash.flac 与 dash.dolby 里的音轨也要解析出来,否则界面上会缺失无损和杜比这两档音质。"""
        tracks = parse_audio_tracks(PLAYURL_SAMPLE)
        self.assertTrue(any(t.is_lossless for t in tracks))
        self.assertTrue(any(t.is_dolby for t in tracks))

    def test_best_is_lossless(self) -> None:
        """存在无损音轨时 pick_best 必须选中它并标注为 Hi-Res 无损,这是优先级最高的音质档。"""
        tracks = parse_audio_tracks(PLAYURL_SAMPLE)
        best = pick_best(tracks)
        self.assertTrue(best.is_lossless)
        self.assertEqual(best.label, "Hi-Res 无损")

    def test_labels(self) -> None:
        """每个 quality_id 要映射到固定的中文标签(64K / 132K / 192K / 杜比全景声),界面直接展示这些文案。"""
        tracks = {t.quality_id: t for t in parse_audio_tracks(PLAYURL_SAMPLE)}
        self.assertEqual(tracks[30216].label, "64K")
        self.assertEqual(tracks[30232].label, "132K")
        self.assertEqual(tracks[30280].label, "192K")
        self.assertEqual(tracks[30250].label, "杜比全景声")

    def test_backup_url_used_when_base_missing(self) -> None:
        """没有 baseUrl 时要回退到 backupUrl,否则备用 CDN 形同虚设、这条音轨会因缺 URL 被丢掉。"""
        payload = {"dash": {"audio": [{"id": 30280, "backupUrl": ["https://backup/x.m4s"], "bandwidth": 1}]}}
        tracks = parse_audio_tracks(payload)
        self.assertEqual(tracks[0].url, "https://backup/x.m4s")

    def test_bandwidth_fallback_by_quality_id(self) -> None:
        """响应缺 bandwidth 时要按 quality_id 给出兜底带宽,否则排序与展示都会拿到 0。"""
        payload = {"dash": {"audio": [{"id": 30280, "baseUrl": "https://cdn/x"}]}}
        tracks = parse_audio_tracks(payload)
        self.assertEqual(tracks[0].bandwidth, 192_000)

    def test_no_audio_raises(self) -> None:
        """dash.audio 为空或整个 payload 里没有 dash 时必须抛 NoAudioSourceError,让上层能给出明确的无音源提示。"""
        with self.assertRaises(NoAudioSourceError):
            parse_audio_tracks({"dash": {"audio": []}}, bvid="BV1")
        with self.assertRaises(NoAudioSourceError):
            parse_audio_tracks({}, bvid="BV1")

    def test_entries_without_url_are_dropped(self) -> None:
        """既没有 baseUrl 也没有 backupUrl 的条目要丢弃,只留下真正能下载的那一条音轨。"""
        payload = {"dash": {"audio": [{"id": 30280}, {"id": 30232, "baseUrl": "https://cdn/y"}]}}
        tracks = parse_audio_tracks(payload)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].quality_id, 30232)

    def test_pick_best_on_empty_raises(self) -> None:
        """空音轨列表调用 pick_best 必须抛 NoAudioSourceError,而不是返回 None 让调用方踩空。"""
        with self.assertRaises(NoAudioSourceError):
            pick_best([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
