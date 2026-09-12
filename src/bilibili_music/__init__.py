"""聚合哔哩哔哩音乐视频的桌面音乐客户端。

分层约定:
    core/   与 UI 无关的纯逻辑(HTTP、数据模型、缓存、错误),可独立单测
    api/    B站接口封装,只依赖 core
    audio/  音源解析与播放,依赖 core 与 QtMultimedia
    ui/     PySide6 界面,依赖以上各层
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
