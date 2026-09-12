"""各层共用的异常类型。

设计取舍:只用一个基类 ``BiliMusicError`` 分四支,而不是按层各建一套异常。

* 各层抛出的都是同一族异常,``ui`` 层只要 ``except BiliMusicError`` 就能兜住
  所有"可预期的失败",不会漏掉某一层;
* 分支按**失败原因**划分(网络 / 接口业务码 / 没有音源),而不是按抛出它的模块,
  因为调用方真正关心的是"该怎么补救",不是"谁抛的"。

异常消息里可以带英文技术标识(URL、字段名、HTTP 状态码),但面向用户的那层
(界面提示)必须是中文,见 ``AGENTS.md`` 第 5 节第 9 条。
"""

from __future__ import annotations

__all__ = [
    "ApiError",
    "BiliMusicError",
    "NetworkError",
    "NoAudioSourceError",
]


class BiliMusicError(Exception):
    """本项目所有自定义异常的基类,便于在 UI 层统一兜底。"""


class NetworkError(BiliMusicError):
    """网络层失败:超时、连接中断、DNS 等。"""


class ApiError(BiliMusicError):
    """B站接口返回了非 0 的业务错误码。

    ``code`` / ``message`` / ``url`` 单独留成属性,而不是只拼进异常消息:
    排查风控问题时经常需要只看状态码(如 ``-412``),从字符串里反解太别扭。
    """

    def __init__(self, code: int, message: str, *, url: str | None = None) -> None:
        """记录业务错误码与原始消息。

        异常消息按 ``code=... message=...`` 的格式拼装,方便日志grep;
        面向用户的展示由上层另行组织。

        Args:
            code: 接口响应体里的 ``code`` 字段(非 0 才会走到这里)。
            message: 接口响应体里的 ``message`` 字段,通常是中文提示。
            url: 出错的请求地址,便于定位是哪个接口被拒。``None`` 表示调用方未提供。
        """
        super().__init__(f"接口错误 code={code} message={message!r}")
        self.code = code
        self.message = message
        self.url = url


class NoAudioSourceError(BiliMusicError):
    """该视频没有任何可用的音频流(付费、已下架、地区限制等)。"""
