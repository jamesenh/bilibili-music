"""登录会话(账号凭据)的模型与落盘。

**为什么放在 ``core``**:凭据本体只是一份"名字到取值"的映射加几个元数据,读写是纯文件
操作 —— 不需要事件循环,也不需要 Qt。放在这一层,测试可以注入沙箱路径,既不用创建
``QApplication``, 也不会碰到用户真实的 ``%APPDATA%``(见 ``AGENTS.md`` 第 4.1 节)。

**落盘形态是明文 JSON(用户 2026-09-14 明确拍板,不是默认值)**:

* 位置与 ``config.json`` 同级(Windows 是 ``%APPDATA%\\BiliMusic\\session.json``),
  写入沿用"先写 ``.part`` 再 ``os.replace``"的原子纪律 —— 半截凭据文件比没有更糟。
* **它是明文**:``SESSDATA`` 等价于账号密码,任何能读到这个文件的程序都能接管该账号。
  所以配套了三件事,缺一不可:

  1. POSIX 下落盘权限收紧到 ``0o600``(Windows 上 ``%APPDATA%`` 本身已是每用户目录,
     没有再做等价处理);
  2. 界面必须**明示**"凭据以明文保存在 <路径>"并给一键登出(登出即删文件);
  3. ``AGENTS.md`` 第 5 节第 10 条禁止把凭据写进日志、异常消息与调试输出。

  Windows 的 DPAPI 加密是**将来可选的加固方向**,不是当前实现;真要换,只需改
  :meth:`SessionStore.save` / :meth:`SessionStore.load` 这两处,调用方不用动。
* **刻意不保存 Cookie 的过期时间**:expires / domain 一律由服务端说了算。本地记一个
  可能算错的过期点,只会把"其实还有效"的凭据误判成失效。登录态是否还有效,一律问
  ``nav.isLogin`` —— README「账号链路」里记过:收藏夹接口在未登录时也返回 ``code=0``,
  拿它判断会得到假阳性。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import config_root
from .logging_setup import get_logger

#: 本模块的日志器。命名空间由 ``core.logging_setup`` 统一决定。
#:
#: **纪律最严的一个模块**(``AGENTS.md`` 5.10):这里只允许记"有没有、几项、都是哪些名字",
#: 取值(``SESSDATA`` / ``bili_jct`` / ``refresh_token``)一个字都不许出现。
#: 名字本身不是秘密 —— 排查"凭据没生效"时要知道的恰恰是"带了哪几个名字"。
_LOGGER = get_logger(__name__)

__all__ = [
    "REQUIRED_COOKIE_NAMES",
    "SESSION_FILE_NAME",
    "Session",
    "SessionStore",
    "parse_cookie_header",
    "session_from_cookies",
]

#: 会话文件名(与 ``config.json`` 同目录)。
SESSION_FILE_NAME = "session.json"

#: 缺了这些名字就不能算登录态。只钉 ``SESSDATA``:它是鉴权的唯一凭据,
#: ``bili_jct`` / ``DedeUserID`` 缺失只影响写操作与显示,不该让整个会话作废。
REQUIRED_COOKIE_NAMES: tuple[str, ...] = ("SESSDATA",)


def parse_cookie_header(raw: str) -> dict[str, str]:
    """把浏览器里粘出来的 ``Cookie`` 头解析成字典。

    刻意不用 ``http.cookies.SimpleCookie``:它处理含 ``=`` 的取值(``SESSDATA`` 里就有)
    与引号的方式和浏览器不一致,而这里要的是"原样搬运"。取值**不做 URL 解码** ——
    ``SESSDATA`` 本身就是 percent-encoded 的,解一次再拼回去就可能失效。

    Args:
        raw: ``Cookie`` 头原文,形如 ``SESSDATA=a%2Cb; bili_jct=c``;允许换行与多余空白。

    Returns:
        名字到取值的映射;空段、不含 ``=`` 的脏段以及取值里只有空白的那一项都会被跳过。
    """
    cookies: dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        # 空取值的 Cookie 没有任何意义(浏览器也不会这么发),留着只会让"有没有凭据"
        # 这种判断出现假阳性
        if name and value:
            cookies[name] = value
    return cookies


def session_from_cookies(
    cookies: dict[str, str],
    *,
    uname: str = "",
    mid: int = 0,
    now: float | None = None,
) -> Session:
    """用一份 Cookie 造一个会话对象,并盖上保存时间。

    ``uname`` / ``mid`` 允许先留空:它们是**显示用**的元数据,登录成功后由 ``nav`` 补,
    不该因为"还没拿到昵称"就让一次凭据保存失败。

    Args:
        cookies: 名字到取值的映射(通常来自 :func:`parse_cookie_header`)。
        uname: 账号昵称;拿不到时留空。
        mid: 账号 mid;拿不到时填 ``0``。
        now: 覆盖保存时间戳,单位为秒;``None`` 表示取当前时间(测试用注入值)。

    Returns:
        已做过合法性收敛的 :class:`Session`。
    """
    return Session(
        cookies=dict(cookies),
        uname=str(uname or ""),
        mid=max(0, int(mid or 0)),
        saved_at=time.time() if now is None else float(now),
    ).normalized()


@dataclass(slots=True)
class Session:
    """一次登录会话。

    只看数据、不做 IO:读写的坑(坏文件、原子替换、权限)集中在 :class:`SessionStore`,
    这样字段本身可以脱离文件系统单测。

    Attributes:
        cookies: 名字到取值的映射,例如 ``{"SESSDATA": "...", "bili_jct": "..."}``。
        uname: 账号昵称,仅用于界面显示。
        mid: 账号 mid,用于取"我创建的收藏夹"(``up_mid`` 参数)。
        saved_at: 保存时间(Unix 秒),仅用于界面显示"登录于 …"。
    """

    cookies: dict[str, str] = field(default_factory=dict)
    uname: str = ""
    mid: int = 0
    saved_at: float = 0.0

    def normalized(self) -> Session:
        """返回一份把脏数据清掉的新会话。

        清理口径:名字与取值都必须是**非空字符串**,重复名字后者覆盖前者;``mid`` 为负
        归零。收敛放在这里而不是读取时,是因为 :meth:`SessionStore.save` 也要用它 ——
        保证脏数据永远不会落盘。

        Returns:
            新的 :class:`Session`;``self`` 不被修改。
        """
        cookies = {
            str(name): str(value)
            for name, value in self.cookies.items()
            if str(name).strip() and str(value).strip()
        }
        return Session(
            cookies=cookies,
            uname=str(self.uname or ""),
            mid=max(0, int(self.mid or 0)),
            saved_at=max(0.0, float(self.saved_at or 0.0)),
        )

    @property
    def is_usable(self) -> bool:
        """这份会话是否具备"能拿去请求"的最小凭据。

        Returns:
            :data:`REQUIRED_COOKIE_NAMES` 里的名字都拿到了非空取值时为 ``True``。
        """
        return all(self.cookies.get(name, "").strip() for name in REQUIRED_COOKIE_NAMES)


class SessionStore:
    """会话文件的读写。

    用法与 :class:`~bilibili_music.core.config.ConfigStore` 一致:先 :meth:`load`,
    改完再 :meth:`save`。平台目录由 :func:`~bilibili_music.core.config.config_root`
    解析,**路径可注入**,所以测试不会写到用户真实目录里。

    Args:
        path: 会话文件路径;``None`` 表示 ``config_root() / SESSION_FILE_NAME``。
    """

    def __init__(self, path: Path | None = None) -> None:
        """记录文件路径(不建目录、不读文件)。

        Args:
            path: 会话文件路径;``None`` 表示用默认位置。传入自定义路径主要用于测试。
        """
        self.path: Path = (
            Path(path) if path is not None else config_root() / SESSION_FILE_NAME
        )

    # ------------------------------------------------------------ 读

    def load(self) -> Session | None:
        """读取会话;**任何形式的不可用都返回 ``None``,不抛异常**。

        以下情况一律算"没有可用凭据":文件不存在(从没登录过)、不是合法 JSON(写到一半
        被杀)、顶层不是对象(手改成数组)、``cookies`` 不是对象、缺 ``SESSDATA``。
        调用方因此只需要判断"有没有",不用先处理异常再判断。

        Returns:
            已收敛的 :class:`Session`;读不到或凭据不完整时返回 ``None``。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None  # 从未登录过,启动时每天都走这条,不该记日志
        except (OSError, ValueError) as exc:
            _LOGGER.warning("会话文件不可用,按未登录处理 原因=%s", exc)
            return None
        if not isinstance(raw, dict):
            _LOGGER.warning("会话文件顶层不是对象,按未登录处理")
            return None
        raw_cookies = raw.get("cookies")
        if not isinstance(raw_cookies, dict):
            _LOGGER.warning("会话文件里 cookies 不是对象,按未登录处理")
            return None
        session = Session(
            cookies={
                str(name): str(value)
                for name, value in raw_cookies.items()
                if isinstance(name, str) and isinstance(value, str)
            },
            uname=str(raw.get("uname") or ""),
            mid=_as_int(raw.get("mid"), 0),
            saved_at=_as_float(raw.get("saved_at"), 0.0),
        ).normalized()
        if not session.is_usable:
            _LOGGER.warning(
                "会话文件里没有可用凭据(缺 %s),按未登录处理",
                "/".join(REQUIRED_COOKIE_NAMES),
            )
            return None
        # 只记名字与数量,取值绝不落盘
        _LOGGER.info(
            "已读取会话 cookie=%d 项 名字=%s",
            len(session.cookies),
            ",".join(sorted(session.cookies)) or "无",
        )
        return session

    # ------------------------------------------------------------ 写

    def save(self, session: Session) -> Path:
        """原子地把会话写盘(明文 JSON)。

        先在同目录写 ``.part`` 再 ``os.replace`` —— 同一文件系统内的替换是原子的,
        "文件存在"因此等价于"它是一份完整凭据"。临时文件与正式文件同目录,正是为了
        保住这个前提。POSIX 上在 ``replace`` **之前**把权限设成 ``0o600``,
        这样正式文件从出现的第一刻起就是"仅本人可读写",不存在短暂的宽松窗口。

        Args:
            session: 要保存的会话;内部会先 :meth:`Session.normalized` 再写。

        Returns:
            实际写入的文件路径(:attr:`path`)。

        Raises:
            OSError: 目录建不出来或写盘失败(磁盘满、无权限、文件被占用)。
        """
        path = self.path
        cleaned = session.normalized()
        text = json.dumps(asdict(cleaned), ensure_ascii=False, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        _LOGGER.info(
            "保存会话 cookie=%d 项 名字=%s 路径=%s",
            len(cleaned.cookies),
            ",".join(sorted(cleaned.cookies)) or "无",
            path,
        )
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            tmp.write_text(text, encoding="utf-8")
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            # 失败必须清掉 .part,否则用户目录里会留一个永远不会被复用的凭据残片
            tmp.unlink(missing_ok=True)
            raise
        return path

    # ------------------------------------------------------------ 登出

    def clear(self) -> bool:
        """删除会话文件(登出)。

        刻意**不抛异常**:登出时文件可能早就被用户手删了,那不是错误。返回布尔值是为了
        让界面能如实说清"删掉了"还是"本来就没有"。

        Returns:
            真的删掉了一个已存在的文件时为 ``True``;文件本来就不存在时为 ``False``。
        """
        try:
            self.path.unlink()
        except FileNotFoundError:
            _LOGGER.info("登出:凭据文件本来就不存在")
            return False
        except OSError as exc:
            # 被占用/无权限时不要抛:登出是收尾动作,失败也要让调用方继续走下去。
            # 但必须留痕 —— 这正是"用户以为登出了,凭据其实还在"的那种坑。
            _LOGGER.warning("登出:凭据文件删不掉 原因=%s", exc)
            return False
        _LOGGER.info("登出:凭据文件已删除")
        return True


def _as_int(value: object, default: int) -> int:
    """把不可信的值转成整数;转不了就用 ``default``。

    Args:
        value: 待转换的原始值(可能来自被手改过的 JSON)。
        default: 转换失败时的兜底值。

    Returns:
        转换后的整数,或 ``default``。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    """把不可信的值转成浮点数;转不了就用 ``default``。

    Args:
        value: 待转换的原始值。
        default: 转换失败时的兜底值。

    Returns:
        转换后的浮点数,或 ``default``。
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
