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

需要登录(``SESSDATA``)的接口,本模块已实现:

* ``/x/web-interface/nav`` 登录态与账号概况
* ``/x/v3/fav/folder/created/list-all`` 我创建的收藏夹
* ``/x/v3/fav/resource/list`` 收藏夹内容

**不需要 WBI 签名**:2026-09-14 在真账号上实测,上面三个接口全程不带 ``w_rid``/``wts``
都返回 ``code=0``(所以路线图 M5 里的 ``core/wbi.py`` 没有做)。但注意"不需要签名"不等于
"可以随便请求" —— ``-352`` 的定义是"UA **或** wbi 参数不合法",限速与预热照旧。

仍未实现:

* CC 字幕(匿名时 ``subtitles`` 恒为空数组)
* 收藏夹的**写回**(收藏 / 取消收藏)与私密夹之外的账号操作,见 ``AGENTS.md`` 1.4

解析层的容错口径:**能少不能错**。字段缺失、类型不对、列表里混进非视频条目,
一律跳过或取默认值,绝不让单条脏数据把整页搜索结果带崩;但"整个响应里一条音轨都
没有"属于真实失败,必须抛 :class:`NoAudioSourceError` 让界面给出明确提示。

收藏夹那三条接口另有一条**实测出来的**容错要求:未登录时
``/x/v3/fav/folder/created/list-all`` 返回 ``code=0`` 且 ``data=null``(它不报错,只静默
给空)。所以解析函数必须能吃下 ``data=None``,而"登录态是否有效"**只能**看
``nav`` 的 ``isLogin``。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

from ..core.errors import NoAudioSourceError
from ..core.headers import API_BASE
from ..core.logging_setup import get_logger
from ..core.models import AudioTrack, Page, Video
from ..net.base import FetchHandle, HttpBackend

#: 本模块的日志器。命名空间由 ``core.logging_setup`` 统一决定。
#:
#: 接口层只记 ``DEBUG`` 一件事:**哪个接口、带什么显式入参**。
#: 这是本方案里"看得见搜索词与 bvid"的唯一来源 —— 网络层记的 URL 已经把 query 取值
#: 全抹了(见 :mod:`..core.redact`),而那些取值恰恰是排障时最想看的。
#: 取值在这里是**显式变量**(``keyword`` / ``bvid`` / ``cid``),不必从 URL 里反解。
_LOGGER = get_logger(__name__)

__all__ = [
    "AccountInfo",
    "BilibiliClient",
    "FavFolder",
    "FavItem",
    "FavPage",
    "PRIVATE_FOLDER_ATTR_BIT",
    "QUALITY_FALLBACK_BPS",
    "SearchResult",
    "parse_audio_tracks",
    "parse_fav_folders",
    "parse_fav_page",
    "parse_nav",
    "parse_search_result",
    "parse_video",
    "pick_best",
    "track_from_cache",
    "track_from_quality",
]

#: 收藏夹 ``attr`` 位域里表示"私密"的那一位(取值 ``2``)。
#:
#: 2026-09-14 实测:16 个收藏夹里多数是 ``22``(``0b10110``,含私密位),默认收藏夹是
#: ``0``(公开)。**只钉这一位**,其余位的含义没有实测依据,不要去猜 —— 猜错会让界面
#: 显示的"公开/私密"反过来。
PRIVATE_FOLDER_ATTR_BIT = 2

#: 收藏夹条目里"正常"的 ``attr`` 值。
#:
#: 2026-09-14 实测:``attr=0`` 的条目 ``pagelist`` 与 ``view`` 都成功;``attr=1`` / ``9``
#: 的条目两个接口都失败(``-404`` / ``62002 稿件不可见``)。所以**判断条目失不失效只看
#: 这一个字段就够了**,不必为每条多打一次请求。
FAV_ITEM_OK_ATTR = 0

#: 收藏夹条目类型:视频稿件。界面目前只处理这一类。
FAV_ITEM_TYPE_VIDEO = 2

#: 收藏夹内容一页最多取多少条(接口 ``ps`` 的定义域是 1~20,实测传 20 足额返回)。
MAX_FAV_PAGE_SIZE = 20

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


@dataclass(slots=True)
class AccountInfo:
    """当前登录账号的概况(``nav`` 接口)。

    :attr:`is_login` 是**唯一**可信的登录态判据:收藏夹接口在未登录时也返回 ``code=0``
    (只是 ``data`` 为 ``null``),拿它判断会得到假阳性。
    """

    is_login: bool = False
    mid: int = 0
    uname: str = ""
    vip_type: int = 0


@dataclass(slots=True)
class FavFolder:
    """一个收藏夹的元信息。"""

    media_id: int = 0
    title: str = ""
    media_count: int = 0
    attr: int = 0

    @property
    def is_private(self) -> bool:
        """是否为私密收藏夹。

        私密夹在登录态下**能读到**(2026-09-14 实测),所以界面上要标出来 ——
        用户得知道这一页的内容不该被外人看到。
        """
        return bool(self.attr & PRIVATE_FOLDER_ATTR_BIT)


@dataclass(slots=True)
class FavItem:
    """收藏夹里的一条内容。

    注意**没有 ``cid``**:接口只给分P**数量**(:attr:`page_count`)。真要多P里的某一首,
    必须再调一次 ``view``(:meth:`BilibiliClient.fetch_video`)拿 ``pages`` ——
    这是本项目的领域铁律(播放一律用分P的 ``cid``)。
    """

    bvid: str = ""
    title: str = ""
    author: str = ""
    cover_url: str = ""
    duration: int = 0
    page_count: int = 0
    attr: int = 0
    item_type: int = 0
    fav_time: int = 0

    @property
    def is_dead(self) -> bool:
        """这条收藏是否已失效(稿件被删/下架)。

        失效条目在收藏夹里仍然占位,但``pagelist``/``view`` 都会失败,所以界面必须
        禁止点击 —— 否则用户一点就是一个错误弹窗。
        """
        return self.attr != FAV_ITEM_OK_ATTR

    @property
    def is_playable(self) -> bool:
        """是否可以拿去播放(是视频稿件、有 ``bvid``、且没有失效)。"""
        return self.item_type == FAV_ITEM_TYPE_VIDEO and bool(self.bvid) and not self.is_dead

    @property
    def is_multipart(self) -> bool:
        """是否是分P合集(``page_count > 1``,每个分P是一首歌)。"""
        return self.page_count > 1


@dataclass(slots=True)
class FavPage:
    """收藏夹内容的一页。

    翻页只看 :attr:`has_more` —— 不要用"这一页条数为 0"当结束条件,接口在这一页恰好
    全是失效条目时的行为没有实测依据。
    """

    items: list[FavItem] = field(default_factory=list)
    media_id: int = 0
    media_count: int = 0
    has_more: bool = False

    def __len__(self) -> int:
        """本页返回的条目数量(含失效条目)。"""
        return len(self.items)


# ====================================================================== 纯解析


def _https_url(raw: object) -> str:
    """把封面地址升级成 https(与 :attr:`~bilibili_music.core.models.Video.cover_https` 同一口径)。

    B站的图片地址有 ``//i0.hdslb.com/...`` 与 ``http://i2.hdslb.com/...`` 两种形态,
    Qt 在部分环境下会拒绝加载非 https 的图,所以在**解析阶段**就统一掉,
    免得每个使用方各写一遍。

    Args:
        raw: 接口给的原始地址,可能是空值或非字符串。

    Returns:
        升级后的 https 地址;拿不到有效字符串时是空串。
    """
    url = str(raw or "")
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def parse_nav(payload: dict[str, Any]) -> AccountInfo:
    """解析 ``nav`` 的 ``data`` 部分。

    未登录时接口会返回 ``code=-101``(由上层转成 :class:`ApiError`),所以走到这里通常
    已经是登录态;但仍然显式读 ``isLogin`` —— 它是唯一可信的判据。

    Args:
        payload: 响应体里的 ``data`` 对象;``None`` 或结构不符时按未登录处理。

    Returns:
        账号概况;``isLogin`` 缺失或为假时 ``is_login`` 为 ``False``。
    """
    data = payload if isinstance(payload, dict) else {}
    # 不能写成 ``data.get("vipStatus") or data.get("vipType")``:``vipStatus=0`` 是**有效**
    # 取值(表示大会员已过期),用 or 会被短路成 vipType,把过期账号显示成大会员
    vip_raw = data.get("vipStatus")
    if vip_raw is None:
        vip_raw = data.get("vipType")
    return AccountInfo(
        is_login=bool(data.get("isLogin")),
        mid=int(data.get("mid") or 0),
        uname=str(data.get("uname") or ""),
        vip_type=int(vip_raw or 0),
    )


def parse_fav_folders(payload: dict[str, Any]) -> list[FavFolder]:
    """解析 ``fav/folder/created/list-all`` 的 ``data`` 部分。

    **必须容忍 ``data=None``**:2026-09-14 实测,未登录(或凭据失效)时这个接口返回
    ``code=0`` + ``data=null``,不是错误码。把它当"没有收藏夹"处理,登录态的问题交给
    ``nav`` 去说。

    Args:
        payload: 响应体里的 ``data`` 对象,或 ``None``。

    Returns:
        收藏夹列表;``data`` 为 ``null``、``list`` 缺失或全是脏数据时返回空列表。
    """
    data = payload if isinstance(payload, dict) else {}
    raw_items = data.get("list")
    if not isinstance(raw_items, list):
        return []
    folders: list[FavFolder] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        media_id = int(item.get("id") or 0)
        if not media_id:
            # 没有 media_id 的条目取不到内容,留着只会让界面点出一次必然失败的请求
            continue
        folders.append(
            FavFolder(
                media_id=media_id,
                title=str(item.get("title") or ""),
                media_count=int(item.get("media_count") or 0),
                attr=int(item.get("attr") or 0),
            )
        )
    return folders


def parse_fav_page(payload: dict[str, Any]) -> FavPage:
    """解析 ``fav/resource/list`` 的 ``data`` 部分。

    Args:
        payload: 响应体里的 ``data`` 对象,或 ``None``(未登录时实测就是 ``null``)。

    Returns:
        一页收藏夹内容。``info`` 缺失时 ``media_count`` 退化为 ``0``;非视频条目
        (``type=12`` 音频、``21`` 合集)**保留但标成不可播**(用户得看得见"这里有一条
        不是视频"),只有"视频类型却缺 ``bvid`` 且未失效"才算脏数据被跳过。
    """
    data = payload if isinstance(payload, dict) else {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    raw_items = data.get("medias")
    items: list[FavItem] = []
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            bvid = str(raw.get("bv_id") or raw.get("bvid") or "")
            item_type = int(raw.get("type") or 0)
            attr = int(raw.get("attr") or 0)
            # 只有"视频稿件却没有 bvid、又没标失效"才是脏数据(拿它必然点不动);
            # 非视频条目与失效条目都要保留 —— 界面要如实显示它们并说明为什么不能播
            if item_type == FAV_ITEM_TYPE_VIDEO and not bvid and attr == FAV_ITEM_OK_ATTR:
                continue
            items.append(
                FavItem(
                    bvid=bvid,
                    title=str(raw.get("title") or ""),
                    author=str((raw.get("upper") or {}).get("name") or "")
                    if isinstance(raw.get("upper"), dict)
                    else "",
                    cover_url=_https_url(raw.get("cover")),
                    duration=_parse_duration(raw.get("duration")),
                    page_count=int(raw.get("page") or 0),
                    attr=attr,
                    item_type=item_type,
                    fav_time=int(raw.get("fav_time") or 0),
                )
            )
    return FavPage(
        items=items,
        media_id=int(info.get("id") or 0),
        media_count=int(info.get("media_count") or 0),
        has_more=bool(data.get("has_more")),
    )


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
        _LOGGER.debug("接口 search 关键词=%s 页=%d", keyword, page)
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
            result = parse_search_result(data.get("data") or {}, requested_page=page)
            _LOGGER.debug(
                "接口 search 结果 命中=%d 页=%d/%d 总数=%d",
                len(result.videos),
                result.page,
                result.total_pages,
                result.total,
            )
            on_success(result)

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
                _LOGGER.debug("接口 view bvid=%s 命中缓存=是", bvid)
                on_success(hit)
                return None
        _LOGGER.debug("接口 view bvid=%s 命中缓存=否", bvid)
        url = f"{API_BASE}/x/web-interface/view?bvid={bvid}"

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:解析并写入缓存后再回调。

            先写缓存再回调,是为了让回调里再次调用 :meth:`fetch_video` 时能直接命中,
            否则切换分P会重复打同一个接口。
            """
            video = parse_video(data.get("data") or {}, fallback_bvid=bvid)
            self._video_cache[bvid] = video
            _LOGGER.debug(
                "接口 view 结果 bvid=%s 分P=%d 时长=%ds",
                video.bvid,
                len(video.pages),
                video.duration,
            )
            on_success(video)

        return self.backend.get_json(url, on_success=handle_data, on_error=on_error)

    # ------------------------------------------------------------ 账号与收藏夹

    def fetch_nav(
        self,
        *,
        on_success: Callable[[AccountInfo], None],
        on_error: ErrorCallback,
    ):
        """取登录态与账号概况。

        这是**唯一**可信的登录态判据(见 :func:`parse_nav`)。凭据失效时接口返回
        ``code=-101``,会由后端转成 :class:`ApiError` 交给 ``on_error``。

        Args:
            on_success: 成功回调,接收 :class:`AccountInfo`。
            on_error: 失败回调;凭据失效时收到 ``code=-101`` 的 :class:`ApiError`。

        Returns:
            可取消的请求句柄。
        """
        url = f"{API_BASE}/x/web-interface/nav"
        _LOGGER.debug("接口 nav")

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:解析 ``data`` 里的账号概况。"""
            info = parse_nav(data.get("data") or {})
            # 只记“是否登录”与会员类型,不记 uname / mid —— 凭据与身份纪律见 AGENTS.md 5.10
            _LOGGER.debug("接口 nav 结果 已登录=%s 会员类型=%d", info.is_login, info.vip_type)
            on_success(info)

        return self.backend.get_json(url, on_success=handle_data, on_error=on_error)

    def fetch_fav_folders(
        self,
        mid: int,
        *,
        on_success: Callable[[list[FavFolder]], None],
        on_error: ErrorCallback,
    ):
        """取"我创建的收藏夹"。

        ``web_location`` 是网页端会带的埋点参数,带上它更接近浏览器行为(风控口径,
        见 README「账号链路」)。**未登录时这个接口也返回 ``code=0``**,只是 ``data`` 为
        ``null`` —— 所以"拿不到收藏夹"不等于"登录失效",别在这里判断登录态。

        Args:
            mid: 账号 mid(从 :meth:`fetch_nav` 拿),作为 ``up_mid`` 参数。
            on_success: 成功回调,接收收藏夹列表(可能是空列表)。
            on_error: 失败回调。

        Returns:
            可取消的请求句柄。
        """
        query = urlencode({"up_mid": mid, "web_location": "333.1387"})
        url = f"{API_BASE}/x/v3/fav/folder/created/list-all?{query}"
        # mid 是账号标识,不落盘:排障需要的是“有没有取到夹子”而不是“是谁的”
        _LOGGER.debug("接口 fav_folders")

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:``data`` 可能是 ``null``,由纯解析函数容忍。"""
            folders = parse_fav_folders(data.get("data"))
            _LOGGER.debug("接口 fav_folders 结果 个数=%d", len(folders))
            on_success(folders)

        return self.backend.get_json(url, on_success=handle_data, on_error=on_error)

    def fetch_fav_page(
        self,
        media_id: int,
        *,
        page: int = 1,
        page_size: int = MAX_FAV_PAGE_SIZE,
        on_success: Callable[[FavPage], None],
        on_error: ErrorCallback,
    ):
        """取某个收藏夹内容的一页。

        ``order=mtime`` 是"最近收藏在前";``type=0`` 表示只查当前收藏夹(文档里 ``type=1``
        是"全部收藏夹",语义完全不同,不要混)。

        Args:
            media_id: 收藏夹 id(来自 :meth:`fetch_fav_folders`)。
            page: 页码,从 1 开始。
            page_size: 每页条数;超过 :data:`MAX_FAV_PAGE_SIZE` 会被夹到上限 ——
                接口的 ``ps`` 定义域是 1~20,传大了不报错但行为没有保证。
            on_success: 成功回调,接收一页内容。
            on_error: 失败回调。

        Returns:
            可取消的请求句柄。
        """
        query = urlencode(
            {
                "media_id": media_id,
                "pn": page,
                "ps": max(1, min(page_size, MAX_FAV_PAGE_SIZE)),
                "order": "mtime",
                "type": 0,
                "tid": 0,
                "platform": "web",
            }
        )
        url = f"{API_BASE}/x/v3/fav/resource/list?{query}"
        _LOGGER.debug("接口 fav_page 页=%d 每页=%d", page, max(1, min(page_size, MAX_FAV_PAGE_SIZE)))

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:解析条目(含失效条目的占位)。"""
            page_data = parse_fav_page(data.get("data"))
            _LOGGER.debug(
                "接口 fav_page 结果 条目=%d 还有更多=%s", len(page_data.items), page_data.has_more
            )
            on_success(page_data)

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
        _LOGGER.debug("接口 playurl bvid=%s cid=%d", bvid, cid)

        def handle_data(data: dict[str, Any]) -> None:
            """响应到达:交给纯解析函数,音轨为空时它会抛出异常。"""
            tracks = parse_audio_tracks(data.get("data") or {}, bvid=bvid)
            # 只记档位与编码,不记地址(地址是带签名的直链,见 core/redact)
            _LOGGER.debug(
                "接口 playurl 结果 音轨=%d 档位=%s",
                len(tracks),
                ",".join(str(track.quality_id) for track in tracks),
            )
            on_success(tracks)

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
