"""界面组件(可单独构造、可单独测的 widget)。

主窗口负责**组装**,具体控件放在这里。拆开的原因是 ``main_window.py`` 曾经既管
布局又管业务编排,加一个面板就要在一个几百行的文件里找位置;拆开之后每块界面有
自己的信号与方法,主窗口只做"接线"。

* :mod:`.track_list` —— ``TrackList``,搜索结果与播放队列共用的曲目表格
* :mod:`.player_bar` —— ``PlayerBar``,底部播放条(传输控件 / 进度 / 音量 / 模式 / 音质)
* :mod:`.queue_drawer` —— ``QueueDrawer``,右侧可折叠的播放队列

这些控件**不含业务判断**:它们只把用户操作转成信号,把上层给的数据渲染成界面。
"下一首放什么"这类决定一律在 :mod:`bilibili_music.audio.playback` 里做。
"""

from .player_bar import PlayerBar
from .queue_drawer import QueueDrawer
from .track_list import TrackList

__all__ = ["PlayerBar", "QueueDrawer", "TrackList"]
