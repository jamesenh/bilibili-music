"""领域数据模型。

这里刻意不引入任何 Qt 或 B站相关的依赖,保证可以脱离界面单独测试。

三个模型构成整条主线的数据骨架:``Video``(一个音乐视频条目)向下包含若干个
``Page``(分P,音乐区里**一个分P就是一首歌**),``AudioTrack`` 则是 playurl 返回的
一条 DASH 音频流。界面、缓存与解析器之间只通过这三个模型传递数据,
不传原始 JSON,这样接口字段一旦变动,只需要改 ``api`` 层的 ``parse_*``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AudioTrack",
    "Page",
    "Playlist",
    "Video",
    "format_count",
    "format_duration",
    "track_subtitle",
    "track_title",
]


def format_duration(seconds: int) -> str:
    """把秒数格式化为 ``mm:ss`` 或 ``h:mm:ss``。

    不足一小时时不显示小时位,避免音乐列表里出现一长串 ``0:04:13``。

    Args:
        seconds: 时长秒数。负数按 ``0`` 处理(接口偶尔会给 -1 表示未知)。

    Returns:
        形如 ``"4:13"`` 或 ``"14:05:22"`` 的字符串。
    """
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_count(count: int) -> str:
    """把播放量之类的数字格式化成 ``1.2万`` / ``3.4亿``。

    与B站网页的显示口径一致:以一万为第一档进位,不足一万时原样显示整数。

    Args:
        count: 待格式化的计数,如播放量。

    Returns:
        中文单位缩写后的字符串,如 ``"7688"`` / ``"1.2万"`` / ``"3.4亿"``。
    """
    if count >= 100_000_000:
        return f"{count / 100_000_000:.1f}亿"
    if count >= 10_000:
        return f"{count / 10_000:.1f}万"
    return str(int(count))


def track_title(video: Video, page: Page | None) -> str:
    """取"这一首要显示的名字"。

    音乐区的多P合集里每个分P是一首独立的歌,所以有分P标题时优先用它;单P视频
    或分P没标题(部分老视频的 ``part`` 是空串)时退回视频标题。

    抽成模块级函数而不是留在某个类里,是因为队列项与解析结果都要显示这个名字,
    两边各写一份迟早会不一致。

    Args:
        video: 目标视频。
        page: 目标分P;详情未补全或序号越界时传 ``None``。

    Returns:
        显示用标题。``None`` 的 ``page`` 只会让它退回视频标题,不会返回空串。
    """
    if video.is_multipart and page is not None and page.title:
        return page.title
    return video.title


def track_subtitle(video: Video, page: Page | None) -> str:
    """取"这一首的副标题":UP主,多P时再附上分P序号。

    单P视频不显示 ``P1``:列表里每行都挂个毫无信息量的 ``P1`` 只是噪声。

    Args:
        video: 目标视频。
        page: 目标分P;传 ``None`` 时不显示序号。

    Returns:
        形如 ``"某UP · P3"``;UP主为空时退化为空串。
    """
    parts = [video.author]
    if video.is_multipart and page is not None:
        parts.append(f"P{page.index}")
    return " · ".join(p for p in parts if p)


@dataclass(slots=True)
class AudioTrack:
    """DASH 中的一条音频流。

    B站的音频质量由 ``id`` 区分,常见取值:

    ====== ==========================
    30216  64K   (codec ``mp4a.40.5``)
    30232  132K  (codec ``mp4a.40.2``)
    30280  192K  (codec ``mp4a.40.2``)
    30250  杜比全景声
    30251  Hi-Res 无损
    ====== ==========================

    杜比与 Hi-Res 不在 ``dash.audio`` 里,而是分别在 ``dash.dolby`` / ``dash.flac``,
    所以 :func:`~bilibili_music.api.bilibili.parse_audio_tracks` 解析时要合并三处。
    """

    quality_id: int
    codec: str
    bandwidth: int
    url: str
    mime_type: str = "audio/mp4"
    is_lossless: bool = False
    is_dolby: bool = False

    @property
    def label(self) -> str:
        """给界面显示用的音质名称。

        优先按杜比 / 无损这两个特殊标记判断,再回落到 ``quality_id`` 对照表;
        遇到未知 ``id`` 时用实际码率拼一个名字,总比显示"未知"有用。
        """
        if self.is_dolby:
            return "杜比全景声"
        if self.is_lossless:
            return "Hi-Res 无损"
        return {
            30216: "64K",
            30232: "132K",
            30280: "192K",
        }.get(self.quality_id, f"{self.bandwidth // 1000}K")

    @property
    def kbps(self) -> int:
        """把接口给的 ``bandwidth``(bit/s)换算成界面惯用的 kbps。

        ``round`` 而不是整除:接口的 bandwidth 常有零头(如 132_005),
        直接取整会显示成 132,先四舍五入更贴近标称档位。
        """
        return round(self.bandwidth / 1000)


@dataclass(slots=True)
class Page:
    """视频的一个分P。

    B站音乐区大量使用多P形式组织内容(``【周杰伦全MV】【200P】`` 这种),
    每个分P其实就是一首歌,所以分P是一等公民而不是附属信息。

    注意 B站的坑:``/x/web-interface/view`` 返回的 ``duration`` 是**所有分P
    时长之和**(``sum(p.duration)``),而 ``cid`` 只对应第 1P。所以展示时长
    必须用分P自己的 ``duration``,不能用视频级的。
    """

    index: int  # 从 1 开始,对应接口里的 page 字段
    cid: int
    title: str = ""
    duration: int = 0

    @property
    def duration_text(self) -> str:
        """分P时长(秒)格式化后的展示文本,如 ``"4:13"``。"""
        return format_duration(self.duration)

    @property
    def label(self) -> str:
        """下拉框里显示的文字。

        分P标题为空时退回 ``P{index}``:部分老视频的 ``part`` 字段是空串,
        没有这个兜底就会出现只有时长的空白条目。
        """
        name = self.title or f"P{self.index}"
        return f"P{self.index} · {name} ({self.duration_text})"


@dataclass(slots=True)
class Video:
    """一个音乐视频条目(可能包含多个分P)。

    由两处接口拼装而成:搜索接口只能给出标题、UP主、封面等表面信息,
    ``aid`` / ``cid`` / ``pages`` 必须等 ``/x/web-interface/view`` 补全后才可用。
    """

    bvid: str
    title: str
    author: str = ""
    duration: int = 0  # 注意:这是所有分P之和
    cover_url: str = ""
    play_count: int = 0
    pubdate: int = 0
    # 以下由 x/web-interface/view 补全
    aid: int = 0
    cid: int = 0  # 仅第 1P,多P场景请用 pages
    description: str = ""
    pages: list[Page] = field(default_factory=list)

    @property
    def duration_text(self) -> str:
        """视频级总时长文本。多P场景下这只是所有分P之和,不能当单曲时长用。"""
        return format_duration(self.duration)

    @property
    def is_multipart(self) -> bool:
        """是否是多P合集(详情未补全时 ``pages`` 为空,会误判为 False)。"""
        return len(self.pages) > 1

    @property
    def part_count(self) -> int:
        """分P数量;详情未补全时为 0。"""
        return len(self.pages)

    def page(self, index: int) -> Page | None:
        """按 1 起始的序号取分P。

        这里按 ``index`` 线性查找而不是用 ``pages[index - 1]``:接口的 ``page``
        字段理论上连续,但遇到过缺号的情况,用下标会在那种数据上错位一首歌。

        Args:
            index: 分P序号,从 1 开始(与B站界面上的 P1 / P2 一致)。

        Returns:
            命中的分P;序号越界时返回 ``None``(调用方需自行决定是报错还是退回第 1P)。
        """
        for item in self.pages:
            if item.index == index:
                return item
        return None

    def first_page(self) -> Page | None:
        """取第 1P,用于"打开视频就放第一首"的默认行为。

        Returns:
            列表中的第一个分P;``pages`` 为空时返回 ``None``。
        """
        return self.pages[0] if self.pages else None

    @property
    def web_url(self) -> str:
        """对应的B站网页地址,便于排查问题时人工打开对照。"""
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def cover_https(self) -> str:
        """封面地址统一升级为 https,否则 Qt 在部分环境会拒绝加载。"""
        url = self.cover_url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http://"):
            return "https://" + url[len("http://"):]
        return url


@dataclass(slots=True)
class Playlist:
    """歌单(后续接收藏夹时复用,先用搜索结果占位)。

    实现 ``__len__`` / ``__iter__`` 是为了让界面能把歌单当普通序列遍历,
    不必关心它内部包着 ``videos`` 列表。
    """

    name: str
    videos: list[Video] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        """歌单里的视频数量。"""
        return len(self.videos)

    def __iter__(self):
        """按顺序遍历歌单里的视频。"""
        return iter(self.videos)
