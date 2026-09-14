"""一键缓存整个合集:与播放互不干扰的串行下载管线。

这个模块解决的是"把 200P 合集存到本地"这件事。它**不复用播放用的
:class:`~bilibili_music.audio.resolver.AudioResolver`** —— 那是一个单任务状态机,
再调一次 ``resolve()`` 就会把上一个取消掉。拿它跑批量下载的后果是:用户一点播放,
缓存任务就被默默掐死;反过来缓存任务在跑时,播放也会把它顶掉。两者要并存,就必须
各有一套独立的流程。

于是这里有第二条管线:同样的"取音轨 → 选音质 → 流式落盘 → 写缓存索引"四步,
但**只下载、不播放**,且自带调度。两条管线共用同一个 :class:`~bilibili_music.core.cache.AudioCache`
(原子写入,文件层面安全)和同一个限速器(都在网络后端里),所以请求不会因为多了一条
管线而变密。

调度规则
--------

* **任务之间严格串行**:同一时刻只有一个任务在 ``RUNNING``,其余是 ``PENDING``。
  这是用户明确要的"串行缓存,避免风控/限流"。
* **分P之间也是串行**:一个任务内部按分P顺序依次下载,一次只发一个 playurl、
  一次只开一个下载。
* **与播放并行**:播放请求插在下载请求之间过同一条限速器,谁都不会被饿死。
  代价是下载期间歌曲的解析会略慢一点点(等一个限速窗口)。
* **正在播放的那一P跳过**:播放器自己正在把它写进同一个缓存文件,两条管线同时写
  同一个 ``.part`` 会写出坏文件(见 :class:`~bilibili_music.core.cache.DownloadSink`)。
  被跳过的分P排到本轮末尾再试一次;仍然在播就留给用户(任务照常结束,界面会说清
  还有几个没缓存)。
* **失败即挂起整条队列**:失败多半是 412/429 风控。继续把队列里排着的任务一个个
  打出去,只会把惩罚拖长,所以当前任务置为失败、排队中的任务一并转为暂停,由用户
  决定"继续"还是"移除",界面同时弹窗提示。
* **不做字节级续传**:暂停就 ``abort()`` 掉当前 ``.part``,恢复时这一P从头下。
  音频 CDN 是否接受 ``Range`` 尚未实测(路线图 M2.5),在没有结论之前不假装支持。

状态与重启
----------

任务落在 ``library.db``(见 :mod:`bilibili_music.core.download_task`)。重启时**未完成
的任务一律规整成"已暂停"**:开一次应用就自动发几百个请求是不可接受的,要继续得由用户
点一下。恢复时不需要记得"下到哪一P了" —— 已经在本地的分P用
:func:`~bilibili_music.audio.resolver.pick_best_cached` 现查现跳,缓存索引才是
"哪些歌真的在盘上"的唯一事实来源。

这个模块**没有 QThread**、也不阻塞事件循环:全程是 QNAM 的异步回调,
``_pump`` 只是"在当前没有在飞请求时继续推进"的一个循环(见 :meth:`Downloader._pump`)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..api.bilibili import BilibiliClient, pick_best
from ..core.cache import AudioCache, DownloadSink
from ..core.download_task import (
    DownloadTask,
    DownloadTaskStore,
    TaskState,
    task_for_video,
)
from ..core.errors import NoAudioSourceError
from ..core.models import AudioTrack, Page, Video
from .resolver import pick_best_cached

__all__ = ["Downloader"]


@dataclass(slots=True)
class _Run:
    """一个正在跑的任务的**运行期**状态(不落库)。

    库里只记"进度数字",而"接下来该下哪几P、当前是否在飞请求"是纯粹的过程数据:
    落盘除了增加写次数没有任何用处 —— 进程一死,这些都该由缓存索引重新推出来。

    实例本身兼作"当前任务"的身份标识(:meth:`Downloader._alive` 用 ``is`` 比较),
    与 ``resolver._ResolveState`` 是同一套防串台手法(``AGENTS.md`` 第 5 节第 8 条)。
    """

    task: DownloadTask
    #: 详情补全后的视频对象(带完整分P列表);``None`` 表示还在等 ``view`` 接口
    video: Video | None = None
    #: 本轮要下的分P序号(已排除任务开始时就已在本地的那些)
    pages: list[int] = field(default_factory=list)
    #: :attr:`pages` 里下一个要处理的下标
    index: int = 0
    #: 因为"正在播放"而推迟到本轮末尾再试的分P序号
    deferred: list[int] = field(default_factory=list)
    #: 推迟的那一批是否已经重试过一轮(只重试一次,避免和播放互相僵住)
    retried: bool = False
    #: 是否有请求/下载在飞。它决定 :meth:`Downloader._pump` 该推进还是该让出
    in_flight: bool = False
    #: 当前分P的写入目标(取消时要用它清掉 ``.part``)
    sink: DownloadSink | None = None
    #: 当前在飞的请求句柄(``fetch_*`` 的或 ``download`` 的),取消时逐个调用
    handle: object | None = None
    #: 是否已被取消/暂停。取消之后所有回调都必须被丢弃
    cancelled: bool = False
    #: 当前分P已收字节与总长(总长未知时为 0),只用于界面显示
    page_bytes: int = 0
    page_total: int = 0


class Downloader(QObject):
    """"缓存整个合集"的串行下载调度器。

    信号:
        tasks_changed(): 任务集合、状态或"刚有分P落盘"变了(增删、暂停、继续、完成、失败),
            界面重画整个列表,缓存页也趁机把新行读出来
        task_updated(str): 只有进度数字变了(当前分P收到新字节),携带 ``bvid``;
            界面只需要刷新那一行
        task_failed(str, str): 某个任务失败并挂起,携带 ``bvid`` 与失败原因

    Args:
        client: 接口客户端(取详情与音轨)。
        cache: 音频缓存(落盘与写索引)。
        store: 任务持久化;``None`` 时用 ``cache.db`` 上的默认实现。
        now_playing: 取"当前正在播放的 ``(bvid, cid)``"的回调;``None`` 表示不知道
            (测试或没有播放器时用)。用它跳过正在播放的那一P,避免两条管线写同一个文件。
        parent: Qt 父对象。
    """

    tasks_changed = Signal()
    task_updated = Signal(str)
    task_failed = Signal(str, str)

    def __init__(
        self,
        client: BilibiliClient,
        cache: AudioCache,
        *,
        store: DownloadTaskStore | None = None,
        now_playing: Callable[[], tuple[str, int] | None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        """接上客户端与缓存,并把库里已有的任务读进来(未完成的一律置为暂停)。

        Args:
            client: 接口客户端。
            cache: 音频缓存。
            store: 任务持久化;``None`` 表示用 ``cache.db``。
            now_playing: "当前正在播放哪一分P"的查询回调。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.client = client
        self.cache = cache
        self.store = store if store is not None else DownloadTaskStore(cache.db)
        self._now_playing = now_playing
        #: 全部任务,按创建顺序(与库里读出来的顺序一致)。它是**唯一**的任务对象来源:
        #: 界面读它、调度改它,不存在第二份副本。
        self._tasks: dict[str, DownloadTask] = {}
        #: 正在跑的任务的运行期状态;``None`` 表示当前没有任务在跑
        self._run: _Run | None = None
        #: :meth:`_pump` 的重入闸。同步回调(缓存命中)会在同一个调用栈里回来,
        #: 用它把"递归"挡成"外层循环继续跑",否则一个 200P 任务会压出 200 层栈。
        self._pumping = False
        self._load()

    # ------------------------------------------------------------ 对外:查询

    def tasks(self) -> tuple[DownloadTask, ...]:
        """全部任务(创建顺序,与界面上的行序一致)。"""
        return tuple(self._tasks.values())

    def task(self, bvid: str) -> DownloadTask | None:
        """按 ``bvid`` 取任务;没有则 ``None``。"""
        return self._tasks.get(bvid)

    def active_count(self) -> int:
        """还没干完的任务数(暂停与失败的也算,它们还在列表里等用户处理)。"""
        return sum(1 for task in self._tasks.values() if not task.state.is_finished)

    def running(self) -> str | None:
        """当前正在跑的任务的 ``bvid``;没有则 ``None``。"""
        run = self._run
        return None if run is None else run.task.bvid

    def page_progress(self, bvid: str) -> tuple[int, int]:
        """当前分P的下载进度 ``(已收字节, 总字节)``。

        总长未知(CDN 没给 ``Content-Length``)时第二个值是 ``0``;这个任务不在跑、
        或正卡在 playurl 请求上时返回 ``(0, 0)``。

        Args:
            bvid: 视频 BV 号。

        Returns:
            ``(已收字节, 总字节)`` 二元组。
        """
        run = self._run
        if run is None or run.task.bvid != bvid:
            return (0, 0)
        return (run.page_bytes, run.page_total)

    def blocks_new_task(self, bvid: str) -> bool:
        """这个视频是不是已有**未完成**的任务(有的话不该再建一个)。

        已完成的旧任务不拦:合集后来可能又更新了分P,重新右键一次正好能把它补齐。

        Args:
            bvid: 视频 BV 号。

        Returns:
            不能新建返回 ``True``。
        """
        task = self._tasks.get(bvid)
        return task is not None and not task.state.is_finished

    def cached_page_count(self, video: Video) -> int:
        """这个视频已经有多少个分P在本地缓存里。

        界面拿它拼确认框的"共 N 个分P,已缓存 M 个"——这句话属于"这批缓存要做多少事"
        的业务判断,所以放在这一层而不是让界面自己去查缓存索引。

        Args:
            video: 目标视频(需要带完整分P列表,否则先把详情取回来)。

        Returns:
            已在本地(文件存在且非空)的分P数。
        """
        return sum(
            1 for page in video.pages if pick_best_cached(self.cache, video, page) is not None
        )

    # ------------------------------------------------------------ 对外:操作

    def enqueue(self, video: Video) -> bool:
        """新建一条任务并交给队列(有排队中的任务在跑就自动排队)。

        Args:
            video: 目标视频。**必须带完整分P列表** —— 主窗口是先取详情、让用户确认过
                "共 N 个分P"才走到这里的,再在这里补一次请求没有意义。
            video.pages: 分P列表。

        Returns:
            建成功返回 ``True``;``pages`` 为空、已有未完成任务或 ``bvid`` 为空时
            返回 ``False``(调用方负责提示用户)。
        """
        if not video.bvid or not video.pages:
            return False
        if self.blocks_new_task(video.bvid):
            return False
        task = task_for_video(video)
        self._tasks[task.bvid] = task
        self._persist(task)
        self.tasks_changed.emit()
        self._pump()
        return True

    def pause(self, bvid: str) -> bool:
        """暂停一个任务(排队中或正在跑的都可以)。

        正在跑的那个会被就地取消:在飞的请求取消、``.part`` 清掉,恢复时这一P从头下
        (不做字节级续传,见模块 docstring)。

        **暂停之后队列仍会继续**:若还有别的任务在排队,它们照常开跑 —— "暂停"表达的是
        对这一个任务的意图,不是"停下整条队列"(那是 :meth:`pause_all`)。

        Args:
            bvid: 视频 BV 号。

        Returns:
            确实改成了暂停返回 ``True``;任务不存在或已经结束/已暂停返回 ``False``。
        """
        task = self._tasks.get(bvid)
        if task is None or not task.state.is_active:
            return False
        was_running = self._cancel_running(bvid)
        self._set_state(task, TaskState.PAUSED)
        self.tasks_changed.emit()
        if was_running:
            self._pump()
        return True

    def resume(self, bvid: str) -> bool:
        """让一个暂停/失败的任务重新排队。

        Args:
            bvid: 视频 BV 号。

        Returns:
            确实排上了返回 ``True``;任务不存在、已完成或正在跑时返回 ``False``。
        """
        task = self._tasks.get(bvid)
        if task is None or task.state.is_finished or task.state.is_active:
            return False
        self._set_state(task, TaskState.PENDING)
        self.tasks_changed.emit()
        self._pump()
        return True

    def remove(self, bvid: str) -> bool:
        """移除一条任务(**不动已经下载下来的音频文件**)。

        正在跑的话先取消;之后队列里若还有排队的任务会接着跑。

        Args:
            bvid: 视频 BV 号。

        Returns:
            确实移除了返回 ``True``;没有这条任务返回 ``False``。
        """
        if bvid not in self._tasks:
            return False
        was_running = self._cancel_running(bvid)
        del self._tasks[bvid]
        self.store.remove(bvid)
        self.tasks_changed.emit()
        if was_running:
            self._pump()
        return True

    def pause_all(self) -> int:
        """暂停所有未完成的任务(界面上的"全部暂停")。

        Returns:
            被改成暂停的任务数。
        """
        changed = 0
        for task in list(self._tasks.values()):
            if not task.state.is_active:
                continue
            self._cancel_running(task.bvid)
            self._set_state(task, TaskState.PAUSED)
            changed += 1
        if changed:
            self.tasks_changed.emit()
        return changed

    def resume_all(self) -> int:
        """把暂停与失败的任务全部重新排队(界面上的"全部继续")。

        失败的任务也算在内:用户点"全部继续"就是想再试一次;真要放弃某一条,
        列表里单独"移除"它即可。

        Returns:
            被重新排队的任务数。
        """
        changed = 0
        for task in list(self._tasks.values()):
            if task.state.is_finished or task.state.is_active:
                continue
            self._set_state(task, TaskState.PENDING)
            changed += 1
        if changed:
            self.tasks_changed.emit()
            self._pump()
        return changed

    def clear_finished(self) -> int:
        """清掉已完成的任务记录(**不动音频文件与其它任务**)。

        Returns:
            被清掉的任务数。
        """
        done = [bvid for bvid, task in self._tasks.items() if task.state.is_finished]
        for bvid in done:
            del self._tasks[bvid]
        if done:
            self.store.clear_finished()
            self.tasks_changed.emit()
        return len(done)

    def shutdown(self) -> None:
        """退出前停下在飞下载,并把当前任务规整成"暂停"。

        进程马上要结束了,库里那条记录不该还写着"缓存中":下次启动虽然也会把未完成的
        一律视作暂停,但让库里的状态当场就是对的,用户翻库排查时不会被误导。
        """
        run = self._run
        if run is None:
            return
        self._cancel_run(run)
        self._run = None
        self._set_state(run.task, TaskState.PAUSED)

    # ------------------------------------------------------------ 内部:加载与调度

    def _load(self) -> None:
        """把库里的任务读进来;未完成的一律规整成"暂停"。

        "重启后不自动开下"是产品决策:一个 200P 任务被自动捡起来,意味着每次启动
        应用都可能悄悄发几百个请求。已完成的任务保持原样(它们只是历史记录)。
        """
        for stored in self.store.entries():
            if stored.state is TaskState.PENDING or stored.state is TaskState.RUNNING:
                # 崩溃/强杀时留下的 RUNNING 也在这里被修正,顺带把库里的状态改成暂停
                stored = stored.with_state(TaskState.PAUSED)
                self._persist(stored)
            self._tasks[stored.bvid] = stored

    def _pump(self) -> None:
        """驱动调度:没有任务在跑就挑一个,跑起来的任务一路推进到"需要等回调"为止。

        循环体只有三件事:挑下一个任务、判断要不要让出、执行一步。
        ``in_flight`` 是唯一的"让出"信号 —— 发了请求就等回调,回调回来再进这里。
        :attr:`_pumping` 把同步回调造成的重入挡在门外,由外层循环继续推进
        (见属性说明)。
        """
        if self._pumping:
            return
        self._pumping = True
        try:
            while True:
                run = self._run
                if run is None:
                    if not self._begin_next():
                        return
                    continue
                if run.in_flight or run.cancelled:
                    return
                self._step(run)
        finally:
            self._pumping = False

    def _begin_next(self) -> bool:
        """挑下一个排队中的任务开始跑。

        Returns:
            挑到了返回 ``True``(可能已在等 ``view`` 回调);没有排队任务返回 ``False``。
        """
        task = next(
            (item for item in self._tasks.values() if item.state is TaskState.PENDING),
            None,
        )
        if task is None:
            return False
        task = self._set_state(task, TaskState.RUNNING)
        run = _Run(task=task)
        self._run = run
        # 详情可能已经在 client 的缓存里(用户刚在搜索结果里播过它),那样回调是同步的,
        # 于是 in_flight 会被立刻清掉、这个循环会直接进入下载 —— 不必特判。
        run.in_flight = True
        run.handle = self.client.fetch_video(
            task.bvid,
            on_success=lambda video: self._after_detail(run, video),
            on_error=lambda exc: self._fail(run, exc),
        )
        return True

    def _step(self, run: _Run) -> None:
        """推进"当前任务的下一P"这一步。

        要么做出一个**完全本地**的决定(跳过、推迟、收尾),要么发出一次请求并把
        :attr:`_Run.in_flight` 置为 ``True`` 等回调。前者由 :meth:`_pump` 的循环
        继续调用本方法,后者让循环让出。
        """
        task = run.task
        video = run.video
        if video is None:
            return  # 详情还没回来(正常不会走到:_after_detail 之后才会开始推进)
        if run.index >= len(run.pages):
            if run.deferred and not run.retried:
                # 第一轮里"正在播放"的那些,排到最后再试一次;只重试一轮,
                # 否则用户一直循环播同一P就会让任务永远结束不了
                run.retried = True
                run.pages, run.deferred = run.deferred, []
                run.index = 0
                return
            self._finish(run)
            return

        index = run.pages[run.index]
        run.index += 1
        page = video.page(index)
        if page is None:
            return  # 分P序号在详情里找不到(接口数据怪):跳过,不让整个任务挂掉
        task.page_index = index
        run.page_bytes = run.page_total = 0
        if self._is_playing(task.bvid, page.cid):
            # 播放器正在把这一P写进同一个缓存文件,两条管线同时写一个 .part 会写出坏文件
            run.deferred.append(index)
            return
        hit = pick_best_cached(self.cache, video, page)
        if hit is not None:
            self._mark_page_done(run, index, self._file_size(hit.path))
            return
        run.in_flight = True
        run.handle = self.client.fetch_audio_tracks(
            task.bvid,
            page.cid,
            on_success=lambda tracks: self._after_tracks(run, page, tracks),
            on_error=lambda exc: self._fail(run, exc),
        )

    def _after_detail(self, run: _Run, video: Video) -> None:
        """详情就绪:按缓存索引算出"还差哪几P",然后开始下。"""
        if not self._alive(run):
            return
        run.in_flight = False
        run.video = video
        task = run.task
        # 详情是标题/UP主/分P的权威来源(搜索结果里可能一个都没有)
        task.title = task.title or video.title
        task.author = task.author or video.author
        task.cover_url = task.cover_url or video.cover_https
        if not video.pages:
            self._fail(run, NoAudioSourceError(f"{task.bvid} 没有取到分P列表"))
            return

        task.total_pages = len(video.pages)
        done, size, remaining = self._scan_cached(video)
        run.pages = remaining
        run.index = 0
        # 进度从**已经在盘上的**分P算起:任务可能被暂停过、重启过,或者这些歌本来就是
        # 播放时顺手缓存下来的,从头数会让界面显示"已完成 0 / 共 200"却只下 3 首
        task.done_pages = done
        task.bytes_done = size
        self._persist(task)
        self.task_updated.emit(task.bvid)
        self._pump()

    def _after_tracks(self, run: _Run, page: Page, tracks: list[AudioTrack]) -> None:
        """音轨列表就绪:选最高档,再查一次缓存,然后下载。"""
        if not self._alive(run):
            return
        video = run.video
        assert video is not None
        track = pick_best(tracks)
        # 再查一次:这一次 playurl 请求期间,播放那条管线可能已经把这首歌缓存好了
        cached = self.cache.lookup(video.bvid, page.cid, track.quality_id, track.codec)
        if cached is not None:
            run.in_flight = False
            self._mark_page_done(run, page.index, self._file_size(cached))
            return
        sink = self.cache.sink_for(video.bvid, page.cid, track.quality_id, track.codec)
        run.sink = sink
        run.handle = self.client.backend.download(
            track.url,
            sink=sink,
            on_success=lambda path: self._after_page(run, page, track, path),
            on_error=lambda exc: self._fail(run, exc),
            on_progress=lambda done, total: self._page_progress(run, done, total),
        )

    def _page_progress(self, run: _Run, done: int, total: int) -> None:
        """当前分P的下载进度:记在运行期状态里并通知界面。

        **不落库**:下载中每秒都会来好几个进度回调,写库会把磁盘写花,而且这些数字
        丢了也无所谓(重启后按缓存索引重算,比这更准)。
        """
        if not self._alive(run):
            return
        run.page_bytes = max(0, int(done))
        run.page_total = max(0, int(total))
        self.task_updated.emit(run.task.bvid)

    def _after_page(
        self, run: _Run, page: Page, track: AudioTrack, path: Path
    ) -> None:
        """一个分P下载完成:写索引、记进度,再推进下一P。"""
        if not self._alive(run):
            return
        run.in_flight = False
        run.sink = None
        video = run.video
        assert video is not None
        # 与播放那条管线一样顺手写索引,否则这一首不会出现在"本地缓存"页里
        self.cache.remember(video, page, track, path)
        self._mark_page_done(run, page.index, self._file_size(path))

    def _mark_page_done(self, run: _Run, index: int, size: int) -> None:
        """把一个分P记为已完成(落库 + 通知界面 + 推进)。

        这里发的是 :attr:`tasks_changed` 而不是 :attr:`task_updated`:一个分P落盘会往
        缓存索引里添一条记录,正开着的"本地缓存"页得跟着出新行,那属于结构变化。
        """
        task = run.task
        task.page_index = index
        task.done_pages = max(0, task.done_pages) + 1
        task.bytes_done = max(0, task.bytes_done) + max(0, size)
        self._persist(task)
        self.tasks_changed.emit()
        self.task_updated.emit(task.bvid)
        self._pump()

    def _finish(self, run: _Run) -> None:
        """任务结束:标记完成并接着跑队列里的下一个。"""
        self._run = None
        self._set_state(run.task, TaskState.DONE)
        self.tasks_changed.emit()
        self._pump()

    def _fail(self, run: _Run, exc: Exception) -> None:
        """失败出口:整个任务挂起,并且**把整条队列也停下来**。

        不自动往下跑的取舍见模块 docstring:失败绝大多数是风控,连着把排队的任务一个个
        发出去只会加深惩罚。所以除了当前任务置为失败,排队中的任务也一并转成暂停 ——
        否则界面上会留着一批永远不动的"排队中",用户完全看不懂它们在等什么。
        决定权交回用户:界面会弹窗,并在列表里提供"继续"。
        """
        if not self._alive(run):
            return
        self._cancel_run(run)
        self._run = None
        task = self._set_state(run.task, TaskState.FAILED, error=str(exc))
        for other in list(self._tasks.values()):
            if other.state is TaskState.PENDING:
                self._set_state(other, TaskState.PAUSED)
        self.task_updated.emit(task.bvid)
        self.tasks_changed.emit()
        self.task_failed.emit(task.bvid, str(exc))

    # ------------------------------------------------------------ 内部:工具

    def _alive(self, run: _Run) -> bool:
        """这个运行期状态是否仍是"当前任务"且未被取消。

        与 ``resolver._alive`` 同一条纪律:暂停/移除之后旧请求的回调还会回来,
        少了这道判断就会把数据写进一个已经不属于它的任务里。
        """
        return self._run is run and not run.cancelled

    def _cancel_running(self, bvid: str) -> bool:
        """若这个视频正在跑,取消并清空当前任务槽。

        Returns:
            确实取消了返回 ``True``。
        """
        run = self._run
        if run is None or run.task.bvid != bvid:
            return False
        self._cancel_run(run)
        self._run = None
        return True

    def _cancel_run(self, run: _Run) -> None:
        """取消在飞请求与下载,并清掉半截的临时文件。可安全重复调用。"""
        run.cancelled = True
        run.in_flight = False
        handle = run.handle
        run.handle = None
        if handle is not None and hasattr(handle, "cancel"):
            handle.cancel()
        sink = run.sink
        run.sink = None
        if sink is not None:
            sink.abort()

    def _set_state(
        self, task: DownloadTask, state: TaskState, *, error: str = ""
    ) -> DownloadTask:
        """改状态(并落库),返回**字典里那一份**任务对象。

        返回新对象而不是就地改:状态变化一律走 :meth:`DownloadTask.with_state`
        的统一出口,省得"改内存忘了写库"。调用方要拿返回值来更新自己手里的引用。

        Args:
            task: 目标任务。
            state: 目标状态。
            error: 失败原因;默认空串(非失败状态就该是空的)。

        Returns:
            替换进 :attr:`_tasks` 之后的那一份任务。
        """
        updated = task.with_state(state, error=error)
        self._tasks[updated.bvid] = updated
        self._persist(updated)
        return updated

    def _persist(self, task: DownloadTask) -> None:
        """把任务写回库。

        写失败**不上报**:任务表只是"下次启动还能接着下"的便利,写不进去不该让当前
        这次下载失败(与缓存索引同一条取舍)。真正的失败口径只在 :meth:`_fail` 里。
        """
        self.store.upsert(task)

    def _scan_cached(self, video: Video) -> tuple[int, int, list[int]]:
        """按缓存索引扫一遍分P:已在本地的计数与体积,以及还差哪几P。

        "已缓存"一律问 :func:`~bilibili_music.audio.resolver.pick_best_cached`
        (先索引、后盲查),而不是看库里那条任务记录 —— 缓存索引才是"哪些歌真的在盘上"
        的唯一事实来源:用户可能在应用之外删过文件,这些歌也可能本来就是播放时顺手
        缓存下来的、任务记录里从来没提过它们。

        Args:
            video: 目标视频(带完整分P列表)。

        Returns:
            ``(已完成的分P数, 这些分P占用的字节数, 还需要下载的分P序号)``。
        """
        remaining: list[int] = []
        done = 0
        size = 0
        for page in video.pages:
            hit = pick_best_cached(self.cache, video, page)
            if hit is None:
                remaining.append(page.index)
            else:
                done += 1
                size += self._file_size(hit.path)
        return done, size, remaining

    def _is_playing(self, bvid: str, cid: int) -> bool:
        """播放器当前是不是正在放这一首(用来避开"两条管线写同一个文件")。

        :attr:`_now_playing` 是上层(主窗口)注入的回调。它抛异常时**当作不在播**:
        下载不该因为一个查询回调出错而整个失败。
        """
        if self._now_playing is None:
            return False
        try:
            current = self._now_playing()
        except Exception:  # noqa: BLE001 - 上层回调的异常不该拖垮下载
            return False
        return current is not None and tuple(current) == (bvid, cid)

    @staticmethod
    def _file_size(path: Path) -> int:
        """量一个已经落盘的缓存文件有多大;量不到就当 0。"""
        try:
            return max(0, path.stat().st_size)
        except OSError:
            return 0
