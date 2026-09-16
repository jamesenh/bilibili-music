"""日志与错误文案里的敏感信息脱敏(纯函数)。

这个模块是整个日志方案的红线所在,它只做"字符串进、字符串出",没有 IO、没有
``logging`` 之外的依赖,因此可以脱离文件系统逐条单测。

三条设计取舍:

* **URL 一律只留 ``host + path + query 参数名``,取值全部抹掉**,不判断域名。
  按域名黑名单(``bilivideo.com`` 之类)识别音视频直链看起来更"准",但 B站 的 CDN
  域名会变,漏一个就会让带签名的完整直链落盘,而且**漏了是看不出来的**。反过来,
  "参数值一律不记"不依赖任何外部清单,"漏"这件事在结构上不可能发生。代价是网络层
  日志看不到 query 取值 —— 排障需要的接口入参(``keyword`` / ``bvid`` / ``cid``)
  由 ``api`` 层以**显式变量**的形式记录,那些调用点本来就有这些变量,不必从 URL 反解。

* **脱敏在 handler 层强制生效**(:class:`RedactionFilter`),不依赖调用方自律。
  调用点一定会漏:项目里有几十处 ``except ... as exc`` 会把异常原文拼进日志,而
  :class:`~bilibili_music.core.errors.ApiError` 的消息里就嵌着完整 URL。有了这一层,
  单个模块忘记脱敏的最坏后果只是多一条 URL,而不是泄一份 ``SESSDATA``。

* **凭据字段清单是常量** :data:`CREDENTIAL_FIELD_NAMES`,单测直接遍历它生成用例。
  往清单里加字段却忘了补测试,这件事因此不可能发生。

字段取值本身禁止出现在任何地方(见 ``AGENTS.md`` 第 5 节第 10 条):诊断只允许报告
**名字、数量、长度或哈希前缀**。
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

__all__ = [
    "CREDENTIAL_FIELD_NAMES",
    "CREDENTIAL_HEADER_NAMES",
    "REDACTED",
    "RedactionFilter",
    "install_redaction",
    "redact_headers",
    "redact_text",
    "redact_url",
]

#: 取值被整体抹掉的占位串。
REDACTED = "<redacted>"

#: 一律只保留"名字与条目数"的请求头(小写比较)。
#:
#: ``Referer`` **故意不在其中**:它不是凭据,而且"请求有没有带 Referer"正是音频 CDN
#: 返回 403 时的第一排查线索(见 ``README.md`` 的接口实测笔记),抹掉等于自断证据。
CREDENTIAL_HEADER_NAMES: frozenset[str] = frozenset(
    {"authorization", "cookie", "set-cookie"}
)

#: 一出现就必须抹掉取值的名字(小写)。
#:
#: 这些名字等价的账号密码:``SESSDATA`` 是鉴权凭据,``bili_jct`` / ``csrf*`` 能发起写
#: 操作,``refresh_token`` 能换新的 ``SESSDATA``。``buvid3`` / ``buvid4`` 严格说不是
#: 账号凭据,而是**设备指纹**,但它同样是服务端用来认人的标识,没有理由落盘。
#:
#: 清单被 ``tests/test_redact.py`` 逐项遍历验证 —— 新增名字会自动获得覆盖。
CREDENTIAL_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "access_token",
        "bili_jct",
        "buvid3",
        "buvid4",
        "csrf",
        "csrf_token",
        "dedeuserid",
        "refresh_csrf",
        "refresh_token",
        "sessdata",
    }
)

#: 匹配文本里出现的 URL。故意不含引号、空白与反斜杠:它们通常是包围 URL 的标点,
#: 不是 URL 的一部分。
#:
#: **不能把 ``<`` / ``>`` 也排除掉**:已脱敏的地址里就带着 ``<redacted>``,
#: 排除它们会把匹配截在 ``=`` 处,于是 ``<redacted>`` 被留在原地、后续再被追加一个
#: 占位串(实测抱到过:日志里出现过 ``=<redacted><redacted>``)。
_URL_RE = re.compile(r"https?://[^\s\"'\\]+")

#: 凭据字段的两种常见形态。JSON 形态必须先处理 —— ``"SESSDATA": "x"`` 里的冒号前面
#: 隔着引号,``_KV_CRED_RE`` 匹配不到它。
_CREDENTIAL_ALT = "|".join(sorted(re.escape(name) for name in CREDENTIAL_FIELD_NAMES))
_JSON_CRED_RE = re.compile(rf'(?i)"({_CREDENTIAL_ALT})"\s*:\s*"[^"]*"')
_KV_CRED_RE = re.compile(rf"(?i)\b({_CREDENTIAL_ALT})\b(\s*[=:]\s*)([^\s,;&\"']+)")


# ============================== URL 与请求头


def _redacted_query(raw: str) -> str:
    """把一段 query 串收敛成 ``名字1,名字2=<redacted>``。

    输出**是幂等的**:再喂给本函数一次仍是同一个结果(``parse_qsl`` 会把首个逗号后的
    全部内容当成一个名字,重新拼出来正好还原)。这一点很重要 —— 调用点已经脱敏过的
    URL 还会被 :class:`RedactionFilter` 再过一遍,不幂等就会被二次加工成乱码。

    Args:
        raw: ``?`` 之后、``#`` 之前的那段原文,不带 ``?``。

    Returns:
        参数名升序拼接 + 取值占位串;拆不出任何参数名时返回 :data:`REDACTED`
        (畸形串宁可不输出,也不冒险原样带出去)。
    """
    if not raw:
        return ""
    names = sorted({name for name, _ in parse_qsl(raw, keep_blank_values=True)})
    if not names:
        return REDACTED
    return f"{','.join(names)}={REDACTED}"


def redact_url(url: str) -> str:
    """把 URL 里的 query / fragment 取值抹掉,只留下参数名。

    用法与效果::

        >>> redact_url("https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/1/2/x.m4s?deadline=17&upsig=ab")
        'https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/1/2/x.m4s?deadline,upsig=<redacted>'

    保留路径与参数名是为了让日志仍能回答"打到了哪个 CDN、路径结构对不对、带了哪些
    参数";丢掉取值是因为音视频直链的取值就是一组会过期的签名,记下来除了多一份泄露面
    没有别的用处。

    Args:
        url: 原始地址;空串原样返回(日志里常见"没有 URL"这种情况)。

    Returns:
        抹掉取值后的地址;畸形到无法拆解时返回 :data:`REDACTED`。
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        # 畸形串(例如带非法 IPv6 字面量)会让 urlsplit 抛错。这里不能让它冒泡 ——
        # 它被调用在日志路径上,而"日志把业务炸掉"是最不能接受的失败方式
        return REDACTED
    if not parts.query and not parts.fragment:
        return url
    fragment = parts.fragment
    # fragment 只有长得像 query 时才处理:``#comments`` 这种纯锚点没有取值可抹
    if fragment and ("=" in fragment or "&" in fragment):
        fragment = _redacted_query(fragment)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, _redacted_query(parts.query), fragment)
    )


def _cookie_entry_count(value: str) -> int:
    """数一份 ``Cookie`` 头里有几个条目。

    按 ``;`` 拆分而不是用 ``http.cookies.SimpleCookie``:后者要解析取值,而这里的
    取值恰恰是碰都不该碰的东西(``SESSDATA`` 里含 ``=``,还有 percent-encoded 的逗号)。

    Args:
        value: ``Cookie`` 头原文。

    Returns:
        含 ``=`` 的条目数。
    """
    return sum(1 for part in value.split(";") if "=" in part)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """抹掉请求头里的凭据取值,只保留名字(以及部分头的条目数)。

    ``Cookie`` / ``Set-Cookie`` 保留"有几项"这一信息,与
    ``HttpBackend.cookie_names`` 的口径一致:知道"带了 3 个 cookie"对排查
    "登录态没生效"已经够用,而具体取值一个字都不该出现。

    Args:
        headers: 原始请求头。

    Returns:
        新的字典;非凭据头原样保留,凭据头的取值被替换成占位串。
    """
    cleaned: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered not in CREDENTIAL_HEADER_NAMES:
            cleaned[name] = value
        elif lowered == "authorization":
            cleaned[name] = REDACTED
        else:
            cleaned[name] = f"{REDACTED}({_cookie_entry_count(value)} 项)"
    return cleaned


# ============================== 任意文本


def redact_text(text: str) -> str:
    """把一段任意文本里的 URL 取值与凭据字段取值抹掉。

    这是兜底入口,:class:`RedactionFilter` 对它每个日志消息都会调一次。处理顺序是
    "先 URL、后字段":URL 的 query 取值里可能就含着凭据,先整串收敛掉更省事,也避免
    字段正则去啃长 URL。

    Args:
        text: 任意文本(日志消息、异常消息、响应体片段)。

    Returns:
        抹掉敏感取值后的文本;``text`` 为空时原样返回。
    """
    if not text:
        return text
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = _JSON_CRED_RE.sub(rf'"\1": "{REDACTED}"', text)
    return _KV_CRED_RE.sub(rf"\1\2{REDACTED}", text)


# ============================== handler 层兜底


class RedactionFilter(logging.Filter):
    """把每条日志的消息与异常栈统一过一遍 :func:`redact_text`。

    做法是**把消息预渲染并就地改写** ``record``:``msg`` 换成脱敏后的成品、``args``
    置空,这样后续 handler 再格式化时不会把原始参数重新拼回来。

    失败一律**封闭**(drop):脱敏本身出错时,宁可把这条消息换成一句提示,也不能把
    未经处理的内容放出去。同时返回 ``True`` 让这条"提示"照常落盘 —— 静默丢日志会让人
    误以为那条路径没执行过。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """脱敏 ``record`` 的消息与异常栈。

        Args:
            record: 待落盘的日志记录。

        Returns:
            恒为 ``True``:脱敏失败时改写为提示文本后仍放行。
        """
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败绝不淡回调用点(见下方注释)
            # 走到了这里说明 ``msg % args`` 本身就炸了(占位符数量对不上之类),
            # 内容不可信,只能整条替换掉
            record.msg = "<日志格式化失败,本条内容已丢弃>"
            record.args = None
            return True
        record.msg = redact_text(message)
        record.args = None
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        if record.exc_info and not record.exc_text:
            # ``Formatter`` 只在 ``exc_text`` 为空时才自己渲染异常栈,所以这里主动
            # 渲染成脱敏后的文本并清掉 ``exc_info``,后续就不会再走到原始栈上
            try:
                rendered = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redact_text(rendered)
            except Exception:  # noqa: BLE001 - 渲染失败也“封闭”,绝不回退到原始异常栈
                record.exc_text = "<异常栈格式化失败,已丢弃>"
        if record.exc_info:
            record.exc_info = None
        return True


def install_redaction(handler: logging.Handler) -> None:
    """给 handler 挂上 :class:`RedactionFilter`。

    幂等:已经挂过就不再重复挂,否则同一个 handler 被复用(测试里常见)会把每条
    日志多洗几遍。

    Args:
        handler: 目标 handler。
    """
    if any(isinstance(existing, RedactionFilter) for existing in handler.filters):
        return
    handler.addFilter(RedactionFilter())
