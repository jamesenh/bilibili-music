"""脚本共用的辅助工具。

脚本是线性的、以阅读为主,所以这里提供一个"把异步回调写成同步等待"的小工具。
注意:**生产代码里不要这样写** —— 嵌套事件循环会阻塞调用线程。它只适合
一次性验证脚本,让流程能从头读到尾。

脚本目录会**手动把 ``src`` 塞进 ``sys.path``**,这是 ``AGENTS.md`` 第 3 节
明确允许的例外(与 ``tests/`` 同级待遇):脚本要能在``uv run`` 下直接执行,
而正式包代码一律用相对导入。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 注意导入顺序:必须先 import QtWidgets/QtGui 再 import QtCore。
# PySide6 会把 `from PySide6.QtCore import X` 记作"第一个 Qt 模块",并据此决定
# 是否加载平台插件;先导入 QtCore 会让 QApplication 拿不到 windows 平台插件,
# 报 "QSocketNotifier: Socket notifiers cannot be enabled from another thread"。
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402

#: ``wait_for`` / ``wait_until`` 默认超时(秒)。触网步骤的实际调用都传了更大的值,
#: 这两个默认值只兜住"忘了传参"的情况。
DEFAULT_TIMEOUT = 30.0

#: ``wait_until`` 轮询时每次 ``processEvents`` 之间的休眠(秒)。
#:
#: 用 ``processEvents`` + ``sleep`` 而不是嵌套 ``QEventLoop``:前者能观察
#: "回调是否已经改动了某个标志",后者只能等到某个明确的事件。20ms 是
#: "不空转烧 CPU"与"发现状态变化够快"的折中。
_POLL_INTERVAL = 0.02


def ensure_app() -> QCoreApplication:
    """确保存在一个 QApplication(Qt 网络必须有事件循环)。

    用 ``QApplication`` 而不是 ``QCoreApplication``:后者缺少平台插件初始化,
    在有 GUI 的场景里会导致套接字通知器无法启用。

    Returns:
        当前进程的 ``QApplication``;若尚未创建则就地创建一个。
    """
    return QApplication.instance() or QApplication(sys.argv)


def wait_for(
    start: Callable[[Callable[[Any], None], Callable[[Exception], None]], Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    what: str = "操作",
) -> Any:
    """发起一个异步操作并阻塞等待结果。

    Args:
        start: 接收 ``(on_success, on_error)`` 的函数。
        timeout: 超时秒数,超时抛 :class:`TimeoutError`。
        what: 操作名,只用于拼超时错误消息。

    Returns:
        ``on_success`` 收到的值;若 ``on_error`` 被调用则抛出收到的异常。

    Raises:
        TimeoutError: 超过 ``timeout`` 秒仍未收到任何回调。
        其他异常:``on_error`` 收到的那个异常原样重抛。
    """
    ensure_app()
    loop = QEventLoop()
    box: dict[str, Any] = {}

    def ok(value: Any) -> None:
        """成功回调:暂存结果并结束等待。"""
        box["value"] = value
        loop.quit()

    def err(exc: Exception) -> None:
        """失败回调:暂存异常并结束等待。"""
        box["error"] = exc
        loop.quit()

    # 定时器必须在 start() 之前启动:回调可能是同步的(缓存命中),那时
    # loop.quit() 发生在 loop.exec() 之前,是空操作。若不定时器兜底,后面的
    # loop.exec() 会进入一个永远没人唤醒的事件循环,只能干等到超时。
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(int(timeout * 1000))

    start(ok, err)
    if "value" not in box and "error" not in box:
        loop.exec()
    timer.stop()

    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise TimeoutError(f"{what} 超时({timeout}s)")
    return box["value"]


def wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    """转动事件循环直到 ``predicate`` 为真。返回是否在超时前满足。

    适合"回调只改标志、不返回值"的场景(例如等播放位置推进)。

    Args:
        predicate: 无参断言,每轮事件循环后求值一次。
        timeout: 最长等待秒数。

    Returns:
        ``predicate`` 是否在超时前变为真;超时返回最后一次求值结果(通常为 ``False``)。
    """
    app = ensure_app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(_POLL_INTERVAL)
    return predicate()


def make_backend(kind: str):
    """按名字造一个网络后端。``kind`` 取 ``qt`` 或 ``urllib``。

    Args:
        kind: 后端名。

    Returns:
        ``QtNetworkClient`` 或 ``UrllibClient`` 实例(接口一致,可互换)。

    Note:
        除 ``"urllib"`` 以外的一切取值都返回 Qt 后端(含拼错的名字)——
        调用方都是 ``argparse`` 的 ``choices``,非法值到不了这里;
        这里不做额外校验是为了让两个脚本的调用点保持无分支。
    """
    if kind == "urllib":
        from bilibili_music.net.urllib_client import UrllibClient

        return UrllibClient()
    from bilibili_music.net.client import QtNetworkClient

    return QtNetworkClient()
