"""与 UI 无关的核心设施:异常体系、数据模型、HTTP 纯逻辑与磁盘缓存。

这一层是分层依赖的**最底层**,只允许依赖标准库 —— 不导入 Qt,也不认识 ``net`` /
``api`` / ``audio`` / ``ui``。这样做的目的是让"解析、格式化、解压、原子写盘"这些
最容易出错的逻辑可以脱离事件循环单测(见 ``tests/test_core.py``)。

子模块分工:

* :mod:`.errors` —— 各层共用的异常体系
* :mod:`.models` —— ``Video`` / ``Page`` / ``AudioTrack`` 等领域模型
* :mod:`.headers` —— 请求头构造与 gzip/deflate 解压(纯函数)
* :mod:`.http` —— 超时、退避、限速等调优常量(只有常量,没有实现)
* :mod:`.cache` —— 音频磁盘缓存与原子写入的 ``DownloadSink``
"""
