"""B站账号链路只读验证脚本(路线图 M5 S0)。

**它为什么存在**:M5 要做的"登录 + 收藏夹当歌单"有一堆只能在**真账号**下才问得清的问题
(收藏夹接口到底要不要 WBI 签名、条目里有没有分P `cid`、登录能不能换到更高音频档位、
登录态下的风控节奏)。这些问题的答案决定后面要不要写 `core/wbi.py`、要不要做那套脆弱的
Cookie 保鲜链路,甚至决定整个里程碑值不值得做 —— 所以**先验证,再写功能代码**。

**它做什么**:用一份粘贴进来的 Cookie 把上面那些问题逐个问一遍,最后打印一段可直接贴回去
的「结论清单」。

**它不做什么**(这是刻意的):

* 不写任何文件(不落盘凭据、不写缓存、不写日志);
* 不修改应用状态、不写入 B站(全程只有 GET,没有任何收藏 / 取消收藏动作);
* 不打印凭据取值 —— 诊断只报 **名字、数量与长度**,见 ``AGENTS.md`` 第 5 节第 10 条;
* 不 import Qt,也不需要 ``QApplication``:它验的是**接口事实**,不是应用的连线
  (S1 才给 ``net/`` 加会话注入契约)。

**怎么用**::

    # 推荐:从环境变量读(不进 shell 历史、不进进程列表)
    set BILI_COOKIE=SESSDATA=xxx; bili_jct=yyy; DedeUserID=123

    .venv\\Scripts\\python.exe scripts\\probe_login.py

    # 或者交互式粘贴(不回显):
    .venv\\Scripts\\python.exe scripts\\probe_login.py

    # 不带凭据也能跑:只出匿名基线,用来对照"登录带来了什么"
    .venv\\Scripts\\python.exe scripts\\probe_login.py --no-credentials

**Cookie 从哪来**:浏览器登录 B站后,开发者工具 → Network → 任意 `bilibili.com` 请求 →
请求头里的 `Cookie:` 一整行(至少要含 `SESSDATA`,建议连 `bili_jct` / `DedeUserID` 一起)。
**建议先用小号或可弃账号**:这条链路打的是非官方接口,风控代价落在账号上。

**WBI 签名那段为什么在这儿**:文档口径说收藏夹接口不需要签名,但 ``-352`` 的定义把
"UA 或 wbi 参数不合法"并列,而社区文档仓库已被关停、拿不到 issue 佐证。所以脚本对每个
收藏夹接口都做**两段式**复验(先不带签名,拿不到数据再带签名),这样"拿不到内容"这个现象
才能被归因到"缺签名"还是"没权限"。S0 出结论后:确认需要签名 → 按 M5 计划迁到
``core/wbi.py``(纯函数 + 固定样本单测);确认不需要 → 这段随脚本一起删。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

# scripts/ 下的独立脚本按仓库惯例自行引导 src 路径(AGENTS.md 第 3 节:仅 tests/ 与 scripts/ 例外)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.headers import (  # noqa: E402
    API_BASE,
    HOME,
    api_headers,
    decompress_all,
)
from bilibili_music.core.http import (  # noqa: E402
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_INTERVAL,
    RETRYABLE_STATUS,
)
from bilibili_music.core.session import parse_cookie_header  # noqa: E402
from bilibili_music.net.base import backoff_delay  # noqa: E402

__all__ = [
    "DEFAULT_BVID",
    "MIXIN_KEY_ENC_TAB",
    "ProbeStats",
    "Prober",
    "audio_summary",
    "main",
    "mixin_key",
    "signed_query",
]

#: 默认探测用的 BV 号。取 ``probe_api.py`` 的同一条,便于两份脚本的结论互相对照。
#:
#: 注意(2026-09-14 实测):它现在**只有 1 个分P**(``pages`` 长度 1),所以拿它做音质对比
#: 是合适的,但"多P语义"必须靠收藏夹里的真实条目来验(第 5 节只在 ``page>1`` 时才跑)。
DEFAULT_BVID = "BV1GJ411x7h7"

#: 收藏夹内容接口一页最多取多少条。文档口径 ``ps`` 定义域是 1~20,
#: 取满是为了顺带验证"返回条数是否真的等于 ps"(可能被服务端截断)。
MAX_PAGE_SIZE = 20

#: WBI 的 64 元素重排表(社区逆向出来的常量)。
#:
#: **只用于 S0 验证**,理由见模块 docstring。表本身是纯数据,不含任何凭据。
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

#: 判定"这次请求撞上风控了"的业务码。写在这里而不是散在判断里:
#: 结论清单要按这几个码统计风控节奏,口径只有一处。
RISK_CODES = frozenset({-352, -412, -799, -509})


# ====================================================================== 纯函数


def mask_cookies(cookies: dict[str, str]) -> str:
    """把 Cookie 概况压成一行**不含取值**的诊断文本。

    只报名字与取值长度:长度足以判断"是不是粘贴残缺了",而取值本身不出现在任何输出里。

    Args:
        cookies: 名字到取值的映射。

    Returns:
        形如 ``SESSDATA(280) bili_jct(32)`` 的文本;空字典时返回 ``(无)``。
    """
    if not cookies:
        return "(无)"
    return " ".join(f"{name}({len(value)})" for name, value in sorted(cookies.items()))


def mixin_key(img_url: str, sub_url: str) -> str:
    """按重排表从 ``wbi_img`` 的两个地址推出 ``mixin_key``。

    Args:
        img_url: ``nav`` 响应里 ``data.wbi_img.img_url`` 的完整地址。
        sub_url: 同上,``sub_url``。

    Returns:
        32 位 ``mixin_key``;两个地址任一为空时返回空串(调用方据此跳过带签名的复验)。
    """
    if not img_url or not sub_url:
        return ""
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def signed_query(params: dict[str, Any], key: str) -> str:
    """给参数补上 ``wts`` 与 ``w_rid``,返回可直接拼进 URL 的 query 串。

    Args:
        params: 业务参数(不含 ``wts`` / ``w_rid``)。
        key: :func:`mixin_key` 算出的 ``mixin_key``;为空时原样返回未签名 query。

    Returns:
        已排序并带签名的 query 串;``key`` 为空时是**未签名**的 query 串,
        这样调用方不必分两条分支。
    """
    params = dict(params)
    params["wts"] = int(time.time())
    # 文档要求:参与签名的取值要先过滤掉 !'()* 这几个字符(不过滤算出的 w_rid 会对不上)
    filtered = [
        (name, "".join(ch for ch in str(value) if ch not in "!'()*"))
        for name, value in sorted(params.items())
    ]
    base = urllib.parse.urlencode(filtered)
    if key:
        params["w_rid"] = hashlib.md5((base + key).encode("utf-8")).hexdigest()
    return urllib.parse.urlencode(sorted(params.items()))


def audio_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """把 ``playurl`` 的 ``data`` 压成"有哪些音频档位"的一句话摘要。

    用于登录前 / 登录后的**同视频对比** —— 这是回答"登录到底换来了什么"的唯一依据,
    所以既要给出档位 id 集合,也要单独标出会员向的 ``flac`` / ``dolby`` 是否为空。

    Args:
        payload: ``playurl`` 响应里的 ``data`` 对象。

    Returns:
        ``{"qualities": [...], "codecs": {...}, "flac": bool, "dolby": bool}``;
        拿不到 ``dash`` 时各项为空。
    """
    dash = payload.get("dash") or {}
    qualities: list[int] = []
    codecs: dict[str, str] = {}
    for item in dash.get("audio") or []:
        if not isinstance(item, dict):
            continue
        quality_id = int(item.get("id") or 0)
        qualities.append(quality_id)
        codecs[str(quality_id)] = str(item.get("codecs") or "")
    flac = (dash.get("flac") or {}).get("audio")
    if isinstance(flac, dict):
        quality_id = int(flac.get("id") or 30251)
        qualities.append(quality_id)
        codecs[str(quality_id)] = str(flac.get("codecs") or "fLaC")
    dolby_items = (dash.get("dolby") or {}).get("audio") or []
    for item in dolby_items:
        if isinstance(item, dict):
            quality_id = int(item.get("id") or 30250)
            qualities.append(quality_id)
            codecs[str(quality_id)] = str(item.get("codecs") or "")
    return {
        "qualities": sorted(set(qualities)),
        "codecs": codecs,
        "flac": isinstance(flac, dict),
        "dolby": bool(dolby_items),
    }


def describe_audio(summary: dict[str, Any]) -> str:
    """把 :func:`audio_summary` 的结果拼成一行可读文本。

    Args:
        summary: :func:`audio_summary` 的返回值。

    Returns:
        形如 ``档位=[30216, 30280] flac=否 dolby=否`` 的文本。
    """
    qualities = ",".join(str(q) for q in summary.get("qualities") or []) or "无"
    return (
        f"档位=[{qualities}] flac={'是' if summary.get('flac') else '否'} "
        f"dolby={'是' if summary.get('dolby') else '否'}"
    )


# ====================================================================== 请求器


@dataclass(slots=True)
class ProbeStats:
    """本次探测的请求统计。

    存在的意义是回答 S0 的第 6 问(登录态下的风控节奏):光看"成功/失败"不够,
    要知道一共发了多少次、业务码分布如何、其中多少次是风控码。
    """

    requests: int = 0
    """发出的 HTTP 请求总数(含重试)。"""

    codes: dict[str, int] = field(default_factory=dict)
    """业务 ``code``(或 ``HTTP:<状态码>``)到出现次数的映射。"""

    risk_hits: int = 0
    """命中 :data:`RISK_CODES` 的次数。"""

    def note(self, key: str) -> None:
        """登记一次响应。

        Args:
            key: 业务码的字符串形式,或 ``HTTP:<状态码>``。
        """
        self.codes[key] = self.codes.get(key, 0) + 1
        if key.startswith("-") and int(key) in RISK_CODES:
            self.risk_hits += 1

    def summary(self) -> str:
        """拼一行统计摘要。"""
        codes = " ".join(f"{k}×{v}" for k, v in sorted(self.codes.items())) or "无"
        return f"共 {self.requests} 次请求;业务码: {codes};风控码 {self.risk_hits} 次"


class Prober:
    """一个带独立 Cookie jar 的只读请求器。

    **为什么不用项目里的 ``net`` 后端**:后端契约里还没有"注入会话 Cookie"的能力
    (那是 S1 的活),而 S0 要问的是接口事实。所以这里直接用标准库 ``urllib``,但请求头与
    限速参数一律复用 ``core/headers.py`` / ``core/http.py`` / ``net/base.py::backoff_delay``
    —— 否则"脚本能过、应用被拒"就同时存在两个变量,结论没法用。

    每个实例一个 jar:匿名臂与登录臂**必须分开**,否则登录臂的 Cookie 会污染匿名基线,
    "登录带来了什么"这个问题就再也答不了。

    Args:
        label: 出现在日志里的臂名(如 ``匿名`` / ``登录``)。
        cookies: 要注入的 Cookie;``None`` 表示匿名。
        min_interval: 请求间最小间隔(秒),默认取应用的 ``DEFAULT_MIN_INTERVAL``。
        max_retries: 含首次在内的最大尝试次数。
        verbose: 是否打印原始响应体(可能含个人信息,默认关闭)。
    """

    def __init__(
        self,
        label: str,
        *,
        cookies: dict[str, str] | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        verbose: bool = False,
    ) -> None:
        self.label = label
        self.verbose = verbose
        self.stats = ProbeStats()
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_request = 0.0
        self._warmed_up = False
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._headers = api_headers()
        if cookies:
            self._inject(cookies)

    # ------------------------------------------------------------ 会话

    def _inject(self, cookies: dict[str, str]) -> None:
        """把粘贴进来的 Cookie 灌进 jar,域固定为 ``.bilibili.com``。

        为什么写死域与 ``secure``:浏览器里这些字段本来就是给整个 ``.bilibili.com``
        下发的,而 ``SESSDATA`` 是 HttpOnly + Secure。凭据只活在内存里,进程退出即消失。

        Args:
            cookies: 名字到取值的映射。
        """
        for name, value in cookies.items():
            self._jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=".bilibili.com",
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

    @property
    def cookie_names(self) -> list[str]:
        """当前 jar 里的 Cookie 名(诊断用,不含取值)。"""
        return sorted({c.name for c in self._jar})

    def warm_up(self, *, force: bool = False) -> bool:
        """访问主页拿 ``buvid3`` / ``b_nut``。

        与应用的 ``warm_up()`` 同一个理由(见 README「坑 3」):B站 API 响应本身不下发
        这两个 Cookie,只有主页会,而缺了它们更容易被风控。脚本没有事件循环,所以这里
        可以同步等待 —— 但**不要**把这个写法搬回 Qt 路径(AGENTS.md 第 3 节)。

        Args:
            force: 为真时即使预热过也重新来一次(重试风控前就该这么做)。

        Returns:
            预热请求是否成功(失败不算致命,后续请求仍会尝试)。
        """
        if self._warmed_up and not force:
            return True
        try:
            request = urllib.request.Request(
                HOME, headers={"User-Agent": self._headers["User-Agent"]}
            )
            with self._opener.open(request, timeout=20) as response:
                response.read(4096)
            self._warmed_up = True
            return True
        except Exception:  # noqa: BLE001
            # 预热失败不致命:正式请求可能仍然成功,让它自己去报错
            return False

    # ------------------------------------------------------------ 请求

    def _wait(self) -> None:
        """按最小间隔阻塞等待(脚本没有事件循环,这里 sleep 是安全的)。"""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        delay = self._min_interval - elapsed
        if delay > 0:
            time.sleep(delay)

    def get_json(self, url: str, *, note: str) -> dict[str, Any] | None:
        """GET 一个 JSON 接口并返回**完整响应体**(不做 ``code`` 校验)。

        刻意不校验 ``code``:S0 要看的恰恰是非 0 的码(``-101`` / ``-352`` / ``-403``),
        而 ``check_payload`` 会把它们转成异常,那样就看不到原始码了。

        Args:
            url: 完整请求地址。
            note: 出错时用于提示的短名(接口名,不含凭据)。

        Returns:
            解析后的响应对象;网络失败或响应不是 JSON 时返回 ``None``。
        """
        last_error = ""
        for attempt in range(self._max_retries):
            self._wait()
            self.stats.requests += 1
            try:
                with self._opener.open(
                    urllib.request.Request(url, headers=self._headers), timeout=20
                ) as response:
                    self._last_request = time.monotonic()
                    payload = decompress_all(
                        response.read(), response.headers.get("Content-Encoding", "")
                    )
                data = json.loads(payload.decode("utf-8"))
                if not isinstance(data, dict):
                    print(f"  [{note}] 响应不是 JSON 对象")
                    return None
                self.stats.note(str(data.get("code")))
                if self.verbose:
                    print(f"  [{note}] 原始响应: {json.dumps(data, ensure_ascii=False)[:2000]}")
                return data
            except urllib.error.HTTPError as exc:
                self._last_request = time.monotonic()
                self.stats.note(f"HTTP:{exc.code}")
                last_error = f"HTTP {exc.code}"
                if exc.code not in RETRYABLE_STATUS and exc.code < 500:
                    break
                self.warm_up(force=True)
            except Exception as exc:  # noqa: BLE001
                self._last_request = time.monotonic()
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self._max_retries - 1:
                time.sleep(backoff_delay(attempt))
        print(f"  [{note}] 请求失败: {last_error}")
        return None


# ====================================================================== 输出


def report(label: str, ok: bool | None, detail: str = "") -> None:
    """打印一条带标记的检查结果。

    ``ok`` 允许是 ``None``,表示"这一项本次无法判定"(例如没给凭据) —— 与 ``False``
    必须区分开,否则结论清单会把"没验"读成"验出问题"。

    Args:
        label: 检查名。
        ok: ``True`` 通过 / ``False`` 失败 / ``None`` 未能判定。
        detail: 补充信息。
    """
    mark = {True: "OK  ", False: "FAIL", None: "SKIP"}[ok]
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    """打印一个小节标题。

    Args:
        title: 小节名。
    """
    print(f"\n=== {title} ===")


# ====================================================================== 各项检查


def read_credentials(use_credentials: bool) -> dict[str, str]:
    """从环境变量 / stdin / 交互式提示里取一份 Cookie。

    优先级是刻意的:环境变量最安全(不进 shell 历史、不进进程列表),其次是从管道读
    (``echo ... | python ...``),最后才是交互式粘贴(``getpass`` 不回显)。
    **不接受命令行参数**传凭据 —— 那会把它写进 shell 历史与进程列表。

    Args:
        use_credentials: 为假时直接返回空字典(对应 ``--no-credentials``)。

    Returns:
        名字到取值的映射;没取到或用户拒绝时返回空字典。
    """
    if not use_credentials:
        return {}
    raw = os.environ.get("BILI_COOKIE", "").strip()
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.readline().strip()
    if not raw:
        try:
            raw = getpass.getpass("粘贴 Cookie 头后回车(不回显,留空则只跑匿名基线): ").strip()
        except Exception:  # noqa: BLE001
            # 某些终端下 getpass 不可用(没有控制台、被重定向),此时退回空凭据而不是崩掉
            raw = ""
    return parse_cookie_header(raw)


def _warm_report(prober: Prober, label: str) -> bool:
    """预热一次,并按"是否真的拿到 buvid 系 Cookie"给出结论。

    只看"HTTP 请求成功"是不够的:2026-09-14 实测**主页偶尔不下发** ``buvid3``
    (同一轮里先跑的臂没拿到、后跑的臂拿到了),而缺 buvid 正是风控的常见诱因 ——
    所以这里把"请求成功"与"拿到 Cookie"分开判断,免得把一个假通过读成健康。

    Args:
        prober: 要预热的臂。
        label: 打印用的检查名。

    Returns:
        是否拿到了 ``buvid`` 开头的 Cookie。
    """
    ok = prober.warm_up(force=True)
    names = prober.cookie_names
    got = any(name.startswith("buvid") for name in names)
    detail = f"cookies={names}" + ("" if got else " ← 主页没下发 buvid 系 Cookie")
    report(label, bool(ok and got), detail)
    return got


def check_session(anon: Prober, auth: Prober | None) -> dict[str, Any]:
    """检查预热与登录态,并返回 ``nav`` 的关键字段。

    登录态一律以 ``nav`` 的 ``isLogin`` 为准,**不看收藏夹接口的 code** ——
    2026-09-14 实测:匿名请求收藏夹接口返回 ``code=0`` 且 ``data=null``,它不会报错。

    Args:
        anon: 匿名臂。
        auth: 登录臂;``None`` 表示本次没有凭据。

    Returns:
        含 ``is_login`` / ``mid`` / ``uname`` / ``vip`` / ``wbi_key`` 的字典。
    """
    section("1. 会话预热与登录态")
    _warm_report(anon, "匿名臂预热主页")
    if auth is not None:
        _warm_report(auth, "登录臂预热主页")

    prober = auth if auth is not None else anon
    nav = prober.get_json(f"{API_BASE}/x/web-interface/nav", note="nav")
    if not nav:
        return {"is_login": False, "mid": 0, "wbi_key": ""}
    data = nav.get("data") or {}
    is_login = bool(data.get("isLogin"))
    vip = data.get("vipStatus") or data.get("vipType") or 0
    report(
        f"{prober.label}臂 nav",
        is_login if auth is not None else None,
        f"code={nav.get('code')} isLogin={is_login} mid={data.get('mid')} "
        f"uname={(data.get('uname') or '')[:12]!r} vipType={vip}",
    )
    wbi = data.get("wbi_img") or {}
    key = mixin_key(wbi.get("img_url", ""), wbi.get("sub_url", ""))
    report("wbi_img 可用(签名前提)", bool(key), f"mixin_key 长度={len(key)}")
    return {
        "is_login": is_login,
        "mid": int(data.get("mid") or 0),
        "uname": data.get("uname") or "",
        "vip": vip,
        "wbi_key": key,
    }


def check_cookie_info(auth: Prober | None, cookies: dict[str, str]) -> dict[str, Any]:
    """检查 Cookie 保鲜接口,回答"登录态要不要刷新、什么时候刷"。

    Args:
        auth: 登录臂;``None`` 时跳过。
        cookies: 粘贴进来的 Cookie(``csrf`` 要取 ``bili_jct`` 的值)。

    Returns:
        含 ``refresh`` / ``timestamp`` 的字典;未判定时 ``refresh`` 为 ``None``。
    """
    section("2. 登录态保鲜(cookie/info)")
    if auth is None:
        report("cookie/info", None, "没有凭据,跳过")
        return {"refresh": None}
    csrf = cookies.get("bili_jct", "")
    url = (
        "https://passport.bilibili.com/x/passport-login/web/cookie/info"
        f"?csrf={urllib.parse.quote(csrf)}"
    )
    payload = auth.get_json(url, note="cookie/info")
    if not payload:
        report("cookie/info", None, "请求失败")
        return {"refresh": None}
    data = payload.get("data") or {}
    refresh = data.get("refresh")
    report(
        "cookie/info",
        payload.get("code") == 0,
        f"code={payload.get('code')} refresh={refresh} timestamp={data.get('timestamp')}",
    )
    if refresh:
        print(
            "       → 服务端认为需要刷新:走 correspond 页抓 refresh_csrf 的三段式(M5 S2),\n"
            "         注意它要自己实现 RSA-OAEP(SHA-256),依赖里没有 cryptography。"
        )
    return {"refresh": refresh, "timestamp": data.get("timestamp")}


def check_fav_folders(auth: Prober | None, mid: int, key: str) -> list[dict[str, Any]]:
    """列出"我创建的收藏夹",并对同一接口做**不带签名 / 带签名**两段式复验。

    Args:
        auth: 登录臂;``None`` 时跳过。
        mid: 自己的 mid(``nav`` 里拿)。
        key: ``mixin_key``;为空时跳过带签名的复验。

    Returns:
        收藏夹条目列表(每个含 ``id`` / ``title`` / ``media_count`` 等)。
    """
    section("3. 收藏夹列表(fav/folder/created/list-all)")
    if auth is None or not mid:
        report("收藏夹列表", None, "没有凭据或没有 mid,跳过")
        return []
    base = {"up_mid": mid, "web_location": "333.1387"}
    unsigned = auth.get_json(
        f"{API_BASE}/x/v3/fav/folder/created/list-all?{urllib.parse.urlencode(base)}",
        note="list-all(无签名)",
    )
    folders = _folders_of(unsigned)
    report(
        "不带签名能拿到 data",
        bool(folders),
        f"code={unsigned.get('code') if unsigned else None} 收藏夹数={len(folders)}",
    )
    if not folders and key:
        signed = auth.get_json(
            f"{API_BASE}/x/v3/fav/folder/created/list-all?{signed_query(base, key)}",
            note="list-all(带签名)",
        )
        folders = _folders_of(signed)
        report(
            "带签名能拿到 data",
            bool(folders),
            f"code={signed.get('code') if signed else None} 收藏夹数={len(folders)}",
        )
        if folders:
            print("       → 结论:该接口**需要 WBI 签名**,S1 前必须先做 core/wbi.py。")
    for folder in folders[:10]:
        print(
            f"       - media_id={folder.get('id')} title={folder.get('title')!r} "
            f"count={folder.get('media_count')} attr={folder.get('attr')}"
        )
    return folders


def _folders_of(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从收藏夹列表响应里安全地取出条目列表。

    ``data`` 可能是 ``null``(实测:匿名请求就是这种形态),所以两层都要兜。

    Args:
        payload: 完整响应体,或 ``None``。

    Returns:
        条目列表;取不到时返回空列表。
    """
    if not payload:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("list")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def check_fav_resources(
    auth: Prober | None, media_id: int, key: str, page_size: int
) -> dict[str, Any]:
    """取一个收藏夹的内容,并统计条目的真实形态(回答 S0 第 3 问)。

    Args:
        auth: 登录臂;``None`` 时跳过。
        media_id: 目标收藏夹 id。
        key: ``mixin_key``。
        page_size: 一页取多少条。

    Returns:
        含 ``medias`` / ``media_count`` / ``has_more`` / 各项统计的字典。
    """
    section("4. 收藏夹内容(fav/resource/list)")
    if auth is None or not media_id:
        report("收藏夹内容", None, "没有凭据或没有 media_id,跳过")
        return {}
    params = {
        "media_id": media_id,
        "pn": 1,
        "ps": page_size,
        "order": "mtime",
        "type": 0,
        "tid": 0,
        "platform": "web",
    }
    payload = auth.get_json(
        f"{API_BASE}/x/v3/fav/resource/list?{urllib.parse.urlencode(params)}",
        note="resource/list(无签名)",
    )
    medias = _medias_of(payload)
    report(
        "不带签名能拿到 medias",
        bool(medias),
        f"code={payload.get('code') if payload else None} 条数={len(medias)}",
    )
    if not medias and key:
        payload = auth.get_json(
            f"{API_BASE}/x/v3/fav/resource/list?{signed_query(params, key)}",
            note="resource/list(带签名)",
        )
        medias = _medias_of(payload)
        report(
            "带签名能拿到 medias",
            bool(medias),
            f"code={payload.get('code') if payload else None} 条数={len(medias)}",
        )
        if medias:
            print("       → 结论:该接口**需要 WBI 签名**,S1 前必须先做 core/wbi.py。")
    if not medias:
        return {}

    data = payload.get("data") or {}
    info = data.get("info") or {}
    types: dict[str, int] = {}
    for item in medias:
        types[str(item.get("type"))] = types.get(str(item.get("type")), 0) + 1
    multi = [m for m in medias if int(m.get("page") or 1) > 1]
    dead = [m for m in medias if int(m.get("attr") or 0) != 0]
    fields = sorted({name for item in medias for name in item})
    print(
        f"       info.media_count={info.get('media_count')} has_more={data.get('has_more')} "
        f"返回条数={len(medias)}(ps={page_size})"
    )
    print(f"       type 分布: {types}(2=视频 12=音频 21=视频合集)")
    print(f"       多P条目(page>1): {len(multi)}/{len(medias)};失效条目(attr≠0): {len(dead)}")
    report("条目里没有 cid", not any("cid" in item for item in medias), "有 cid 就直接能用,没有就要补 pagelist")
    print(f"       条目字段全集: {fields}")
    return {
        "medias": medias,
        "media_count": info.get("media_count"),
        "has_more": data.get("has_more"),
        "types": types,
        "multi_count": len(multi),
        "dead_count": len(dead),
        "has_cid": any("cid" in item for item in medias),
    }


def _medias_of(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从收藏夹内容响应里安全地取出条目列表。

    Args:
        payload: 完整响应体,或 ``None``。

    Returns:
        条目列表;``data`` 为 ``null`` 或结构不符时返回空列表。
    """
    if not payload:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("medias")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def check_multi_page(prober: Prober, medias: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """对前若干条多P条目补一次 ``pagelist``,确认"分P数怎么拿、cid 用哪一个"。

    这是 S0 里最能决定请求量的一步:条目的 ``page`` 只给分P**数量**,
    真要播必须逐个补一次请求拿 ``cid``(领域铁律)。

    2026-09-14 的实测补充:多P条目里有 ``pagelist`` 返回 ``-404`` 的情况。收藏夹里本来
    就存在**已失效条目**(``attr≠0``,实测一页 20 条里有 6 条),所以这里对拿不到分P的
    条目再用 ``view`` 交叉验证一次,把"条目本身失效"与"分P接口出问题"分开 ——
    这个区分决定 S1 是"跳过失效条目就行"还是"必须换接口"。

    Args:
        prober: 用来发请求的臂(优先用登录臂,顺便观察它是否被风控)。
        medias: 收藏夹条目列表。
        limit: 最多补几条(控制请求量)。

    Returns:
        含 ``checked`` / ``mismatch`` / ``failed`` / ``dead`` 的统计字典;
        ``dead`` 是"``pagelist`` 与 ``view`` 都拿不到"的条目数(即确认失效)。
    """
    section("5. 多P条目的 cid 怎么拿(/x/player/pagelist)")
    candidates = [m for m in medias if int(m.get("page") or 1) > 1][:limit]
    if not candidates:
        report("多P条目复验", None, "本页没有多P条目,跳过")
        return {"checked": 0, "mismatch": 0, "failed": 0, "dead": 0}
    stats = {"checked": 0, "mismatch": 0, "failed": 0, "dead": 0}
    for item in candidates:
        bvid = item.get("bvid") or ""
        attr = int(item.get("attr") or 0)
        if not bvid:
            # type=21(视频合集)没有 bvid,只有 season id:本阶段直接跳过
            report(
                f"条目 {item.get('id')} 补 cid",
                None,
                f"type={item.get('type')} attr={attr} 没有 bvid",
            )
            continue
        payload = prober.get_json(f"{API_BASE}/x/player/pagelist?bvid={bvid}", note="pagelist")
        raw_pages = ((payload or {}).get("data") or []) if payload else []
        pages = [p for p in raw_pages if isinstance(p, dict)]
        stats["checked"] += 1
        if not pages:
            stats["failed"] += 1
            # 交叉验证:view 也拿不到 ⇒ 这条收藏已经失效(UP 删除/稿件下架),不是接口坏了
            view = prober.get_json(f"{API_BASE}/x/web-interface/view?bvid={bvid}", note="view")
            view_code = (view or {}).get("code")
            dead = view_code != 0
            if dead:
                stats["dead"] += 1
            report(
                f"{bvid} 补 cid",
                None if dead else False,
                f"pagelist code={(payload or {}).get('code')} view code={view_code} 条目 attr={attr} "
                + ("⇒ 条目已失效,S1 跳过即可" if dead else "⇒ 接口没给原因,要单独排查"),
            )
            continue
        declared = int(item.get("page") or 0)
        if declared and declared != len(pages):
            stats["mismatch"] += 1
        report(
            f"{bvid} 补 cid",
            True,
            f"条目 page={declared} attr={attr} pagelist={len(pages)} "
            f"第1P cid={pages[0].get('cid')} 第1P 时长={pages[0].get('duration')}秒",
        )
    return stats


def _one_quality_probe(
    anon: Prober, auth: Prober | None, bvid: str, cid: int, *, fnval: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """对一条视频各打一次匿名 / 登录的 ``playurl``,返回两份音频档位摘要。

    Args:
        anon: 匿名臂。
        auth: 登录臂;``None`` 时只打匿名那一发。
        bvid: 视频 BV 号。
        cid: 分P的 cid。
        fnval: ``playurl`` 的 ``fnval`` 取值(``16`` 与 App 对齐,``4048`` 是要全档位的写法)。

    Returns:
        ``(匿名摘要, 登录摘要)``;没打登录那一发时第二项是空字典。
    """
    base = {"bvid": bvid, "cid": cid, "fnval": fnval, "fourk": 1, "try_look": 1}
    url = f"{API_BASE}/x/player/playurl?{urllib.parse.urlencode(base)}"
    payload = anon.get_json(url, note=f"playurl(匿名,fnval={fnval})")
    anon_summary = audio_summary(((payload or {}).get("data") or {}))
    if auth is None:
        return anon_summary, {}
    payload = auth.get_json(url, note=f"playurl(登录,fnval={fnval})")
    return anon_summary, audio_summary(((payload or {}).get("data") or {}))


def check_audio_quality(
    anon: Prober,
    auth: Prober | None,
    candidates: list[tuple[str, str]],
) -> dict[str, Any]:
    """对候选视频逐个做"匿名 vs 登录"的音频档位对比。

    这是**决定 M5 值不值得做**的那一问:若登录换不来更高档位,登录就只剩"收藏夹的前置件"
    这一个身份。

    **为什么要扫多条(2026-09-14 的教训)**:第一版只对收藏夹里的第一条正常视频做对比,
    两条臂都是 ``[30216, 30232, 30280]`` —— 但这**推不出"登录无收益"**:一个本身没有
    Hi-Res / 杜比音轨的视频,登录前后当然一模一样,那只是"这条视频没有会员档位"。
    命中会员档位要靠样本里有那样的视频,所以这里按顺序扫前 N 条,一旦发现增益就停;
    扫完都没有,结论只能写成"这 N 条样本上没有增益",而不是"登录没有收益"。

    Args:
        anon: 匿名臂。
        auth: 登录臂;``None`` 表示只跑匿名基线。
        candidates: ``(bvid, 标题)`` 列表,按优先级排列。

    Returns:
        含 ``rows``(每条一行明细)、``gained``(发现的总增益档位)与 ``scanned`` 的字典。
    """
    section("6. 登录是否换来更高音频档位(决定 M5 收益)")
    rows: list[dict[str, Any]] = []
    gained: list[int] = []
    for bvid, title in candidates:
        view = anon.get_json(f"{API_BASE}/x/web-interface/view?bvid={bvid}", note="view")
        data = (view or {}).get("data") or {}
        cid = int(data.get("cid") or 0)
        if not cid:
            report(f"{bvid} 取 cid", None, f"code={(view or {}).get('code')} 跳过")
            continue
        anon_summary, auth_summary = _one_quality_probe(anon, auth, bvid, cid, fnval=16)
        row: dict[str, Any] = {
            "bvid": bvid,
            "title": title,
            "cid": cid,
            "anon": anon_summary,
            "auth": auth_summary,
        }
        if auth is None:
            report(f"匿名 fnval=16 {bvid}", bool(anon_summary["qualities"]), describe_audio(anon_summary))
            rows.append(row)
            continue
        new = sorted(set(auth_summary["qualities"]) - set(anon_summary["qualities"]))
        row["gained"] = new
        report(
            f"{bvid} {title[:18]!r}",
            bool(new),
            f"匿名 {describe_audio(anon_summary)} / 登录 {describe_audio(auth_summary)}"
            + (f" ⇒ 登录多出 {new}" if new else ""),
        )
        rows.append(row)
        if new:
            # 命中一条就能定案了:顺带用"要全档位"的写法确认会员档位到底有几档,然后收工
            _, auth_full = _one_quality_probe(anon, auth, bvid, cid, fnval=4048)
            report(f"{bvid} 登录 fnval=4048", bool(auth_full.get("qualities")), describe_audio(auth_full))
            row["auth_4048"] = auth_full
            gained = new
            break
    if auth is not None and not gained:
        report(
            "登录档位增益",
            None,
            f"扫了 {len(rows)} 条都没有增益;样本里可能本就没有会员音质视频(可用 --bvid 指定一条)",
        )
    return {"rows": rows, "gained": gained, "scanned": len(rows)}


def print_verdict(
    cookies: dict[str, str],
    session: dict[str, Any],
    cookie_info: dict[str, Any],
    folders: list[dict[str, Any]],
    resources: dict[str, Any],
    pages: dict[str, Any],
    audio: dict[str, Any],
    anon: Prober,
    auth: Prober | None,
) -> None:
    """把各项检查的结果压成一段可直接贴回去的结论清单。

    刻意做成"一行一个结论"的形态:这段文本要能被人一眼读完,也要能被直接复制进
    README「B站接口实测笔记」或 issue。

    Args:
        cookies: 本次使用的 Cookie(只用于判断"有哪些字段",不打印取值)。
        session: :func:`check_session` 的结果。
        cookie_info: :func:`check_cookie_info` 的结果。
        folders: 收藏夹列表。
        resources: :func:`check_fav_resources` 的结果。
        pages: :func:`check_multi_page` 的结果。
        audio: :func:`check_audio_quality` 的结果。
        anon: 匿名臂(取统计)。
        auth: 登录臂;``None`` 表示本次没有凭据。
    """
    section("S0 结论清单(把这一段贴回来即可)")
    mode = "登录" if session.get("is_login") else ("有凭据但登录态无效" if cookies else "匿名基线")
    print(f"模式: {mode}")
    print(f"凭据字段: {mask_cookies(cookies)}")
    print(f"登录态: isLogin={session.get('is_login')} mid={session.get('mid')} vipType={session.get('vip')}")
    print(f"wbi_img: {'可用' if session.get('wbi_key') else '不可用'}")

    print(f"cookie/info: refresh={cookie_info.get('refresh')}")
    print(f"收藏夹列表: {len(folders)} 个")
    if resources:
        print(
            f"收藏夹内容: media_count={resources.get('media_count')} "
            f"has_more={resources.get('has_more')} type 分布={resources.get('types')}"
        )
        print(
            f"  多P={resources.get('multi_count')} 失效={resources.get('dead_count')} "
            f"条目自带 cid={resources.get('has_cid')}"
        )
    if pages and pages.get("checked"):
        print(
            f"  pagelist 复验: 查了 {pages['checked']} 条,分P数不一致 {pages['mismatch']} 条,"
            f"确认失效 {pages.get('dead', 0)} 条"
        )
    if audio.get("rows"):
        print(f"音质对比: 扫了 {audio.get('scanned')} 条正常视频")
        for row in audio["rows"]:
            line = f"  {row['bvid']}: 匿名 {describe_audio(row['anon'])}"
            if row.get("auth"):
                line += f" / 登录 {describe_audio(row['auth'])}"
            if row.get("gained"):
                line += f" ⇒ 登录多出 {row['gained']}"
            print(line)
        print(f"登录档位增益: {audio.get('gained') or '未发现(样本内没有会员音质视频)'}")
    print(f"匿名臂统计: {anon.stats.summary()}")
    if auth is not None:
        print(f"登录臂统计: {auth.stats.summary()}")
    print("\n本轮未覆盖(别当成已验证):")
    print("  · 扫码成功时的凭据形态(Set-Cookie 与 data.url 是否一致)—— 需要真扫一次码")
    print("  · Cookie 保鲜三段式(要自己实现 RSA-OAEP,属 M5 S2)")
    print("  · 扫码轮询的内层码 86090(已扫码未确认)—— 需要真扫一次码")
    print(
        "\n提示: 上面这段不含凭据取值;若要贴出来,请再自查一遍有没有夹带昵称以外的个人信息。"
    )


# ====================================================================== 入口


def main() -> int:
    """按顺序跑完 S0 的各项检查。

    每一项失败都只打印结果、不中断,这样一次运行能拿到完整清单 —— 与
    ``scripts/probe_api.py`` 同一个取舍:接口出问题时,我们最需要的是"哪几项坏了"。

    Returns:
        进程退出码:``0`` 关键检查都跑完了;``1`` 带了凭据但登录态无效
        (其余情况都返回 0,因为"没给凭据"是合法的匿名基线用法)。
    """
    parser = argparse.ArgumentParser(description="B站账号链路只读验证(M5 S0)")
    parser.add_argument("--bvid", default="", help="音质对比用的 BV 号(默认自动挑收藏夹里的正常视频)")
    parser.add_argument(
        "--quality-scan",
        type=int,
        default=5,
        help="音质对比最多扫几条正常视频(默认 5;一条没有会员音轨推不出'登录无收益')",
    )
    parser.add_argument("--media-id", type=int, default=0, help="指定收藏夹 id(默认取列表第一个)")
    parser.add_argument("--ps", type=int, default=MAX_PAGE_SIZE, help=f"内容页条数,上限 {MAX_PAGE_SIZE}")
    parser.add_argument("--max-items", type=int, default=3, help="最多对几条多P条目复验 pagelist")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL, help="请求最小间隔(秒)")
    parser.add_argument("--no-sign", action="store_true", help="不做带 WBI 签名的复验")
    parser.add_argument("--no-credentials", action="store_true", help="只跑匿名基线")
    parser.add_argument("--verbose", action="store_true", help="打印原始响应(可能含个人信息)")
    args = parser.parse_args()

    # Windows 控制台默认 cp936,打印中文与 emoji 会 UnicodeEncodeError。
    # 显式切成 utf-8 并对不可编码字符降级,免得"结论没看到先看到异常"。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cookies = read_credentials(not args.no_credentials)
    print("BiliMusic M5 S0 —— 账号链路只读验证")
    print(f"凭据字段: {mask_cookies(cookies)}(取值不打印、不落盘)")
    if not cookies:
        print("提示: 没有凭据,本次只出匿名基线;带凭据跑才能回答收藏夹与音质那几个问题。")

    anon = Prober("匿名", min_interval=args.min_interval, verbose=args.verbose)
    auth = (
        Prober("登录", cookies=cookies, min_interval=args.min_interval, verbose=args.verbose)
        if cookies
        else None
    )

    session = check_session(anon, auth)
    cookie_info = check_cookie_info(auth, cookies)
    key = "" if args.no_sign else session.get("wbi_key", "")
    folders = check_fav_folders(auth, int(session.get("mid") or 0), key)

    media_id = args.media_id or (int(folders[0].get("id") or 0) if folders else 0)
    resources = check_fav_resources(auth, media_id, key, min(args.ps, MAX_PAGE_SIZE))
    medias = resources.get("medias") or []
    pages = check_multi_page(auth or anon, medias, args.max_items)

    # 音质对比的目标:优先用命令行指定;否则按顺序取收藏夹里的正常视频并**扫多条** ——
    # 因为"某一条视频没有会员音轨"推不出"登录没有收益"(见 check_audio_quality 的说明)
    candidates: list[tuple[str, str]] = []
    if args.bvid:
        candidates.append((args.bvid, "(命令行指定)"))
    else:
        for item in medias:
            if (
                str(item.get("type")) == "2"
                and int(item.get("attr") or 0) == 0
                and item.get("bvid")
            ):
                candidates.append((str(item["bvid"]), str(item.get("title") or "")))
            if len(candidates) >= max(1, args.quality_scan):
                break
    if not candidates:
        candidates.append((DEFAULT_BVID, "(默认目标)"))
    audio = check_audio_quality(anon, auth, candidates)

    print_verdict(cookies, session, cookie_info, folders, resources, pages, audio, anon, auth)
    if cookies and not session.get("is_login"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
