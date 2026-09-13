"""界面组件(可单独构造、可单独测的 widget)。

主窗口负责**组装**,具体控件放在这里。拆开的原因是 ``main_window.py`` 曾经既管布局又管
业务编排,加一个面板就要在一个几百行的文件里找位置;拆开之后每块界面有自己的信号与方法,
主窗口只做"接线"。

* :mod:`.window_frame` —— ``FramelessWindow``,无边框窗口与八个边缘缩放把手
* :mod:`.title_bar` —— ``TitleBar``,自绘标题栏(应用图标 / 搜索框 / 窗口按钮)
* :mod:`.sidebar` —— ``Sidebar``,左侧导航与"我的歌单"
* :mod:`.track_list` —— ``TrackList``,搜索结果与播放队列共用的曲目列表
* :mod:`.player_bar` —— ``PlayerBar``,底部播放条
* :mod:`.page_selector` —— ``PageSelector``,播放条上的分P选择器与它的弹出菜单
* :mod:`.queue_drawer` —— ``QueueDrawer``,右侧播放队列面板
* :mod:`.placeholder` —— ``PlaceholderPage``,尚未实现的功能的占位页
* :mod:`.elided_label` —— ``ElidedLabel``,按宽度省略过长文字的标签

这些控件**不含业务判断**:它们只把用户操作转成信号,把上层给的数据渲染成界面。
"下一首放什么"这类决定一律在 :mod:`bilibili_music.audio.playback` 里做。
"""

from .elided_label import ElidedLabel
from .page_selector import PageSelector
from .placeholder import PlaceholderPage
from .player_bar import PlayerBar
from .queue_drawer import QueueDrawer
from .sidebar import Sidebar
from .title_bar import TitleBar
from .track_list import TrackList, TrackRow
from .window_frame import FramelessWindow

__all__ = [
    "ElidedLabel",
    "FramelessWindow",
    "PageSelector",
    "PlaceholderPage",
    "PlayerBar",
    "QueueDrawer",
    "Sidebar",
    "TitleBar",
    "TrackList",
    "TrackRow",
]
