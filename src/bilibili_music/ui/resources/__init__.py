"""静态资源目录。

``icons/``  线性 SVG 图标源文件(单色蒙版,运行时着色),见 :mod:`bilibili_music.ui.icons`。
``app/``    应用图标位图(``app.ico`` / ``app.png``)及其源 ``app.svg``。

放一个 ``__init__.py`` 是为了让它成为**规范的包目录** —— hatchling 配置里
``packages = ["src/bilibili_music"]`` 会连带打进包内数据文件,显式成包可以避免
"资源没进 wheel,装完就找不到图标"这类只有打包后才暴露的问题。

当前 ``icons/`` 下的图标清单(改这里就要同步 ``icons.py`` 的 ``__all__`` 说明):

* 播放控制:``play`` / ``pause`` / ``stop`` / ``prev`` / ``next`` / ``repeat``
* 音量:``volume`` / ``volume-mute``
* 内容操作:``search`` / ``refresh`` / ``download`` / ``heart`` / ``list``
* 其它:``close``(关窗)、``music``(封面缺失时的占位图)

注意**没有 ``shuffle``**(随机播放图标)与"单曲循环"的 repeat 变体 ——
接播放队列时需按 ``icons.py`` 的约定新增(``viewBox="0 0 24 24"``、
可见图元统一 ``fill="#000000"`` 当单色蒙版,染色交给 ``render_pixmap``)。
"""
