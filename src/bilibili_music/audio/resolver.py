"""把 ``Video`` 的某个分P解析成本地可播放的音频文件(异步)。

流程:补全详情(拿分P与 ``cid``) -> 请求 playurl 拿音轨 -> 选音质 -> 下载到缓存。
命中缓存时立即回调,不发任何请求。

**这个模块里没有 QThread**。改造成 QNetworkAccessManager 之后,整个下载过程
就是"发请求 -> 收进度信号 -> 收完成信号",本来就不需要工作线程。之前用
``QThread`` 纯粹是为了绕开 ``urllib`` 的同步阻塞。

为什么不让 ``QMediaPlayer`` 直接播远程 URL:Qt 的媒体请求不经过我们的网络层,
无法附加防盗链必需的 ``Referer``,实测会被 CDN 拒绝。落盘再播还顺带拿到
离线缓存能力。

异步状态机的安全底线:所有阶段回调**先做** :meth:`AudioResolver._alive` 判断。
用户快速切换分P时,旧请求的回调会在新任务开始之后才回来,少了这道判断就会把
上一个视频的数据写进当前界面。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..api.bilibili import BilibiliClient, pick_best
from ..core.cache import AudioCache, DownloadSink
from ..core.errors import NoAudioSourceError
from ..core.models import AudioTrack, Page, Video

#: 常见音轨的 (quality_id, codec) 组合,用于在不发请求的情况下盲查缓存。
#:
#: 顺序即优先级:**从高到低** —— 命中高码率就按高码率播,避免同一首歌既存了
#: 192K 又存了 64K 时随机命中低音质。这里只列常规三档,杜比/Hi-Res 需要大会员,
#: 匿名场景下基本不会落盘,列进来只会白白多三轮 ``stat`` 调用。
KNOWN_QUALITIES: tuple[tuple[int, str], ...] = (
    (30280, "mp4a.40.2"),
    (30232, "mp4a.40.2"),
    (30216, "mp4a.40.2"),
)


@dataclass(slots=True)
class ResolvedAudio:
    """一次解析的完整结果。

    四个字段刚好覆盖"播什么、放哪个文件、界面显示什么"这三件事,
    界面层不需要再去 client 或 cache 里回查任何东西。
    """

    video: Video
    page: Page
    track: AudioTrack
    path: Path

    @property
    def display_title(self) -> str:
        """界面上显示的名字:多P时优先用分P标题。"""
        if self.video.is_multipart and self.page.title:
            return self.page.title
        return self.video.title

    @property
    def subtitle(self) -> str:
        """副标题:UP主 + 分P序号(单P时不显示序号)。"""
        parts = [self.video.author]
        if self.video.is_multipart:
            parts.append(f"P{self.page.index}")
        return " · ".join(p for p in parts if p)


def pick_best_cached(cache: AudioCache, video: Video, page: Page) -> Path | None:
    """盲查缓存:不知道实际音质时,按常见档位从高到低试。

    用于界面预判(命中时可以直接播放,不用先发 playurl 请求)。

    Args:
        cache: 音频缓存实例。
        video: 目标视频,提供 ``bvid``。
        page: 目标分P,提供 ``cid``。

    Returns:
        命中的缓存文件路径;全部档位都未命中时返回 ``None``。
    """
    for quality_id, codec in KNOWN_QUALITIES:
        hit = cache.lookup(video.bvid, page.cid, quality_id, codec)
        if hit is not None:
            return hit
    return None


@dataclass(slots=True)
class _ResolveState:
    """一次解析的中间状态。

    除了任务本身的入参,还带着"运行期填充"的四个字段:它们是各阶段之间的传递物,
    放在同一个对象里是为了让回调闭包只需要捕获 ``state`` 一个变量。

    实例本身还兼作"当前任务"的身份标识 —— :meth:`AudioResolver._alive` 用 ``is``
    比较它和 :attr:`AudioResolver._state`,从而判断一次回调是否已经过期。
    """

    video: Video
    page_index: int
    quality_id: int | None
    on_success: Callable[[ResolvedAudio], None]
    on_error: Callable[[Exception], None]
    on_progress: Callable[[int, int], None] | None
    # 运行期填充
    page: Page | None = None
    track: AudioTrack | None = None
    sink: DownloadSink | None = None
    handle: object | None = None
    cancelled: bool = False


class AudioResolver:
    """把视频分P解析成本地音频文件的异步流程控制器。

    一次只处理一个任务:再次调用 :meth:`resolve` 会自动取消上一个未完成的解析。
    典型用法::

        resolver = AudioResolver(client, cache)
        resolver.resolve(video, page_index=1, on_success=play_it, on_error=show_error)

    Args:
        client: 接口客户端。
        cache: 音频缓存。
    """

    def __init__(self, client: BilibiliClient, cache: AudioCache) -> None:
        """绑定客户端与缓存;初始没有在飞任务。

        Args:
            client: 接口客户端,解析过程会用它补详情、取音轨。
            cache: 音频缓存,用于命中判断与流式落盘。
        """
        self.client = client
        self.cache = cache
        self._state: _ResolveState | None = None

    # ------------------------------------------------------------ 对外接口

    @property
    def busy(self) -> bool:
        """是否有正在进行的解析(已取消的任务不算)。"""
        return self._state is not None and not self._state.cancelled

    def cancel(self) -> None:
        """取消当前解析。已下载的临时文件会被清理。

        没有在飞任务时是空操作,所以可以无脑在切换内容前调用。
        """
        state = self._state
        if state is None:
            return
        state.cancelled = True
        if state.handle is not None and hasattr(state.handle, "cancel"):
            state.handle.cancel()
        if state.sink is not None:
            state.sink.abort()
        self._state = None

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
        """开始解析。缓存命中时同步回调(不经过事件循环)。

        Args:
            video: 目标视频。``pages`` 为空会自动先补详情。
            page_index: 目标分P序号,从 1 开始。
            quality_id: 指定音质档位;``None`` 表示自动选最高码率
                (指定的档位不存在时也会退回自动选择,而不是直接失败)。
            on_success: 成功回调,接收 :class:`ResolvedAudio`。
            on_error: 失败回调。
            on_progress: 可选下载进度回调 ``(已收字节数, 总字节数)``;
                总长未知时第二个参数为 0。缓存命中时不会触发。

        Raises:
            NoAudioSourceError: 直接由 :meth:`_after_detail` 同步抛出 ——
                视频连 ``cid`` 都没有时属于调用方给了无效数据,不做异步兜底。
        """
        self.cancel()
        state = _ResolveState(
            video=video,
            page_index=page_index,
            quality_id=quality_id,
            on_success=on_success,
            on_error=on_error,
            on_progress=on_progress,
        )
        self._state = state

        if not video.pages:
            state.handle = self.client.fetch_video(
                video.bvid,
                on_success=lambda detailed: self._after_detail(state, detailed),
                on_error=lambda exc: self._fail(state, exc),
            )
        else:
            self._after_detail(state, video)

    # ------------------------------------------------------------ 各阶段

    def _alive(self, state: _ResolveState) -> bool:
        """该状态是否仍是"当前任务"且未被取消。

        用 ``is`` 比较身份而不是比内容:重新 ``resolve`` 会造一个新对象,
        旧回调手里的 ``state`` 自然就不等于 ``self._state`` 了 —— 这道判断是
        防止旧请求污染新状态的唯一屏障(``AGENTS.md`` 第 5 节第 8 条)。
        """
        return self._state is state and not state.cancelled

    def _fail(self, state: _ResolveState, exc: Exception) -> None:
        """统一失败出口:先让出"当前任务"身份,再回调。"""
        if not self._alive(state):
            return
        self._state = None
        state.on_error(exc)

    def _after_detail(self, state: _ResolveState, video: Video) -> None:
        """分P信息就绪。

        把详情里带回的分P列表写回 ``state.video``,这样界面后续切换分P时
        可以直接用同一份数据,不必再打一次 ``view`` 接口。
        """
        if not self._alive(state):
            return
        # 详情里带了分P列表,写回对象,后续切换分P就不用再请求
        if video.pages and not state.video.pages:
            state.video.pages = video.pages
            state.video.cid = video.cid or state.video.cid
            state.video.aid = video.aid or state.video.aid
            if video.cover_url:
                state.video.cover_url = video.cover_url

        page = state.video.page(state.page_index)
        if page is None:
            if not state.video.cid:
                self._fail(state, NoAudioSourceError(f"{state.video.bvid} 缺少可用 cid"))
                return
            # 容错:分P列表缺失或序号越界,退回视频级 cid
            # (视频级 cid 就是第 1P,总比直接报错好;时长用视频级的值仅为展示)
            page = Page(
                index=state.page_index,
                cid=state.video.cid,
                title="",
                duration=state.video.duration,
            )
        state.page = page

        state.handle = self.client.fetch_audio_tracks(
            state.video.bvid,
            page.cid,
            on_success=lambda tracks: self._after_tracks(state, tracks),
            on_error=lambda exc: self._fail(state, exc),
        )

    def _after_tracks(self, state: _ResolveState, tracks: list[AudioTrack]) -> None:
        """音轨列表就绪:选音质,查缓存,必要时下载。

        先查缓存再下载,是让"重复播放"退化成零网络请求的关键路径。
        """
        if not self._alive(state):
            return
        track = None
        if state.quality_id is not None:
            track = next((t for t in tracks if t.quality_id == state.quality_id), None)
        if track is None:
            # 指定档位不存在(同一视频不同时刻给的档位会变)时退回最高码率,
            # 不因为"用户指定的 192K 这次没有"就让整次播放失败
            track = pick_best(tracks)
        state.track = track

        assert state.page is not None
        cached = self.cache.lookup(
            state.video.bvid, state.page.cid, track.quality_id, track.codec
        )
        if cached is not None:
            self._done(state, cached)
            return

        sink = self.cache.sink_for(
            state.video.bvid, state.page.cid, track.quality_id, track.codec
        )
        state.sink = sink
        state.handle = self.client.backend.download(
            track.url,
            sink=sink,
            on_success=lambda path: self._done(state, path),
            on_error=lambda exc: self._fail(state, exc),
            on_progress=(lambda done, total: self._progress(state, done, total))
            if state.on_progress
            else None,
        )

    def _progress(self, state: _ResolveState, done: int, total: int) -> None:
        """转发下载进度,已切走的任务直接丢弃(否则进度条会闪回上一个视频的值)。"""
        if self._alive(state) and state.on_progress:
            state.on_progress(done, total)

    def _done(self, state: _ResolveState, path: Path) -> None:
        """成功出口:组装 :class:`ResolvedAudio` 并回调。"""
        if not self._alive(state):
            return
        self._state = None
        assert state.page is not None and state.track is not None
        state.on_success(
            ResolvedAudio(
                video=state.video,
                page=state.page,
                track=state.track,
                path=path,
            )
        )


__all__ = [
    "AudioResolver",
    "ResolvedAudio",
    "KNOWN_QUALITIES",
    "pick_best_cached",
]
