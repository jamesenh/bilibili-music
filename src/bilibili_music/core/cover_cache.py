"""封面磁盘缓存:把封面图片按 URL 落到本地文件。

为什么需要它
------------

封面原先只有 Qt 的 ``QPixmapCache``(进程内、按容量淘汰),进程一退就全丢:每次重开
应用,首屏十几行封面都要重新走一遍 CDN。而 ``BilibiliClient.fetch_cover`` 走的是
``HttpBackend.get_bytes``,与 API 请求**共用同一个限速器**,封面加载器又是"一次只发
一张"的串行队列 —— 于是冷启动时首屏封面要十几秒才能填满。落盘之后第二次启动是本地
读文件,完全不受限速约束。

设计取舍
--------

* **文件名 = URL 的 SHA-1 前 20 位**(与 :class:`~bilibili_music.core.cache.AudioCache`
  同一套路):B站封面地址又长又带查询串,直接拿来当文件名会超过路径长度上限。
* **扩展名按图片魔数推断**(``.jpg`` / ``.webp`` / ``.png`` …):缓存目录是用户
  "该清就清"的地方,能双击预览总比一串十六进制强。认不出的格式退回 ``.img`` ——
  读回时按内容解码,后缀叫什么并不影响正确性。
* **写入原子**:先写 ``.part`` 再 ``os.replace``,与音频缓存同一条纪律。否则进程被杀
  会留下半截图,而"文件存在即完整"这条前提一旦破了,界面就会挂上永远贴不出来的封面。
* **有容量上限**:音频是用户主动点的,封面却是**顺手**缓存的 —— 只是翻列表就会攒下
  几百 MB,所以这里必须自己兜住总量(见 :meth:`CoverCache._trim`)。
* **失败一律静默**:缓存是锦上添花。磁盘满、目录没权限都只让这次落盘作废,绝不能因此
  让封面贴不出来。

本模块**不 import Qt**(``core`` 的红线):格式只按字节头判断,解码与贴图留给
``ui/cover_loader.py``。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

from .cache import cache_root

__all__ = [
    "CoverCache",
    "COVERS_DIR_NAME",
    "DEFAULT_MAX_BYTES",
]

#: 封面缓存放在缓存根目录下的子目录名。与音频文件分开:用户清理时能分辨
#: "这些是可以再下载回来的图,那些是我特意缓存的歌"。
COVERS_DIR_NAME = "covers"

#: 封面缓存的默认容量上限(200 MiB)。单张封面几十到几百 KB,这个量级够存几千张;
#: 封面是可再生资源,不值得为它占更多磁盘。
DEFAULT_MAX_BYTES = 200 * 1024 * 1024

#: 清理后的目标水位(上限的 90%)。留余量是为了避免"刚删完又超"—— 每次落盘都触发
#: 一轮清理会让磁盘一直抖,而这个缓存的价值不值得这种代价。
_LOW_WATER_RATIO = 0.9

#: 图片魔数 -> 扩展名。顺序即 :meth:`CoverCache.lookup` 的查找顺序:B站封面绝大多数
#: 是 jpg / webp,排在前面可以少几次 ``stat``。
_MAGIC_SUFFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"RIFF", ".webp"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
)

#: 认不出格式时的兜底后缀。读回时一律按内容解码,所以它只影响文件名好不好看。
_FALLBACK_SUFFIX = ".img"

#: ``lookup`` / ``discard`` 依次尝试的后缀(与 :data:`_MAGIC_SUFFIXES` 同源)。
_CANDIDATE_SUFFIXES: tuple[str, ...] = tuple(
    suffix for _, suffix in _MAGIC_SUFFIXES
) + (_FALLBACK_SUFFIX,)

#: 原子写入用的临时后缀(临时文件与正式文件同目录,``os.replace`` 才能保证原子)。
_TMP_SUFFIX = ".part"


def _silent_unlink(path: Path) -> bool:
    """尽力删掉一个文件,失败当没发生。

    缓存目录里可能有别的东西占着文件(Windows 不允许删除已打开的文件),为它中断
    清理或抛异常都不划算,所以统一走这个"删不掉就算了"的入口。

    Args:
        path: 要删除的文件;不存在也算成功。

    Returns:
        真的删掉了返回 ``True``,文件不存在或删不掉返回 ``False``。
    """
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _sniff_suffix(data: bytes) -> str:
    """按图片魔数推断扩展名。

    必须看魔数而不是信后端给的 URL 后缀:同一个地址在不同 CDN 节点上可能返回 jpg 或
    webp,文件名与实际内容不符会让用户双击时打不开。

    Args:
        data: 图片原始字节。

    Returns:
        形如 ``.jpg`` 的后缀;认不出的格式返回 :data:`_FALLBACK_SUFFIX`。
    """
    for magic, suffix in _MAGIC_SUFFIXES:
        if not data.startswith(magic):
            continue
        # RIFF 是容器头,WAV / AVI 与 WEBP 共用它,必须再看 FourCC 才能确定是图片
        if magic == b"RIFF" and data[8:12] != b"WEBP":
            continue
        return suffix
    return _FALLBACK_SUFFIX


class CoverCache:
    """按封面 URL 缓存图片文件的磁盘缓存。

    Args:
        root: 缓存目录;``None`` 表示 :func:`~bilibili_music.core.cache.cache_root`
            下的 ``covers`` 子目录。
        max_bytes: 缓存总量上限(字节);``<= 0`` 表示不设上限(测试与"我就是要全留着"
            的场景用)。
    """

    def __init__(
        self, root: Path | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> None:
        """绑定缓存目录与容量上限。

        与 :class:`~bilibili_music.core.cache.AudioCache` 不同,这里**不在构造时建目录**:
        主窗口一启动就会构造它,但用户可能一张封面都没取过,没必要先占一个空目录。
        目录推迟到首次 :meth:`store` 时创建。

        Args:
            root: 缓存目录;``None`` 表示平台默认的封面缓存目录。
            max_bytes: 缓存总量上限(字节)。
        """
        self.root = Path(root) if root is not None else cache_root() / COVERS_DIR_NAME
        self.max_bytes = max_bytes
        #: 缓存总量的估算值。``None`` 表示还没量过:首次落盘时扫一遍目录初始化,
        #: 之后每次落盘只按写入的字节数累加,不必反复扫盘(见 :meth:`_trim`)。
        self._approx_bytes: int | None = None

    # ------------------------------------------------------------ 键与路径

    def key_for(self, url: str) -> str:
        """计算某个封面 URL 的缓存键。

        用 SHA-1 摘要截到 20 位(80 bit)而不是拼可读文件名:B站封面地址里带
        ``/`` 与查询串,Windows 文件名禁用字符与路径长度上限两头都过不去。

        Args:
            url: 封面地址(调用方传的已是 https,但本方法不关心协议)。

        Returns:
            20 位十六进制字符串(不含扩展名)。
        """
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]

    def lookup(self, url: str) -> Path | None:
        """按 URL 找缓存文件。

        依次试各个已知后缀 —— 缓存目录是自己的私有子目录,与其维护一份索引文件
        (那是 M2.1 的事),不如多几次 ``stat``:最多 6 次本地系统调用,可忽略。

        Args:
            url: 封面地址;空串直接视为未命中。

        Returns:
            存在的缓存文件路径;没有、或文件是 0 字节(上次写到一半被杀)时返回
            ``None``。
        """
        if not url:
            return None
        key = self.key_for(url)
        for suffix in _CANDIDATE_SUFFIXES:
            path = self.root / f"{key}{suffix}"
            try:
                if path.stat().st_size > 0:
                    return path
            except OSError:
                continue
        return None

    def read(self, url: str) -> bytes | None:
        """读取缓存里的封面字节。

        整块读进内存而不流式:单张封面几十到几百 KB,调用方本来也要整块喂给
        ``QPixmap.loadFromData``,分块只会多一层循环。

        Args:
            url: 封面地址。

        Returns:
            图片原始字节;未命中、空文件或读失败(被并发清掉、磁盘错误)返回 ``None``。
        """
        path = self.lookup(url)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data or None

    # ------------------------------------------------------------ 写入与清理

    def store(self, url: str, data: bytes) -> Path | None:
        """把一张封面原子写入缓存。

        写完顺手清掉同一 URL 的其它后缀文件(格式换过之后残留的旧文件),否则同一个
        地址会在目录里留下多份,用户看着像重复。

        Args:
            url: 封面地址。
            data: 图片原始字节;空字节直接忽略(不该留下 0 字节的"命中")。

        Returns:
            落盘后的文件路径;写不进去(磁盘满 / 无权限)时返回 ``None``,**不抛异常**
            —— 缓存失败不该影响这一次的封面显示。
        """
        if not url or not data:
            return None
        final = self.root / f"{self.key_for(url)}{_sniff_suffix(data)}"
        tmp = final.with_suffix(final.suffix + _TMP_SUFFIX)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, final)
        except OSError:
            # 失败路径也要收尾:临时文件可能已经建出来了,留着永远不会被复用
            _silent_unlink(tmp)
            return None
        # 删掉的那份不让估算值减少:格式切换极少发生,让它偏大一点无害,
        # 下次真的超限时扫盘会把精确值校准回来(见 _trim)
        self._drop_files(url, keep=final)
        # 估算值只在量过一次之后才维护:没量过说明还没到该关心容量的时候
        if self._approx_bytes is not None:
            self._approx_bytes += len(data)
        self._trim()
        return final

    def discard(self, url: str) -> None:
        """删掉某个 URL 的所有缓存文件(可安全重复调用)。

        解不出图片的坏条目必须清掉:留着的话每次加载都要白读一遍盘,而"重新下载再
        覆盖"未必发生(网络也可能一直失败)。

        Args:
            url: 封面地址;空串不做任何事。
        """
        self._drop_files(url, keep=None)

    def clear(self) -> int:
        """清空封面缓存,返回删除的文件数。

        单个文件删不掉时**跳过而不是中断**(正被图片查看器打开的文件在 Windows 上
        删不掉),与 :meth:`AudioCache.clear` 同一条口径。

        Returns:
            成功删除的文件数(含顺带清理的 ``.part`` 残留)。
        """
        removed = 0
        for path in self._iter_files():
            if _silent_unlink(path):
                removed += 1
        self._approx_bytes = 0
        return removed

    def size_bytes(self) -> int:
        """统计封面缓存占用的总字节数(供界面显示"缓存占用")。"""
        total = 0
        for path in self._iter_files():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    # ------------------------------------------------------------ 内部

    def _drop_files(self, url: str, *, keep: Path | None) -> None:
        """删掉某个 URL 在各个后缀下的缓存文件。

        Args:
            url: 封面地址;空串直接返回。
            keep: 需要保留的文件(刚写入的那份);``None`` 表示全删。
        """
        if not url:
            return
        key = self.key_for(url)
        for suffix in _CANDIDATE_SUFFIXES:
            path = self.root / f"{key}{suffix}"
            if keep is not None and path == keep:
                continue
            _silent_unlink(path)

    def _iter_files(self) -> Iterator[Path]:
        """遍历缓存目录里的普通文件(目录不存在时什么都不产出)。

        ``.part`` 残留也在其中:大小统计会把它算进去(它确实占着磁盘),由
        :meth:`clear` 负责清掉。
        """
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for path in entries:
            try:
                if path.is_file():
                    yield path
            except OSError:
                continue

    def _trim(self) -> None:
        """超过容量上限就把最旧的封面删到低水位。

        淘汰依据是**写入时间**(``mtime``)而不是访问时间:多数文件系统的 ``atime``
        被挂载参数关掉或只在日期变化时更新(``relatime``),靠它做 LRU 只会得到一堆
        假命中。写入时间实际等价于"最早缓存的先删",对封面这种可再生资源足够了。

        只在估算值超标时才扫盘,**并且以扫出来的精确值为准**:估算值会因为用户手动
        删过文件、或上次进程异常退出而偏高,照它删就会误删刚存下的好图。
        """
        if self.max_bytes <= 0:
            return
        if self._approx_bytes is None:
            self._approx_bytes = self.size_bytes()
        if self._approx_bytes <= self.max_bytes:
            return

        entries: list[tuple[float, int, Path]] = []
        for path in self._iter_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        self._approx_bytes = total
        if total <= self.max_bytes:
            return

        target = int(self.max_bytes * _LOW_WATER_RATIO)
        entries.sort(key=lambda item: item[0])  # 旧 -> 新
        for _, size, path in entries:
            if total <= target:
                break
            if _silent_unlink(path):
                total -= size
        self._approx_bytes = total
