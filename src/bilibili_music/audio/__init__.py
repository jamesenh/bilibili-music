"""音源解析与播放控制。

* :mod:`.resolver` —— ``AudioResolver``,把视频分P异步解析成本地音频文件
* :mod:`.player` —— ``PlayerController``,``QMediaPlayer`` 的薄封装

这一层是"业务与界面之间"的最后一段:它已经能碰 Qt(播放器、信号),
但**不含任何界面代码**,也不直接改控件。界面只订阅信号、转发用户操作。
"""

from .player import PlayerController
from .resolver import AudioResolver, ResolvedAudio, pick_best_cached

__all__ = [
    "AudioResolver",
    "PlayerController",
    "ResolvedAudio",
    "pick_best_cached",
]
