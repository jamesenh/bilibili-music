"""PySide6 界面层。

* :mod:`.main_window` —— 主窗口(只做组装与接线)
* :mod:`.widgets` —— 播放条 / 曲目列表 / 队列抽屉
* :mod:`.icons` —— 图标资源与运行时着色
* :mod:`.theme` —— 浅色 / 深色主题的应用级 `QPalette`
* :mod:`.cover_loader` —— 封面下载与内存缓存

界面层依赖下面所有层(``core`` / ``net`` / ``api`` / ``audio``),但依赖方向仍然
单向:这一层只被入口脚本引用,自己不再被任何业务层 import。
同理,界面里不写解析与业务判断,只做展示与事件转发。
"""

from .main_window import MainWindow, run

__all__ = ["MainWindow", "run"]
