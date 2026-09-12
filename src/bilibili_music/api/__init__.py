"""B站接口封装与解析。只依赖 ``core`` / ``net``,不依赖 Qt 以外的界面组件。

对外只暴露两样东西:

* ``parse_*`` 纯函数 —— 输入接口原始 JSON、输出数据模型,不碰网络与 Qt
* :class:`BilibiliClient` —— 负责发请求,把解析结果交给回调

把两者拆开是为了让接口解析能用固定样本单测(``tests/test_api_parsing.py``),
不触网也就不会被风控和网络抖动干扰。因此**不要**往 ``parse_*`` 里加任何请求逻辑。
"""

from .bilibili import (
    BilibiliClient,
    SearchResult,
    parse_audio_tracks,
    parse_search_result,
    parse_video,
    pick_best,
)

__all__ = [
    "BilibiliClient",
    "SearchResult",
    "parse_audio_tracks",
    "parse_search_result",
    "parse_video",
    "pick_best",
]
