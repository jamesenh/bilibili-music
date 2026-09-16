"""两个网络后端的接口契约测试。

为什么需要这个文件:``QtNetworkClient`` 与 ``UrllibClient`` 是两个独立实现,
只靠 "鸭子类型" 保持一致。曾经踩过两次坑:

* ``cookie_names`` 一边是属性、一边是方法,导致诊断脚本打印出绑定方法对象,
  而且 ``bool(bound_method)`` 恒为真 —— 检查"假通过",永远发现不了问题
* 早期 ``UrllibClient.download`` 引用了不存在的 ``self._sink``,直到真正调用
  才发现

所以这里不下"能不能联网",而是钉死**两个实现的公共接口必须一致**。
"""

from __future__ import annotations

import inspect
import io
import logging
import re
import sys
import unittest
import urllib.error
from http.cookiejar import Cookie
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QObject, QUrl, Signal  # noqa: E402
from PySide6.QtNetwork import QNetworkCookie, QNetworkReply  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402  (必须先于 QtCore)

from bilibili_music.core.redact import redact_text  # noqa: E402
from bilibili_music.net.client import QtNetworkClient, _STATUS  # noqa: E402
from bilibili_music.net.urllib_client import UrllibClient  # noqa: E402

# 两个后端都必须提供的公共方法
REQUIRED_METHODS = (
    "warm_up",
    "get_json",
    "get_bytes",
    "download",
    "close",
    "session_cookies",
    "set_session_cookies",
    "clear_session_cookies",
)

# 签名必须一致的公共方法(用于把 sink / on_* 回调这类关键字参数对齐)
SIGNATURE_MATCHED = (
    "get_json",
    "get_bytes",
    "download",
    "warm_up",
    "close",
    "session_cookies",
    "set_session_cookies",
    "clear_session_cookies",
)


class TestBackendContract(unittest.TestCase):
    """静态契约:不触网,不发请求。"""

    @classmethod
    def setUpClass(cls) -> None:
        """只在类级别建一次 ``QApplication`` 与两个后端实例,避免每个用例重复初始化。"""
        cls.app = QApplication.instance() or QApplication([])
        cls.qt = QtNetworkClient()
        cls.urllib = UrllibClient()

    def test_both_expose_required_methods(self) -> None:
        """两个后端必须暴露同一组方法名,否则 --backend 切换会静默炸掉。

        ``REQUIRED_METHODS`` 是上层唯一依赖的调用面(预热、取 JSON、取字节、下载、关闭),
        只用一个后端跑通不代表切换后还能跑,所以逐个方法名在两侧都做 ``callable`` 检查。
        """
        for name in REQUIRED_METHODS:
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(self.qt, name, None)), f"Qt 缺少 {name}")
                self.assertTrue(callable(getattr(self.urllib, name, None)), f"urllib 缺少 {name}")

    def test_cookie_names_is_a_property_on_both(self) -> None:
        """必须是属性 —— 方法会让 ``bool()`` 恒为真,检查静默失效。"""
        for cls in (QtNetworkClient, UrllibClient):
            with self.subTest(backend=cls.__name__):
                self.assertIsInstance(
                    inspect.getattr_static(cls, "cookie_names"),
                    property,
                    f"{cls.__name__}.cookie_names 必须是 property",
                )

    def test_cookie_names_returns_sorted_list(self) -> None:
        """``cookie_names`` 必须返回排好序的 ``list``,保证诊断信息与日志稳定可比对。

        若返回 ``set`` 或乱序列表,同一份 Cookie 在两次运行里会打印成不同顺序,
        排查风控问题时无法直接对比差异。
        """
        for backend in (self.qt, self.urllib):
            with self.subTest(backend=type(backend).__name__):
                value = backend.cookie_names
                self.assertIsInstance(value, list)
                self.assertEqual(value, sorted(value))

    def test_method_signatures_match(self) -> None:
        """关键字参数名必须一致,否则换后端就炸。"""
        for name in SIGNATURE_MATCHED:
            with self.subTest(method=name):
                qt_sig = inspect.signature(getattr(self.qt, name))
                ur_sig = inspect.signature(getattr(self.urllib, name))
                self.assertEqual(
                    list(qt_sig.parameters),
                    list(ur_sig.parameters),
                    f"{name} 的参数名不一致: Qt={list(qt_sig.parameters)} urllib={list(ur_sig.parameters)}",
                )

    def test_download_accepts_sink(self) -> None:
        """``download`` 必须收 ``sink``:早期 urllib 版误用 self._sink,调用即崩。"""
        for backend in (self.qt, self.urllib):
            with self.subTest(backend=type(backend).__name__):
                params = inspect.signature(backend.download).parameters
                self.assertIn("sink", params)
                self.assertIn("on_success", params)
                self.assertIn("on_error", params)
                self.assertIn("on_progress", params)

    def test_get_json_uses_callback_names(self) -> None:
        """``get_json`` 的回调参数必须叫 ``on_success`` / ``on_error``,与全仓库回调命名约定一致。

        调用方普遍用关键字传参,一旦某个后端把参数改名,先跑通的实现会让另一侧在
        运行时抛 ``TypeError``,而静态检查阶段本可以发现这个问题。
        """
        for backend in (self.qt, self.urllib):
            with self.subTest(backend=type(backend).__name__):
                params = inspect.signature(backend.get_json).parameters
                self.assertIn("on_success", params)
                self.assertIn("on_error", params)

    def test_close_is_idempotent(self) -> None:
        """重复 close 不应抛异常。"""
        qt = QtNetworkClient()
        ur = UrllibClient()
        qt.close()
        qt.close()
        ur.close()
        ur.close()

    def test_session_cookies_round_trip_on_both(self) -> None:
        """会话导入导出必须往返一致,否则重启后登录态会静默丢失。

        这条不触网:只往 jar 里灌一份凭据再读出来。两个后端的域/路径口径必须完全一样,
        所以同一个断言跑两遍 —— 换 ``--backend`` 时凭据读写才不会有"一边能存一边读不出"。
        """
        cookies = {"SESSDATA": "a%2Cb%2Cc%2A11", "bili_jct": "deadbeef", "DedeUserID": "42"}
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.set_session_cookies(cookies)
                exported = backend.session_cookies()
                for name, value in cookies.items():
                    self.assertEqual(exported.get(name), value, f"{name} 没有往返成功")

    def test_session_cookies_returns_a_dict(self) -> None:
        """必须是 ``dict``(而不是 list/迭代器):调用方直接拿它落盘或注回 jar。"""
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                self.assertIsInstance(backend.session_cookies(), dict)

    def test_session_cookies_excludes_other_domains(self) -> None:
        """非 B站 域的 Cookie 不许进会话文件。

        这里刻意碰内部 jar:公开接口没法往别的域写 Cookie,而这正是要钉死的边界 ——
        一份"会话文件"里混进别的站点凭据,既无意义又多一份泄露面。
        """
        foreign = "not-bilibili"
        qt = QtNetworkClient()
        jar = qt._manager.cookieJar()  # noqa: SLF001  (契约测试必须直接摆布 jar)
        cookie = QNetworkCookie(b"other", b"x")
        cookie.setDomain(".example.com")
        cookie.setPath("/")
        jar.setCookiesFromUrl([cookie], QUrl("https://example.com/"))
        self.assertNotIn(foreign, qt.session_cookies())

        ur = UrllibClient()
        ur._cookie_jar.set_cookie(  # noqa: SLF001
            Cookie(
                version=0,
                name="other",
                value="x",
                port=None,
                port_specified=False,
                domain=".example.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
        self.assertNotIn(foreign, ur.session_cookies())

    def test_set_session_cookies_ignores_blank_entries(self) -> None:
        """空名字与空取值不许进 jar —— 它们会在 ``cookie_names`` 里造出假阳性。"""
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.set_session_cookies({"": "x", "SESSDATA": "   ", "bili_jct": "ok"})
                self.assertNotIn("", backend.cookie_names)
                self.assertEqual(backend.session_cookies(), {"bili_jct": "ok"})

    def test_clear_session_cookies_empties_the_jar(self) -> None:
        """登出必须真的把凭据从 jar 里去掉,否则"登出了还能接着用"。

        两个后端都要清干净 —— 只删文件不动 jar 是最容易漏的一步:SESSDATA 还在内存里,
        后续请求照样是登录态,用户以为已经登出,实际没有。
        """
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.set_session_cookies({"SESSDATA": "x", "bili_jct": "y"})
                self.assertTrue(backend.session_cookies())
                backend.clear_session_cookies()
                self.assertEqual(backend.session_cookies(), {})
                self.assertEqual(backend.cookie_names, [])

    def test_clear_session_cookies_is_idempotent(self) -> None:
        """重复登出(或从未登录就登出)不该抛异常。"""
        for backend in (QtNetworkClient(), UrllibClient()):
            with self.subTest(backend=type(backend).__name__):
                backend.clear_session_cookies()
                backend.clear_session_cookies()
                self.assertEqual(backend.session_cookies(), {})


#: 契约测试里用的合成请求地址。带 query 是为了同时验证地址脱敏在两边一致。
_LOG_URL = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD"

#: 一份合法的成功响应体。
_OK_BODY = b'{"code": 0, "data": {"ok": true}}'

#: 一份风控页形态的失败响应体。
_BLOCKED_BODY = b"<html><body>412 Precondition Failed</body></html>"


def _normalize(line: str) -> str:
    """抹掉两种后端**必然**不同的东西,让两边可以逐字比对。

    四处差异都有明确原因,不是"对不齐就忽略"的意思:

    * ``后端`` 字段本来就是用来区分后端的;
    * 耗时是真实测出来的;
    * ``urllib.request.Request`` 会把头名标准化成 ``Xxx-Yyy``,而 Qt 侧原样传 ——
      HTTP 头名本来就大小写不敏感,比对前统一成小写;
    * **异常消息的措辞不比对**。两个后端从不同来源拼它(urllib 用 ``HTTPError`` 的
      原文、Qt 用状态码 + 原因短语,而原因短语由服务端给),逐字相同本来就做不到。
      这是**改动日志之前就存在**的措辞差异,不属于本次要守的契约;这里保留到
      ``异常=<类型名>`` 为止,消息正文交给别的用例管。

    Args:
        line: 一条日志消息。

    Returns:
        归一化后的消息。
    """
    line = line.replace("后端=qt", "后端=X").replace("后端=urllib", "后端=X")
    line = re.sub(r"耗时=\d+ms", "耗时=Xms", line)
    line = re.sub(r"'([A-Za-z-]+)':", lambda m: f"'{m.group(1).lower()}':", line)
    return re.sub(r"异常=(\w+): .*$", r"异常=\1", line)


class _FakeHttpResponse:
    """``urllib`` 响应的最小替身:在离屏、离网环境里驱动 urllib 后端。"""

    def __init__(self, *, status: int = 200, body: bytes = b"", encoding: str = "") -> None:
        """记录状态码、响应体与内容编码。

        Args:
            status: HTTP 状态码。
            body: 响应体原文。
            encoding: ``Content-Encoding``;空串表示未压缩。
        """
        self.status = status
        self._body = body
        self._offset = 0
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def read(self, size: int = -1) -> bytes:
        """按块读数,读完后一直返回空字节串(与真实响应一致)。"""
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> _FakeHttpResponse:
        """支持 ``with`` 语法(后端就是这样用它的)。"""
        return self

    def __exit__(self, *exc: object) -> bool:
        """退出 ``with`` 时不吞异常。"""
        return False


class _FakeReply(QObject):
    """``QNetworkReply`` 的最小替身。

    只需要后端真正用到的那几个成员。信号用 ``Signal`` 声明,于是后端里的
    ``reply.finished.connect(...)`` 能正常工作,再由测试手动 ``emit()`` 驱动 ——
    不需要事件循环,更不需要网。
    """

    readyRead = Signal()
    finished = Signal()
    downloadProgress = Signal(int, int)

    def __init__(
        self,
        *,
        url: str = _LOG_URL,
        status: int = 200,
        body: bytes = b"",
        error: QNetworkReply.NetworkError | None = None,
        error_text: str = "",
    ) -> None:
        """记录这次“响应”的全部形状。

        Args:
            url: 请求地址。
            status: HTTP 状态码。
            body: 响应体原文。
            error: 网络错误;``None`` 表示 ``NoError``。

                注意不能拿 ``0`` 代替 ``NoError``:真身后端是用 ``!=`` 直接与枚举成员
                比对的,而 ``QNetworkError.NoError`` 并不是 ``int``。
            error_text: ``errorString()`` 的取值。
        """
        super().__init__()
        self._url = url
        self._status = status
        self._body = body
        self._error = error if error is not None else QNetworkReply.NetworkError.NoError
        self._error_text = error_text

    def readAll(self) -> bytes:
        """一次性交出全部响应体,并清空(第二次调用返回空)。"""
        data, self._body = self._body, b""
        return data

    def rawHeader(self, name: bytes) -> bytes:
        """这个替身不做内容编码,所以统一返回空。"""
        return b""

    def attribute(self, attr: object) -> object:
        """只支持状态码属性(后端只用这一个)。"""
        return self._status if attr == _STATUS else None

    def error(self) -> QNetworkReply.NetworkError:
        """返回构造时指定的网络错误(默认 ``NoError``)。"""
        return self._error

    def errorString(self) -> str:
        """返回构造时指定的错误文案。"""
        return self._error_text

    def url(self) -> QUrl:
        """返回请求地址。"""
        return QUrl(self._url)

    def deleteLater(self) -> None:
        """真身会交给 Qt 回收;替身什么都不用做。"""

    def abort(self) -> None:
        """取消:替身里没有在飞的东西可取消。"""

    def isFinished(self) -> bool:
        """替身永远处于“已完成”状态。"""
        return True


class _FakeManager:
    """``QNetworkAccessManager`` 的最小替身:交出一份预设好的响应。"""

    def __init__(self, reply: _FakeReply, jar: object = None) -> None:
        """绑定要交出的响应与真正的 Cookie jar。

        Args:
            reply: ``get()`` 要返回的替身响应。
            jar: Cookie jar 原身;``None`` 表示没有。
        """
        self._reply = reply
        self._jar = jar

    def get(self, request: object) -> _FakeReply:
        """忽略请求内容,直接交出预设响应。"""
        return self._reply

    def cookieJar(self) -> object:
        """返回真正的 jar,让 ``cookie_names`` 照常工作。"""
        return self._jar


class _FakeOpener:
    """``urllib`` opener 的替身:直接交出预设结果。

    刻意只替掉 **opener** 而不是整个 ``_open``:后者的重试、退避与“失败取证”正是
    被测的逻辑,替掉它等于什么都没测。
    """

    def __init__(self, result: object) -> None:
        """记录本次“请求”要得到的结果。

        Args:
            result: 要返回的响应对象,或要抛出的异常实例。
        """
        self._result = result

    def open(self, request: object, timeout: float | None = None) -> object:
        """按预设结果返回或抛出。

        Returns:
            预设的响应对象。

        Raises:
            BaseException: ``result`` 本身就是异常实例时原样抛出。
        """
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class TestRequestLogContract(unittest.TestCase):
    """两个后端写出的请求日志必须逐字一致。

    把 ``AGENTS.md`` 第 4 节"两种后端必须同步"从人工纪律变成可执行断言。两边调用的是
    同一个 ``net.base.RequestLogger``,字段由构造保证一致;这里额外钉的是**调用点**
    —— 漂移恰恰最容易发生在"某个后端忘了记,或者少传了一个字段"。

    全程不触网:两个后端的传输层都被替身接管。
    """

    @classmethod
    def setUpClass(cls) -> None:
        """只需一个 ``QApplication``(``_FakeReply`` 是 ``QObject``)。"""
        cls.app = QApplication.instance() or QApplication([])

    # ---------------------------------------------------------- 驱动

    def _run_urllib(self, *, status: int, body: bytes) -> list[logging.LogRecord]:
        """跑一遍 urllib 的后端请求路径,返回它写出的日志记录。

        Args:
            status: 替身响应要回的状态码。
            body: 替身响应要回的正文。

        Returns:
            该后端本次写出的日志记录,按时间先后排列。
        """
        client = UrllibClient(min_interval=0, max_retries=1)
        # 预热与真实连接都替换掉:本用例只验证日志,不验证网络。
        # 注意只替 opener 而不是 _open —— 后者的重试与取证逻辑正是被测对象。
        client.warm_up = lambda force=False: None  # type: ignore[method-assign]
        client.set_session_cookies({"SESSDATA": "x", "bili_jct": "y"})
        result: object = (
            _FakeHttpResponse(status=status, body=body)
            if status == 200
            else urllib.error.HTTPError(
                _LOG_URL, status, "Precondition Failed", {}, io.BytesIO(body)
            )
        )
        client._opener = _FakeOpener(result)  # type: ignore[assignment]  # noqa: SLF001
        with self.assertLogs("bilibili_music.net.urllib_client", level="DEBUG") as captured:
            client.get_json(
                _LOG_URL,
                on_success=lambda data: None,
                on_error=lambda exc: None,
            )
            client._pool.shutdown(wait=True)  # noqa: SLF001 - 等线程池把日志写完
        return list(captured.records)

    def _run_qt(self, *, status: int, body: bytes) -> list[logging.LogRecord]:
        """跑一遍 Qt 后端的请求路径,返回它写出的日志记录。

        Qt 后端是事件驱动的,但 ``_schedule`` 在限速为 0 时会**同步**发起请求,
        所以这里手动 ``emit()`` 两个信号就够了,不需要跑事件循环。

        Args:
            status: 替身响应要回的状态码。
            body: 替身响应要回的正文。

        Returns:
            该后端本次写出的日志记录,按时间先后排列。
        """
        client = QtNetworkClient(min_interval=0, max_retries=1)
        client.warm_up = lambda force=False: None  # type: ignore[method-assign]
        client.set_session_cookies({"SESSDATA": "x", "bili_jct": "y"})
        jar = client._manager.cookieJar()  # noqa: SLF001
        reply = _FakeReply(status=status, body=body)
        client._manager = _FakeManager(reply, jar)  # type: ignore[assignment]  # noqa: SLF001
        with self.assertLogs("bilibili_music.net.client", level="DEBUG") as captured:
            client.get_json(_LOG_URL, on_success=lambda data: None, on_error=lambda exc: None)
            reply.readyRead.emit()
            reply.finished.emit()
        return list(captured.records)

    @staticmethod
    def _messages(records: list[logging.LogRecord]) -> list[str]:
        """把日志记录取成纯消息文本并归一化,便于两种后端逐字比对。

        ``assertLogs`` 的 ``output`` 会带上 ``级别:logger 名:`` 前缀,直接比会比出
        假差异(两个后端的模块名本来就不同),所以这里只取消息本身。

        Args:
            records: 捕获到的日志记录。

        Returns:
            归一化后的消息文本列表。
        """
        return [_normalize(record.getMessage()) for record in records]

    # ---------------------------------------------------------- 用例

    def test_successful_request_logs_the_same_lines(self) -> None:
        """成功的请求:两边写出的行逐字一致(归一化后端名与耗时之后)。"""
        qt_lines = self._messages(self._run_qt(status=200, body=_OK_BODY))
        url_lines = self._messages(self._run_urllib(status=200, body=_OK_BODY))
        self.assertEqual(qt_lines, url_lines)
        self.assertEqual(len(qt_lines), 2, "一次成功请求应当只有“请求 + 响应”两行")
        self.assertIn("响应", qt_lines[1])
        self.assertIn("状态=200", qt_lines[1])

    def test_blocked_request_logs_the_same_lines(self) -> None:
        """被风控拦下的请求:两边写出的行逐字一致,且都带上了响应体证据。

        这条正是"排 412"最需要的场景:状态码、正文片段、请求头必须都能在日志里找到,
        而且两个后端不能一个有一个没有。
        """
        qt_lines = self._messages(self._run_qt(status=412, body=_BLOCKED_BODY))
        url_lines = self._messages(self._run_urllib(status=412, body=_BLOCKED_BODY))
        self.assertEqual(qt_lines, url_lines)
        joined = "\n".join(qt_lines)
        self.assertIn("状态=412", joined)
        self.assertIn("412 Precondition Failed", joined)
        self.assertIn("Referer", joined)
        self.assertIn("异常=NetworkError", joined)

    def test_a_failed_request_logs_exactly_one_error(self) -> None:
        """一次失败只允许有一条 ``ERROR``。

        传输层的失败证据由 ``_open`` / ``_start_json`` 就地记,上层再遇到
        ``NetworkError`` 时必须跳过 —— 否则一次 412 会刷出两条 ERROR,日志越排越乱。
        """
        for label, records in (
            ("qt", self._run_qt(status=412, body=_BLOCKED_BODY)),
            ("urllib", self._run_urllib(status=412, body=_BLOCKED_BODY)),
        ):
            with self.subTest(backend=label):
                errors = [item for item in records if item.levelno == logging.ERROR]
                self.assertEqual(len(errors), 1, f"{label} 写出了 {len(errors)} 条 ERROR")

    def test_url_query_values_never_reach_the_log(self) -> None:
        """地址里的取值不会真的落盘(参数名要留)。

        异常消息里确实带着未脱敏的完整 URL(``NetworkError`` / ``ApiError`` 都这样拼),这
        正是 ``RedactionFilter`` 存在的理由 —— handler 会在**写盘前**把它洗掉。这里
        手动过一遍 ``redact_text``,模拟的就是那一道关。
        """
        joined = "\n".join(self._messages(self._run_qt(status=200, body=_OK_BODY)))
        written = redact_text(joined)
        self.assertNotIn("BV1xx411c7mD", written)
        self.assertIn("bvid=<redacted>", written)

    def test_cookie_values_never_reach_the_log(self) -> None:
        """异常与请求头里都不许出现 Cookie 取值。"""
        joined = "\n".join(self._messages(self._run_qt(status=412, body=_BLOCKED_BODY)))
        self.assertNotIn("SESSDATA=x", joined)
        self.assertNotIn("bili_jct=y", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)