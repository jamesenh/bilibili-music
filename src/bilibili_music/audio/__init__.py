"""音源解析与播放控制。

* :mod:`.resolver` —— ``AudioResolver``,把视频分P异步解析成本地音频文件
* :mod:`.downloader` —— ``Downloader``,批量缓存的串行调度(只下载、不播放)
* :mod:`.player` —— ``PlayerController``,``QMediaPlayer`` 的薄封装
* :mod:`.playback` —— ``PlaybackController``,把队列、解析器与播放器编排成自动前进

这一层是"业务与界面之间"的最后一段:它已经能碰 Qt(播放器、信号),
但**不含任何界面代码**,也不直接改控件。界面只订阅信号、转发用户操作。
"""

from .downloader import Downloader
from .playback import PlaybackController
from .player import PlayerController
from .resolver import AudioResolver, CachedHit, ResolvedAudio, pick_best_cached

__all__ = [
    "AudioResolver",
    "CachedHit",
    "Downloader",
    "PlaybackController",
    "PlayerController",
    "ResolvedAudio",
    "pick_best_cached",
]
