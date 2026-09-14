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
    parse_fav_folders,
    parse_fav_page,
    parse_nav,
    parse_search_result,
    parse_video,
    pick_best,
    track_from_quality,
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


class TestTrackFromQuality(unittest.TestCase):
    """缓存命中时按档位重建音轨(拿不到接口响应,只能用标称码率)。"""

    def test_uses_nominal_bandwidth_of_the_quality(self) -> None:
        """标称码率要能对上档位,否则界面会显示 0 kbps。"""
        track = track_from_quality(30280, "mp4a.40.2")
        self.assertEqual(track.quality_id, 30280)
        self.assertEqual(track.codec, "mp4a.40.2")
        self.assertEqual(track.bandwidth, 192_000)
        self.assertEqual(track.kbps, 192)
        self.assertEqual(track.label, "192K")

    def test_url_is_empty_because_nothing_is_downloaded(self) -> None:
        """这条音轨只服务于播本地文件,不能带一个会被误下载的 URL。"""
        self.assertEqual(track_from_quality(30216, "mp4a.40.2").url, "")

    def test_unknown_quality_still_returns_a_track(self) -> None:
        """未知档位也要返回可用对象:缓存键里存什么档位不由我们决定。"""
        track = track_from_quality(99999, "mp4a.40.2")
        self.assertEqual(track.quality_id, 99999)
        self.assertEqual(track.bandwidth, 0)
        self.assertEqual(track.label, "0K")


class TestParseNav(unittest.TestCase):
    """``nav`` 解析:登录态是唯一可信判据。"""

    def test_logged_in_account(self) -> None:
        """登录态、昵称、mid 与大会员状态都要取到。"""
        info = parse_nav(
            {"isLogin": True, "mid": 42, "uname": "甲", "vipStatus": 1, "vipType": 2}
        )
        self.assertTrue(info.is_login)
        self.assertEqual((info.mid, info.uname, info.vip_type), (42, "甲", 1))

    def test_not_logged_in(self) -> None:
        """``isLogin`` 为假时一律按未登录处理。"""
        self.assertFalse(parse_nav({"isLogin": False}).is_login)

    def test_tolerates_missing_and_wrong_shapes(self) -> None:
        """``data`` 缺失或字段类型不对时退回默认值,不能抛。"""
        for payload in ({}, None, {"mid": "42", "uname": None}):  # type: ignore[arg-type]
            with self.subTest(payload=payload):
                info = parse_nav(payload)  # type: ignore[arg-type]
                self.assertFalse(info.is_login)
                self.assertEqual(info.uname, "")

    def test_expired_vip_reads_as_zero(self) -> None:
        """过期的大会员 ``vipType`` 仍是 2,但 ``vipStatus`` 是 0 —— 取后者才不会骗人。"""
        self.assertEqual(parse_nav({"isLogin": True, "vipType": 2, "vipStatus": 0}).vip_type, 0)


class TestParseFavFolders(unittest.TestCase):
    """收藏夹列表解析。"""

    SAMPLE = {
        "count": 2,
        "list": [
            {"id": 169038169, "title": "默认收藏夹", "media_count": 128, "attr": 0},
            {"id": 3756843069, "title": "mac教程", "media_count": 1, "attr": 22},
        ],
    }

    def test_reads_id_title_count_and_attr(self) -> None:
        """基本字段与 ``attr`` 都要取到。"""
        folders = parse_fav_folders(self.SAMPLE)
        self.assertEqual([f.media_id for f in folders], [169038169, 3756843069])
        self.assertEqual(folders[0].title, "默认收藏夹")
        self.assertEqual(folders[0].media_count, 128)

    def test_private_bit_is_recognised(self) -> None:
        """``attr`` 的私密位(2)要能认出来:实测多数收藏夹是 22,默认夹是 0。"""
        folders = parse_fav_folders(self.SAMPLE)
        self.assertFalse(folders[0].is_private)
        self.assertTrue(folders[1].is_private)

    def test_null_data_means_no_folders(self) -> None:
        """**未登录时接口返回 ``code=0`` + ``data=null``** —— 必须当成空列表而不是崩溃。"""
        self.assertEqual(parse_fav_folders(None), [])  # type: ignore[arg-type]
        self.assertEqual(parse_fav_folders({"list": None}), [])

    def test_skips_entries_without_media_id(self) -> None:
        """没有 ``media_id`` 的条目点开必然失败,直接丢掉。"""
        folders = parse_fav_folders({"list": [{"title": "没有 id"}, {"id": 5, "title": "x"}]})
        self.assertEqual([f.media_id for f in folders], [5])

    def test_skips_non_dict_entries(self) -> None:
        """列表里混进非对象时跳过,不能让整页带崩。"""
        self.assertEqual(parse_fav_folders({"list": ["x", None, {"id": 1}]})[0].media_id, 1)


class TestParseFavPage(unittest.TestCase):
    """收藏夹内容解析(含失效条目与多P语义)。"""

    SAMPLE = {
        "info": {"id": 169038169, "media_count": 128},
        "has_more": True,
        "medias": [
            {
                "id": 111,
                "type": 2,
                "bvid": "BV1ok",
                "title": "正常单P",
                "cover": "http://i2.hdslb.com/a.jpg",
                "duration": 213,
                "page": 1,
                "attr": 0,
                "fav_time": 1700000000,
                "upper": {"name": "某UP"},
            },
            {
                "id": 222,
                "type": 2,
                "bvid": "BV1multi",
                "title": "100P 合集",
                "cover": "//i0.hdslb.com/b.jpg",
                "duration": 3600,
                "page": 100,
                "attr": 0,
                "upper": {"name": "UP2"},
            },
            {"id": 333, "type": 2, "bvid": "BV1dead", "title": "已失效", "attr": 9, "page": 1},
            {"id": 444, "type": 12, "bvid": "", "title": "音频", "attr": 0, "page": 1},
        ],
    }

    def test_page_metadata(self) -> None:
        """``has_more`` 与 ``media_count`` 是翻页与统计的依据。"""
        page = parse_fav_page(self.SAMPLE)
        self.assertTrue(page.has_more)
        self.assertEqual(page.media_count, 128)
        self.assertEqual(page.media_id, 169038169)
        self.assertEqual(len(page), 4)

    def test_item_fields(self) -> None:
        """条目字段要逐个对得上(``upper.name`` 是作者)。"""
        item = parse_fav_page(self.SAMPLE).items[0]
        self.assertEqual(item.bvid, "BV1ok")
        self.assertEqual(item.title, "正常单P")
        self.assertEqual(item.author, "某UP")
        self.assertEqual(item.duration, 213)
        self.assertEqual(item.fav_time, 1700000000)

    def test_cover_is_upgraded_to_https(self) -> None:
        """``http://`` 与 ``//`` 两种封面形态都要升级成 https,否则 Qt 可能拒绝加载。"""
        items = parse_fav_page(self.SAMPLE).items
        self.assertEqual(items[0].cover_url, "https://i2.hdslb.com/a.jpg")
        self.assertEqual(items[1].cover_url, "https://i0.hdslb.com/b.jpg")

    def test_dead_items_are_kept_but_not_playable(self) -> None:
        """失效条目要**留着**(用户得看得见),但一律标成不可播。"""
        items = parse_fav_page(self.SAMPLE).items
        dead = items[2]
        self.assertTrue(dead.is_dead)
        self.assertFalse(dead.is_playable)
        self.assertTrue(items[0].is_playable)

    def test_multipart_flag(self) -> None:
        """``page > 1`` 即多P合集(每个分P是一首歌)。"""
        items = parse_fav_page(self.SAMPLE).items
        self.assertFalse(items[0].is_multipart)
        self.assertTrue(items[1].is_multipart)
        self.assertEqual(items[1].page_count, 100)

    def test_non_video_items_are_not_playable(self) -> None:
        """``type=12``(音频)等非视频条目解析出来但不可播 —— 界面要标注原因。"""
        items = parse_fav_page(self.SAMPLE).items
        self.assertFalse(items[3].is_playable)
        self.assertFalse(items[3].is_dead)

    def test_null_data_means_empty_page(self) -> None:
        """未登录时 ``data`` 是 ``null``,要给一个空页而不是抛异常。"""
        page = parse_fav_page(None)  # type: ignore[arg-type]
        self.assertEqual(len(page), 0)
        self.assertFalse(page.has_more)
        self.assertEqual(page.media_count, 0)

    def test_skips_video_entry_without_bvid(self) -> None:
        """能播的类型却缺 ``bvid`` 属于脏数据,跳过;失效条目的空 bvid 则保留。"""
        page = parse_fav_page({"medias": [{"type": 2, "attr": 0}, {"type": 2, "attr": 9}]})
        self.assertEqual(len(page), 1)
        self.assertTrue(page.items[0].is_dead)

    def test_skips_non_dict_entries(self) -> None:
        """列表里混进非对象时跳过。"""
        self.assertEqual(len(parse_fav_page({"medias": ["x", None, {"bvid": "BV1", "type": 2}]})), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
