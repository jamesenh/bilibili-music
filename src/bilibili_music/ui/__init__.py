"""PySide6 界面层。

* :mod:`.main_window` —— MVP 主窗口
* :mod:`.icons` —— 图标资源与运行时着色

界面层依赖下面所有层(``core`` / ``net`` / ``api`` / ``audio``),但依赖方向仍然
单向:这一层只被入口脚本引用,自己不再被任何业务层 import。
同理,界面里不写解析与业务判断,只做展示与事件转发。
"""

from .main_window import MainWindow, run

__all__ = ["MainWindow", "run"]
