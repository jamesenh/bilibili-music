"""哔哩哔哩 Web 接口客户端(异步)。

设计:把**解析**与**请求**分开。

* ``parse_*`` 是纯函数,输入接口原始 JSON、输出数据模型,不依赖网络与 Qt,
  可以直接单测(见 ``tests/test_api_parsing.py``)
* :class:`BilibiliClient` 只负责发起请求并把解析结果交给回调

实测(匿名无登录)可用且返回 ``code=0`` 的接口:

============================== ==========================================
``/x/web-interface/view``      视频元信息,拿到 ``aid`` / ``cid`` / 分P
``/x/player/playurl``          DASH 音轨(``fnval=16``)
``/x/web-interface/search/type`` 视频搜索,**无需 WBI 签名**
============================== ==========================================

仍需登录 / WBI 签名的能力(未实现):

* 收藏夹列表与内容(需 ``SESSDATA`` + WBI 签名 ``w_rid``/``wts``)
* CC 字幕(匿名时 ``subtitles`` 恒为空数组)

解析层的容错口径:**能少不能错**。字段缺失、类型不对、列表里混进非视频条目,
一律跳过或取默认值,绝不让单条脏数据把整页搜索结果带崩;但"整个响应里一条音轨都
没有"属于真实失败,必须抛 :class:`NoAudioSourceError` 让界面给出明确提示。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

from ..core.errors import NoAudioSourceError
from ..core.headers import API_BASE
from ..core.models import AudioTrack, Page, Video
from ..net.base import FetchHandle, HttpBackend

__all__ = [
    "BilibiliClient",
    "QUALITY_FALLBACK_BPS",
    "SearchResult",
    "parse_audio_tracks",
    "parse_search_result",
    "parse_video",
    "pick_best",
    "track_from_cache",
    "track_from_quality",
]

#: 搜索结果里的 HTML 标签(主要是关键字高亮用的 ``<em>``)。
#:
#: 注意用正则剥标签而不是引入 HTML 解析器:这里只处理 B站搜索接口那一种固定形态的
#: 高亮标记,引第三方解析库属于为一个字段增加依赖(``AGENTS.md`` 第 3 节)。
_TAG_RE = re.compile(r"<[^>]+>")

#: 已知的音频码率对照(bit/s),用于在接口未给 ``bandwidth`` 时兜底。
#:
#: 数值取各档位的标称值,只用于展示"多少 kbps"与排序,不参与任何鉴权判断。
#: 公开(不带下划线)是因为 :func:`track_from_quality` 也要用它 —— 缓存命中时
#: 手上已经没有接口响应可读,只能按档位取标称码率。
QUALITY_FALLBACK_BPS = {
    30216: 64_000,
    30232: 132_000,
    30280: 192_000,
    30250: 256_000,
    30251: 1_000_000,
}

#: 失败回调签名。与 :data:`bilibili_music.net.base.ErrorCallback` 形状一致,
#: 这里单独定义是为了让 ``api`` 层不必从 ``net`` 再导一次类型别名。
ErrorCallback = Callable[[Exception], None]


def _clean_text(raw: str) -> str:
    """去掉搜索结果的 ``<em>`` 高亮标签并还原 HTML 实体。

    Args:
        raw: 接口返回的原始文本(可能含标签与实体)。

    Returns:
        清洗后的纯文本;``raw`` 为空时返回空串。
    """
    if not raw:
        return ""
    return html.unescape(_TAG_RE.sub("", raw)).strip()


def _parse_duration(raw: object) -> int:
    """把 ``"4:13"`` / ``"1:02:33"`` 或纯秒数解析为秒。

    搜索接口给的是 ``"mm:ss"`` 文本,详情接口给的是整数秒 —— 同一个字段两种形态,
    所以这里统一按"能解析就解析"处理。

    Args:
        raw: 原始取值,可能是 int、float 或 ``"mm:ss"`` 形式的字符串。

    Returns:
        解析出的秒数;无法解析(空串、含非数字段)时返回 ``0``。

    Raises:
        ValueError: ``":"`` 分隔的段只有部分是非数字(如 ``"4:ab"``)时,
            由 :func:`int` 抛出。这类数据 B站实际不会给,不额外兜底。
        OverflowError: 数值超出 ``int`` 范围(实际不会出现,仅为标注完整)。
    """
    if isinstance(raw, (int, float)):
        return int(raw)
    if not isinstance(raw, str) or not raw.strip():
        return 0
    text = raw.strip()
    if ":" not in text:
        return int(text) if text.isdigit() else 0
    total = 0
    for part in text.split(":"):
        if not part.isdigit():
            return 0
        total = total * 60 + int(part)
    return total


@dataclass(slots=True)
class SearchResult:
    """一页搜索结果。

    只承载"这一页返回了什么",不含翻页逻辑 —— 翻页由界面决定要不要再调一次搜索。
    """

    videos: list[Video] = field(default_factory=list)
    page: int = 1
    total_pages: int = 1
    total: int = 0

    def __len__(self) -> int:
        """本页返回的视频数量。"""
        return len(self.videos)


# ====================================================================== 纯解析


def parse_search_result(payload: dict[str, Any], *, requested_page: int = 1) -> SearchResult:
    """解析 ``search/type`` 的 ``data`` 部分。

    Args:
        payload: 响应体里的 ``data`` 对象(不是整个响应)。
        requested_page: 调用方请求的页码,用于接口没回 ``page`` 字段时兜底。

    Returns:
        本页搜索结果。``result`` 为空或全是非视频条目时返回空列表,不抛异常 ——
        "搜不到"是正常结果,不该当错误处理。
    """
    raw_items = payload.get("result") or []
    videos: list[Video] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        # 搜索结果里偶有非视频类型(番剧、课程),用 type 过滤
        if item.get("type") and item["type"] != "video":
            continue
        bvid = item.get("bvid") or ""
        if not bvid:
            continue
        videos.append(
            Video(
                bvid=bvid,
                title=_clean_text(item.get("title", "")),
                author=_clean_text(item.get("author", "")),
                duration=_parse_duration(item.get("duration")),
                cover_url=item.get("pic", "") or "",
                play_count=int(item.get("play") or 0),
                pubdate=int(item.get("pubdate") or 0),
                description=_clean_text(item.get("description", "")),
            )
        )
    return SearchResult(
        videos=videos,
        page=int(payload.get("page") or requested_page),
        total_pages=max(1, int(payload.get("numPages") or 1)),
        total=int(payload.get("numResults") or payload.get("numPages") or 0),
    )


def parse_video(payload: dict[str, Any], *, fallback_bvid: str = "") -> Video:
    """解析 ``view`` 的 ``data`` 部分。

    多P视频务必用返回的 ``pages``:``duration`` 是所有分P之和,
    ``cid`` 只是第 1P,直接拿它播放会永远只播第一首。

    Args:
        payload: 响应体里的 ``data`` 对象。
        fallback_bvid: 接口没回 ``bvid`` 时用它顶上(调用方本来就知道自己请求的是哪个)。

    Returns:
        补全后的 :class:`Video`。``pages`` 里的条目会跳过缺少 ``cid`` 的脏数据。
    """
    raw_pages = payload.get("pages") or []
    pages: list[Page] = []
    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        cid = int(item.get("cid") or 0)
        if not cid:
            continue
        pages.append(
            Page(
                # 接口没给 page 序号时用"当前已解析数量 + 1"顶上,
                # 保证 index 至少是 1 起始且不重复,不会被当成第 0P
                index=int(item.get("page") or len(pages) + 1),
                cid=cid,
                title=(item.get("part") or "").strip(),
                duration=int(item.get("duration") or 0),
            )
        )

    cid = int(payload.get("cid") or 0)
    if not cid and pages:
        # 视频级 cid 缺失时退回第 1P:单P视频没有 pages 之外的可用来源
        cid = pages[0].cid
    return Video(
        bvid=payload.get("bvid") or fallback_bvid,
        title=payload.get("title") or "",
        author=(payload.get("owner") or {}).get("name", "") or "",
        duration=int(payload.get("duration") or 0),
        cover_url=payload.get("pic") or "",
        play_count=int((payload.get("stat") or {}).get("view") or 0),
        pubdate=int(payload.get("pubdate") or 0),
        aid=int(payload.get("aid") or 0),
        cid=cid,
        description=payload.get("desc") or "",
        pages=pages,
    )


def _track_from(
    item: dict[str, Any],
    *,
    lossless: bool = False,
    dolby: bool = False,
) -> AudioTrack | None:
    """从一条 DASH 音频条目构造 :class:`AudioTrack`。

    接口字段有 camelCase(``baseUrl``)与 snake_case(``base_url``)两种写法,
    不同时间点返回的形态不一致,所以两种都要认。

    Args:
        item: ``dash.audio`` / ``dash.flac.audio`` / ``dash.dolby.audio`` 里的一项。
        lossless: 是否来自 ``dash.flac``(标记为 Hi-Res 无损)。
        dolby: 是否来自 ``dash.dolby``(标记为杜比全景声)。

    Returns:
        构造好的音轨;地址字段全为空(既没有 ``baseUrl`` 也没有备用地址)时返回
        ``None``,由调用方跳过这一条而不是让整次解析失败。

    Raises:
        ValueError: ``id`` / ``bandwidth`` 是非数字字符串时由 :func:`int` 抛出。
    """
    url = item.get("baseUrl") or item.get("base_url") or ""
    if not url:
        # 主地址缺失时退回第一个备用地址,CDN 偶尔只给 backupUrl
        backups = item.get("backupUrl") or item.get("backup_url") or []
        url = backups[0] if backups else ""
    if not url:
        return None
    quality_id = int(item.get("id") or 0)
    return AudioTrack(
        quality_id=quality_id,
        codec=item.get("codecs") or "",
        bandwidth=int(item.get("bandwidth") or QUALITY_FALLBACK_BPS.get(quality_id, 0)),
        url=url,
        mime_type=item.get("mimeType") or "audio/mp4",
        is_lossless=lossless,
        is_dolby=dolby,
    )


def parse_audio_tracks(payload: dict[str, Any], *, bvid: str = "") -> list[AudioTrack]:
    """解析 ``playurl`` 的 ``data`` 部分,返回按码率升序的音轨。

    兼容三种位置:``dash.audio``(常规)、``dash.flac``(Hi-Res)、``dash.dolby``。

    Args:
        payload: 响应体里的 ``data`` 对象。
        bvid: 视频 BV 号,仅用于拼错误消息。

    Returns:
        按 ``bandwidth`` 升序排列的音轨列表,便于 :func:`pick_best` 与"降档重试"直接复用。

    Raises:
        NoAudioSourceError: 三种位置都没有可用音轨(需要大会员、已下架或地区限制)。
    """
    dash = payload.get("dash") or {}
    tracks: list[AudioTrack] = []

    for item in dash.get("audio") or []:
        if isinstance(item, dict):
            track = _track_from(item)
            if track:
                tracks.append(track)

    # flac 是单个对象(不是列表),与 audio / dolby 的结构不同,要单独取
    flac = (dash.get("flac") or {}).get("audio")
    if isinstance(flac, dict):
        track = _track_from(flac, lossless=True)
        if track:
            tracks.append(track)

    for item in (dash.get("dolby") or {}).get("audio") or []:
        if isinstance(item, dict):
            track = _track_from(item, dolby=True)
            if track:
                tracks.append(track)

    if not tracks:
        raise NoAudioSourceError(
            f"{bvid or '该视频'} 没有可用音轨(可能需要大会员、已下架或存在地区限制)"
        )
    tracks.sort(key=lambda t: t.bandwidth)
    return tracks


def pick_best(tracks: list[AudioTrack]) -> AudioTrack:
    """默认选码率最高的一条;界面后续可让用户手动切换。

    Args:
        tracks: :func:`parse_audio_tracks` 返回的音轨列表。

    Returns:
        码率最高的一条音轨。

    Raises:
        NoAudioSourceError: 列表为空。
    """
    if not tracks:
        raise NoAudioSourceError("音轨列表为空")
    return max(tracks, key=lambda t: t.bandwidth)


def track_from_cache(
    quality_id: int, codec: str = "", bandwidth: int = 0
) -> AudioTrack:
    """按缓存里记下来的档位、codec 与码率重建一条 ``AudioTrack``。

    用于**缓存命中**的场景:音频文件已经在本地,不需要再请求 playurl,于是也就拿不到
    接口的 ``baseUrl``。``bandwidth`` 优先用缓存索引里存下的真实值(它是上次解析时接口
    给的),拿不到(索引缺失、老记录、盲查命中)才退回该档位的标称值。

    Args:
        quality_id: 音质档位 id,如 ``30280``。
        codec: 编码串;由缓存键或索引里存下来的那一份原样传入。
        bandwidth: 已知的真实码率(bit/s);``<= 0`` 表示不知道,按档位取标称值。

    Returns:
        一条 ``url`` 为空的音轨(它只服务于"展示 + 播本地文件",不参与下载)。
    """
    return AudioTrack(
        quality_id=quality_id,
        codec=codec,
        bandwidth=bandwidth if bandwidth > 0 else int(QUALITY_FALLBACK_BPS.get(quality_id, 0)),
        url="",
    )


def track_from_quality(quality_id: int, codec: str = "") -> AudioTrack:
    """按音质档位直接造一条 ``AudioTrack``(码率取标称值)。

    用于**盲查命中**的场景:那时连索引都没有,只知道档位与 codec。

    Args:
        quality_id: 音质档位 id,如 ``30280``。
        codec: 编码串;由缓存键里存下来的那一份原样传入。

    Returns:
        一条 ``url`` 为空、``bandwidth`` 为**标称值**的音轨。

    Note:
        标称码率可能与文件真实码率有零头差异(界面会显示成 192 kbps 而不是
        191.9 kbps)。真实值在缓存索引里(见 :func:`track_from_cache`)。
        ``url`` 为空是刻意的:这条音轨只服务于"展示 + 播本地文件",不参与下载。
    """
    return track_from_cache(quality_id, codec)


# ====================================================================== 客户端


class BilibiliClient:
    """B站接口的异步封装。

    所有 ``fetch_*`` 方法都是"发起即返回",结果通过回调送回。回调里抛出的异常
    会被后端捕获并转成 ``on_error``,不会炸掉事件循环。

    命中详情缓存时回调是**同步**的(直接在当前调用栈里执行),调用方不要假定
    "回调一定在方法返回之后才触发"。
    """

    def __init__(self, backend: HttpBackend | None = None) -> None:
        """绑定网络后端。

        Args:
            backend: 网络后端;``None`` 时使用默认的 Qt 后端
                (延迟导入,避免在没有 Qt 的环境里 import 本模块就失败)。
        """
        if backend is None:
            # 延迟导入:避免在没有 Qt 的环境里 import 本模块就失败
            from ..net.client import QtNetworkClient

            backend = QtNetworkClient()
        self.backend = backend
        # view 接口结果缓存,避免播放分P时重复请求(也降低风控风险)
        self._video_cache: dict[str, Video] = {}

    # ------------------------------------------------------------ 搜索

    def search_video(
        self,
        keyword: str,
        *,
        page: int = 1,
        on_success: Callable[[SearchResult], None],
        on_error: ErrorCallback,
    ):
        """按关键字搜索视频。返回可取消的句柄。

        关键字为空时**不发请求**,直接同步回调一个空结果 —— 空搜索本来就搜不到东西,
        发出去只会白白消耗一次风控额度。

        Args:
            keyword: 搜索关键字,首尾空白会被去掉。
            page: 页码,从 1 开始。
            on_success: 成功回调,接收 :class:`SearchResult`。
            on_error: 失败回调。

        Returns:
            可取消的请求句柄;关键字为空时返回 ``None``(没有请求需要取消)。
        """
        keyword = keyword.strip()
        if not keyword:
            on_success(SearchResult())
            return None
        query = urlencode(
            {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "page_size": 30,
            }
        )
        url = f"{API_BASE}/x/web-interface/search/type?{query}"

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:只取 ``data`` 部分交给纯解析函数。"""
            on_success(parse_search_result(data.get("data") or {}, requested_page=page))

        return self.backend.get_json(url, on_success=handle_data, on_error=on_error)

    # ------------------------------------------------------------ 详情

    def cached_video(self, bvid: str) -> Video | None:
        """取已缓存的详情(没有则 ``None``)。

        用于界面预判:命中时可以直接用里面的分P列表,不必等 :meth:`fetch_video` 回调。
        """
        return self._video_cache.get(bvid)

    def fetch_video(
        self,
        bvid: str,
        *,
        on_success: Callable[[Video], None],
        on_error: ErrorCallback,
        use_cache: bool = True,
    ):
        """补全 ``aid`` / ``cid`` / 分P列表 / 封面。结果会缓存。

        Args:
            bvid: 视频 BV 号。
            on_success: 成功回调,接收 :class:`Video`。
            on_error: 失败回调。
            use_cache: 是否允许命中缓存。缓存命中时回调是同步的。

        Returns:
            可取消的请求句柄;命中缓存时返回 ``None``。
        """
        if use_cache:
            hit = self._video_cache.get(bvid)
            if hit is not None:
                on_success(hit)
                return None
        url = f"{API_BASE}/x/web-interface/view?bvid={bvid}"

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:解析并写入缓存后再回调。

            先写缓存再回调,是为了让回调里再次调用 :meth:`fetch_video` 时能直接命中,
            否则切换分P会重复打同一个接口。
            """
            video = parse_video(data.get("data") or {}, fallback_bvid=bvid)
            self._video_cache[bvid] = video
            on_success(video)

        return self.backend.get_json(url, on_success=handle_data, on_error=on_error)

    # ------------------------------------------------------------ 音源

    def fetch_audio_tracks(
        self,
        bvid: str,
        cid: int,
        *,
        on_success: Callable[[list[AudioTrack]], None],
        on_error: ErrorCallback,
    ):
        """获取 DASH 音频流,按码率从低到高。

        ``fnval=16`` 表示请求 DASH;不带 ``fnval`` 时返回的是 ``durl``
        音视频合并流,那样只能拿到带画面的整段视频,不适合做音乐播放。

        Args:
            bvid: 视频 BV 号。
            cid: **分P**的 cid(用视频级 cid 只会拿到第 1P 的音轨)。
            on_success: 成功回调,接收升序排列的音轨列表。
            on_error: 失败回调;无可用音轨时收到 :class:`NoAudioSourceError`。

        Returns:
            可取消的请求句柄。
        """
        query = urlencode({"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1, "try_look": 1})
        url = f"{API_BASE}/x/player/playurl?{query}"

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:交给纯解析函数,音轨为空时它会抛出异常。"""
            on_success(parse_audio_tracks(data.get("data") or {}, bvid=bvid))

        return self.backend.get_json(url, on_success=handle_data, on_error=on_error)

    # ------------------------------------------------------------ 封面

    def fetch_cover(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_error: ErrorCallback,
    ) -> FetchHandle | None:
        """取封面图原始字节。

        走 :meth:`HttpBackend.get_bytes` 而不是 ``get_json``:封面是二进制图片,
        没有业务 ``code`` 可校验。

        请求头**用后端默认的 API 头即可** —— 实测封面 CDN(``i0.hdslb.com`` /
        ``i1.hdslb.com``)并不介意 ``Origin``:带与不带都返回 ``200`` 且字节数完全相同。
        这点与音频 CDN **相反**(那边必须摘掉 ``Origin``,见
        :func:`~bilibili_music.core.headers.media_headers`),所以不要照搬那条规则。

        Args:
            url: 封面地址;调用方应传 ``Video.cover_https``(已升级成 https)。
            on_success: 成功回调,参数是图片原始字节。
            on_error: 失败回调;这条路径**不重试**(拿不到封面就显示占位图)。

        Returns:
            可取消的请求句柄;后端未返回句柄时为 ``None``。
        """
        return self.backend.get_bytes(url, on_success=on_success, on_error=on_error)

    # ------------------------------------------------------------ 收尾

    def close(self) -> None:
        """关闭后端并清空详情缓存。

        清缓存是必要的:后端关掉后这些 ``Video`` 对象再也发不出请求,
        留着只会让下一次使用同一个 client 时"命中缓存却什么都播不了"。
        """
        self.backend.close()
        self._video_cache.clear()
