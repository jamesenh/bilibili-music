"""磁盘缓存。

缓存的是**音频文件本身**,而不是 CDN 直链。原因:playurl 返回的直链带签名,
有有效期(通常几小时),过期后必须重新解析;把文件落盘才能离线复用,
也绕开了 Qt 播放远程 URL 时的 Referer 头问题。

写入采用"先写 ``.part`` 再原子替换":下载中断或进程被杀时,不会留下一个
半截文件被后续误判为有效缓存。``os.replace`` 在同一文件系统内是原子的,
所以"缓存文件存在"就等价于"这个文件是完整的",不需要额外的元数据校验。

**除了文件,还维护一份索引**(:class:`~bilibili_music.core.cache_index.CacheIndex`,
与本模块同目录的 ``index.json``):文件名是摘要,只有"知道键再查文件"这一条路,
而"本地缓存"页需要反向枚举出曲名 / UP主 / 体积。索引只服务于展示与快路径,
坏了、丢了都不影响播放 —— 见 :func:`~bilibili_music.audio.resolver.pick_best_cached`。
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from .cache_index import CachedTrack, CacheIndex, entry_for
from .errors import BiliMusicError
from .models import AudioTrack, Page, Video

__all__ = [
    "AudioCache",
    "CHUNK_SIZE",
    "DownloadSink",
    "cache_root",
]

#: 应用在系统缓存目录下使用的子目录名。
_APP_DIR_NAME = "BiliMusic"

#: 缓存写入的块大小(64 KiB)。与 :data:`bilibili_music.core.http.DOWNLOAD_CHUNK`
#: 取值一致:小于磁盘页对齐的收益不明显,大于它则单次写盘的抖动更明显。
CHUNK_SIZE = 1 << 16  # 64 KiB


def cache_root() -> Path:
    """返回平台相关的缓存根目录(不保证已存在)。

    三个平台各走各的惯例,是为了让缓存出现在用户"该清就清"的位置:

    * Windows 用 ``%LOCALAPPDATA%``(不用 ``%APPDATA%`` —— 后者会跟着漫游配置同步)
    * macOS 用 ``~/Library/Caches``(``~/Library/Application Support`` 是放配置的)
    * 其余按 XDG 规范用 ``$XDG_CACHE_HOME``,未设置时退回 ``~/.cache``

    Returns:
        缓存根目录路径;目录本身可能还不存在,由 :class:`AudioCache` 负责创建。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / _APP_DIR_NAME / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / _APP_DIR_NAME
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / _APP_DIR_NAME


class DownloadSink:
    """一次音频下载的写入目标(原子落盘)。

    后端无关:调用方只管 ``write()`` 字节块,收尾时 ``commit()``;
    中途出错必须调用 ``abort()``。

    也支持 ``with`` 用法,但注意 :meth:`__exit__` **不会**自动 ``commit()`` ——
    因为"块写完了"不等于"下载成功了",后端必须明确区分成功与失败两条路径。
    """

    def __init__(self, final_path: Path) -> None:
        """记录最终路径,并推导出临时文件路径。

        临时文件就用最终路径加 ``.part`` 后缀,不另建目录:这样 :meth:`commit`
        的 ``os.replace`` 一定在同一文件系统内,才具备原子性。

        Args:
            final_path: 下载成功后的正式缓存文件路径。
        """
        self.final_path = Path(final_path)
        self.tmp_path = self.final_path.with_suffix(self.final_path.suffix + ".part")
        self._handle = None
        self.bytes_written = 0

    def open(self) -> "DownloadSink":
        """打开临时文件准备写入(父目录不存在时自动创建)。

        Returns:
            ``self``,便于链式调用 ``DownloadSink(p).open()``。
        """
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.tmp_path.open("wb")
        return self

    def write(self, data: bytes) -> int:
        """写入一块数据并累计字节数。

        Args:
            data: 本次要落盘的字节块。

        Returns:
            实际写入的字节数,等于 ``len(data)``(供调用方统一按返回值计进度)。

        Raises:
            BiliMusicError: 忘记先调用 :meth:`open`。
        """
        if self._handle is None:
            raise BiliMusicError("DownloadSink 未打开")
        self._handle.write(data)
        self.bytes_written += len(data)
        return len(data)

    def commit(self) -> Path:
        """收尾并原子替换。空文件视为失败。

        为什么零字节要报错:CDN 偶尔会返回 200 但内容为空(签名过期时的表现之一),
        如果照常落盘,"缓存命中"就会永远播不出声音,而且再也不会重新下载。

        Returns:
            落盘后的正式缓存文件路径(:attr:`final_path`)。

        Raises:
            BiliMusicError: 一个字节都没写到(音源可能已失效)。
        """
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self.bytes_written == 0:
            self.abort()
            raise BiliMusicError("下载得到 0 字节,音源可能已失效")
        os.replace(self.tmp_path, self.final_path)
        return self.final_path

    def abort(self) -> None:
        """放弃本次下载并清理临时文件。可安全重复调用。"""
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
        self.tmp_path.unlink(missing_ok=True)

    def __enter__(self) -> "DownloadSink":
        """进入 ``with`` 块时打开临时文件。"""
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> bool:
        """退出 ``with`` 块:有异常则清理临时文件。

        无论成功与否都返回 ``False``,即不吞掉异常 —— 成功路径的落盘由调用方
        显式 ``commit()``,这里只负责失败时的清理。
        """
        if exc_type is not None:
            self.abort()
            return False
        return False


class AudioCache:
    """按 ``bvid + cid + 音质`` 缓存音频文件。

    缓存键包含 ``cid`` 是**领域铁律**:音乐区的多P合集里每个分P是一首不同的歌,
    键里漏掉 ``cid`` 会让同一视频的不同分P互相覆盖,表现为"切了分P还是上一首"。
    """

    def __init__(self, root: Path | None = None) -> None:
        """绑定缓存目录并确保它存在。

        Args:
            root: 覆盖缓存根目录,主要给测试用沙箱目录。
                ``None`` 表示使用 :func:`cache_root` 的平台默认值。
        """
        self.root = Path(root) if root is not None else cache_root()
        self.root.mkdir(parents=True, exist_ok=True)
        #: 与音频文件同目录的索引(可枚举的元数据)。两者**必须同一个 root**:
        #: 分开了就会出现"清空了音频却留下索引"这类不一致,注入沙箱目录时也容易漏一个。
        self.index = CacheIndex(root=self.root)

    def key_for(self, bvid: str, cid: int, quality_id: int, codec: str = "") -> str:
        """计算缓存键。

        用 SHA-1 摘要截断到 20 位而不是直接拼可读文件名:B站标题里有 Windows
        文件名禁用字符,还可能超过路径长度上限。截 20 位十六进制(80 bit)的碰撞
        概率对本场景足够低,又能保证文件名定长。

        Args:
            bvid: 视频 BV 号。
            cid: **分P**的 cid(不是视频级的,否则多P会串歌)。
            quality_id: 音质档位 id,如 ``30280``。
            codec: 编码串(``mp4a.40.2`` / ``fLaC`` 等)。同一档位可能给不同 codec,
                所以必须参与键计算。

        Returns:
            20 位十六进制字符串。
        """
        raw = f"{bvid}|{cid}|{quality_id}|{codec}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def path_for(self, bvid: str, cid: int, quality_id: int, codec: str = "") -> Path:
        """缓存文件路径。扩展名统一用 ``.m4a``(DASH 音频就是 fMP4 容器)。"""
        return self.root / f"{self.key_for(bvid, cid, quality_id, codec)}.m4a"

    def lookup(self, bvid: str, cid: int, quality_id: int, codec: str = "") -> Path | None:
        """命中缓存则返回路径,否则 ``None``。

        额外校验文件非空:进程被杀可能留下 0 字节文件,把它当成命中会一直播不出声。
        """
        path = self.path_for(bvid, cid, quality_id, codec)
        if path.exists() and path.stat().st_size > 0:
            return path
        return None

    def sink_for(self, bvid: str, cid: int, quality_id: int, codec: str = "") -> DownloadSink:
        """为指定音轨创建一个写入目标(Qt 后端的流式下载用)。

        Returns:
            尚未 ``open()`` 的 :class:`DownloadSink`;Qt 后端的 ``download()``
            会自己打开它,同步路径请改用 :meth:`store_chunks`。
        """
        return DownloadSink(self.path_for(bvid, cid, quality_id, codec))

    # ------------------------------------------------------------ 索引

    def indexed_hit(self, bvid: str, cid: int) -> tuple[CachedTrack, Path] | None:
        """按 ``(bvid, cid)`` 从索引里取音质最高的一条,并确认文件还在。

        与 :meth:`lookup` 的分工:``lookup`` 是"知道键去问文件在不在"(不需要索引),
        这里是"从索引反查已经缓存了什么" —— 它有**真实的档位与 codec**,所以连
        ``fLaC`` 这类不在常规三档里的缓存也能命中,离线点播因此不会因为索引里记的
        codec 与盲查猜的不一样而落空。

        索引条目指向的文件可能已经被用户手删,所以这里必须再 ``stat`` 一次:
        "索引里有"不等于"文件还在"。

        Args:
            bvid: 视频 BV 号。
            cid: 分P的 cid。

        Returns:
            ``(索引记录, 音频文件路径)``;索引里没有这个分P,或对应文件已不在
            (或为 0 字节)时返回 ``None``。
        """
        entry = self.index.find(bvid, cid)
        if entry is None:
            return None
        path = self.root / entry.file_name
        try:
            if path.stat().st_size > 0:
                return entry, path
        except OSError:
            pass
        return None

    def remember(
        self, video: Video, page: Page, track: AudioTrack, path: Path
    ) -> bool:
        """把一次成功的解析结果写进索引(供"本地缓存"页枚举)。

        由解析流程在**成功出口**调用,命中缓存与刚下载完两条路径都会走:前者顺带把
        "索引里还没有它"(例如索引丢了、或这首歌是在有索引之前缓存的)补上。

        文件大小在这里量:它是界面要显示的信息,而调用方(解析器)手上没有 stat。

        Args:
            video: 目标视频。
            page: 目标分P。
            track: 实际落盘的音轨。
            path: 音频文件路径。

        Returns:
            索引内容真的变了并尝试落盘返回 ``True``;完全没变返回 ``False``。
            **写盘失败不抛异常**,只当这次记录作废(索引不该影响播放)。
        """
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass
        return self.index.remember(entry_for(video, page, track, path, size_bytes=size))

    def forget(self, entry: CachedTrack) -> bool:
        """删掉某条缓存:音频文件 + 索引记录。

        先删文件再删索引:反过来的话,文件删不掉(Windows 不允许删已打开的文件,而它
        很可能正被播放器占着)就会留下一个谁也看不见、却实实在在占着磁盘的孤儿文件。

        Args:
            entry: 要删除的索引记录。

        Returns:
            成功删除返回 ``True``;文件被占用或无权限时返回 ``False`` ——
            此时**索引保持不动**,界面上的这一首还在,用户可以停掉播放再删一次。
        """
        path = self.root / entry.file_name
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # 文件本来就不在了(用户手删过),索引照样要清掉
        except OSError:
            return False
        self.index.forget(entry.key)
        return True

    def prune(self) -> int:
        """剪掉"音频文件已经不在"的索引条目。

        Returns:
            被剪掉的条目数;索引与磁盘一致时是 ``0``(也不会落盘)。
        """
        return self.index.retain_files(self._audio_file_names())

    def _audio_file_names(self) -> list[str]:
        """列出缓存目录里现存的音频文件名(``prune`` / ``clear`` 的判据)。"""
        return [path.name for path in self.root.glob("*.m4a") if path.is_file()]

    def store_chunks(
        self,
        bvid: str,
        cid: int,
        quality_id: int,
        codec: str,
        chunks: Iterable[bytes],
    ) -> Path:
        """把字节块迭代器写入缓存(urllib 后端的同步路径)。

        全程只有一份缓冲区,边收边写盘,所以两小时的合集也不会把内存吃满。

        Args:
            bvid: 视频 BV 号。
            cid: 分P的 cid。
            quality_id: 音质档位 id。
            codec: 编码串。
            chunks: 字节块迭代器。空块会被跳过。

        Returns:
            落盘后的正式缓存文件路径。

        Raises:
            BiliMusicError: 迭代完得到 0 字节(音源失效)。
            其他异常:迭代过程中抛出的任何异常都会被原样重抛,
                但会先 ``abort()`` 清掉临时的 ``.part`` 文件。
        """
        sink = self.sink_for(bvid, cid, quality_id, codec).open()
        try:
            for chunk in chunks:
                if chunk:
                    sink.write(chunk)
            return sink.commit()
        except BaseException:
            sink.abort()
            raise

    def size_bytes(self) -> int:
        """统计缓存占用的总字节数(供界面显示"缓存占用")。"""
        return sum(p.stat().st_size for p in self.root.glob("*.m4a") if p.is_file())

    def clear(self) -> int:
        """清空缓存,返回删除的文件数。

        单个文件删不掉时**跳过而不是中断**:缓存目录里可能有正被播放器占用的
        文件(Windows 不允许删除已打开的文件),为它放弃清理其余文件并不划算。

        删完要把索引与磁盘对齐(见 :meth:`prune`):否则"删不掉的那几个文件"会连着
        索引记录一起消失,变成谁也看不见、却实实在在占着磁盘的孤儿 —— 界面于是显示
        "已清空 0 首"但缓存占用纹丝不动,用户完全无从理解。

        Returns:
            成功删除的 ``.m4a`` 文件数(不含顺带清理的 ``.part`` 残留)。
        """
        removed = 0
        for path in self.root.glob("*.m4a"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        # 上次异常退出遗留的临时文件也一并清掉,它们永远不会被复用
        for path in self.root.glob("*.part"):
            path.unlink(missing_ok=True)
        self.prune()
        return removed
