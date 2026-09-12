"""纯逻辑的 HTTP 辅助:请求头构造与内容解码。

这个模块**刻意不导入 Qt、也不做任何网络 IO**,所以可以脱离事件循环直接单测。
两种后端(``net.urllib_client`` 与 ``net.client``)共用这里的实现,保证
"发什么头、怎么解压"这两件容易出错的事只有一份代码。

这里放的都是**实测踩出来的结论**,改动前建议先看 ``README.md`` 的「B站接口实测笔记」:
``Referer`` 是防盗链的硬要求,``Origin`` 在下载音频时反而要摘掉,
而 ``Accept-Encoding`` 一旦声明就必须自己解压(urllib 与 QNAM 都不会代劳)。
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Iterable

from .errors import NetworkError

__all__ = [
    "API_BASE",
    "APP_HEADERS",
    "DEFAULT_UA",
    "HOME",
    "IncrementalDecoder",
    "REFERER",
    "api_headers",
    "decompress_all",
    "iter_chunks",
    "make_decompressor",
    "media_headers",
]

#: 音频 CDN 的防盗链要求:缺了这个 ``Referer`` 会**直接 403**。
REFERER = "https://www.bilibili.com/"
#: 会话预热的入口。必须访问它才能拿到 ``buvid3`` / ``b_nut`` ——
#: B站 API 响应本身不下发这两个 Cookie,只有主页会。
HOME = "https://www.bilibili.com/"
#: API 域名。与主页分开是因为两者的请求头要求不同(见 :func:`media_headers`)。
API_BASE = "https://api.bilibili.com"

#: 用真实浏览器 UA,降低被风控拦截的概率。
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: 应用发出的标识,便于排查问题时区分自己发出的请求。
APP_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def api_headers(*, user_agent: str = DEFAULT_UA, with_compression: bool = True) -> dict[str, str]:
    """构造访问 B站 JSON 接口用的请求头。

    与 :func:`media_headers` 的差别是这里**带 ``Origin``**、且默认声明压缩:
    JSON 接口走的是普通 XHR 语义,带 ``Origin`` 与浏览器行为一致,
    而响应体是文本,声明 gzip 能显著省流量。

    Args:
        user_agent: 覆盖默认 UA。仅在需要排查"UA 是否被针对"时才传。
        with_compression: 是否声明 ``Accept-Encoding``。注意声明了就必须自己解压,
            见 :func:`make_decompressor`。

    Returns:
        可直接交给 ``QNetworkRequest`` 或 ``urllib.request.Request`` 的头部字典。
    """
    headers = {
        "User-Agent": user_agent,
        "Referer": REFERER,
        "Origin": "https://www.bilibili.com",
        **APP_HEADERS,
    }
    if with_compression:
        headers["Accept-Encoding"] = "gzip, deflate"
    return headers


def media_headers(*, user_agent: str = DEFAULT_UA) -> dict[str, str]:
    """构造下载音视频流用的请求头。

    与 :func:`api_headers` 的三个关键差别,都是踩坑换来的:

    * **保留 Referer** —— CDN 有防盗链,缺了直接 403
    * **摘掉 Origin** —— 它会触发 CORS 校验,可能导致 CDN 拒绝
    * 不声明 ``Accept-Encoding`` —— 音视频是已压缩数据,再压一遍只是浪费 CPU

    Args:
        user_agent: 覆盖默认 UA。

    Returns:
        可直接交给 ``QNetworkRequest`` 或 ``urllib.request.Request`` 的头部字典。
    """
    return {
        "User-Agent": user_agent,
        "Referer": REFERER,
        "Accept": "*/*",
        "Accept-Language": APP_HEADERS["Accept-Language"],
    }


def make_decompressor(encoding: str):
    """按 ``Content-Encoding`` 造一个增量解压函数。

    返回 ``callable(bytes) -> bytes``;未知编码时返回 ``None``,调用方应直接透传。

    为什么要增量解压:流式下载时不能等收完再解压,否则大文件会把内存吃满。
    ``zlib.decompressobj`` 支持分块喂入。deflate 有 zlib 包裹和裸流两种,
    后者在 HTTP 里很常见,必须处理。

    Args:
        encoding: 响应头 ``Content-Encoding`` 的原始取值,大小写与空白都会在内部归一化。

    Returns:
        一次性解压函数(注意它是**一次性**的,流式场景请用 :class:`IncrementalDecoder`);
        编码为 ``identity`` / 空 / 未知时返回 ``None``,表示应当原样透传。

    Raises:
        OSError: 数据损坏或流被截断(gzip 与 deflate 两种分支都会抛出)。
            调用方如果不希望处理标准库异常,请改用 :func:`decompress_all` ——
            它会把同样的失败统一转成 :class:`~bilibili_music.core.errors.NetworkError`。
    """
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return None
    if encoding == "gzip":
        return gzip.decompress
    if encoding == "deflate":
        # 先按 zlib 包裹试;PHP 等后端常发裸 deflate。用 -MAX_WBITS 兜底。
        def _inflate(data: bytes) -> bytes:
            """解压 deflate:先按 zlib 包裹试,失败再按裸流试。"""
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)

        return _inflate
    return None


class IncrementalDecoder:
    """流式解压器:收一块解一块,内存占用与总大小无关。

    与 :func:`make_decompressor` 的区别是实例持有 ``zlib.decompressobj`` 状态,
    可以跨多次 ``feed()`` 处理被 TCP 切碎的压缩流 —— 后者只能处理完整的一段。

    编码未知时构造出的对象 ``active`` 为 ``False``,``feed()`` 会原样透传,
    这样调用方不必在每个回调里判断"到底要不要解压"。
    """

    def __init__(self, encoding: str) -> None:
        """按 ``Content-Encoding`` 准备解压状态。

        Args:
            encoding: 响应头取值。空串用于"响应头还没到、编码未知"的初始状态,
                此时不建解压器,等拿到真实编码后由调用方重建一个实例。
        """
        self.encoding = (encoding or "").strip().lower()
        self._obj = None
        if self.encoding in ("gzip", "deflate"):
            # gzip 的 zlib 包裹与 deflate 相同,统一用 wbits 处理
            # 16 + MAX_WBITS 表示 gzip 头
            wbits = zlib.MAX_WBITS | 16 if self.encoding == "gzip" else zlib.MAX_WBITS
            self._obj = zlib.decompressobj(wbits)
        elif self.encoding == "deflate-raw":
            self._obj = zlib.decompressobj(-zlib.MAX_WBITS)

    @property
    def active(self) -> bool:
        """是否真的需要解压(``False`` 表示数据原样透传)。"""
        return self._obj is not None

    def feed(self, data: bytes) -> bytes:
        """喂入一块压缩数据,返回已解压出的明文(可能为空)。

        解压器有内部缓冲,一块输入不一定立刻产出一块输出,所以空返回值不代表出错。

        Args:
            data: 本次收到的原始字节(可能只是压缩流的一个片段)。

        Returns:
            本次能解出的明文;``active`` 为 ``False`` 时原样返回 ``data``。

        Raises:
            zlib.error: gzip 流损坏。裸 deflate 会先重置解压器再试一次,
                只有 gzip 才直接抛出 —— 因为 gzip 头能明确判断是数据坏了,
                而 deflate 的失败往往只是"猜错了包裹格式"。
        """
        if self._obj is None:
            return data
        try:
            out = self._obj.decompress(data)
        except zlib.error:
            # 裸 deflate 兜底:重置后按无头模式再试一次
            if self.encoding != "gzip":
                self._obj = zlib.decompressobj(-zlib.MAX_WBITS)
                out = self._obj.decompress(data)
            else:
                raise
        return out

    def finish(self) -> bytes:
        """收尾,冲刷解压器缓冲的剩余数据。

        必须在响应结束时调用一次,否则最后几个字节会留在解压器里丢失,
        表现为 JSON 结尾被截断、解析失败。

        Returns:
            缓冲区里剩余的明文;未启用解压或流已损坏时返回空字节串。
        """
        if self._obj is None:
            return b""
        try:
            return self._obj.flush()
        except zlib.error:
            return b""


def decompress_all(payload: bytes, encoding: str) -> bytes:
    """一次性解压(用于非流式场景)。

    注意:我们在请求头里声明了 ``Accept-Encoding: gzip``,而 **urllib 和 Qt 都
    不会自动解压**。漏掉这一步会拿到二进制乱码并在 JSON 解析处报错。

    Args:
        payload: 完整的响应体原始字节。
        encoding: 响应头 ``Content-Encoding`` 的取值。

    Returns:
        解压后的字节;编码为 ``identity`` / 空 / 未知时原样返回 ``payload``,
        交由上层解析时报错(比在这里猜编码更容易定位问题)。

    Raises:
        NetworkError: gzip 或 deflate 数据损坏、流被截断。
    """
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return payload
    if encoding == "gzip":
        try:
            return gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise NetworkError(f"gzip 解压失败: {exc}") from exc
    if encoding == "deflate":
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(payload, wbits)
            except zlib.error:
                continue
        raise NetworkError("deflate 解压失败:既不是 zlib 包裹也不是裸流")
    # 未知编码:原样返回,交由上层解析时报错
    return payload


def iter_chunks(chunks: Iterable[bytes], sink_chunk: int = 1 << 16) -> Iterable[bytes]:
    """把任意大小的字节序列重新切成固定大小的块,便于写入与上报进度。

    网络层给出的块大小完全不可控(Qt 可能一次给 1KB,也可能给 1MB),
    统一成定长块之后,进度回调的频率与写盘次数才是可预期的。

    Args:
        chunks: 原始字节块序列。
        sink_chunk: 目标块大小(字节),默认 64 KiB。

    Yields:
        长度不超过 ``sink_chunk`` 的字节块;最后一块可能更短,不补齐。
        输入中的空块会被自然跳过(不产出空块)。
    """
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        while len(buffer) >= sink_chunk:
            yield bytes(buffer[:sink_chunk])
            del buffer[:sink_chunk]
    if buffer:
        yield bytes(buffer)
