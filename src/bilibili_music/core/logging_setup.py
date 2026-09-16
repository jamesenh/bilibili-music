"""日志的落盘位置、级别、轮转与命名空间。

它是应用里**唯一**装配 ``logging`` 的地方:各层只管 ``get_logger(__name__)`` 之后打日志,
不关心文件写到哪里、按什么格式写。

设计取舍:

* **落盘目录与配置目录物理隔离**。``config.json`` / ``session.json``(明文凭据)都在
  :func:`~bilibili_music.core.config.config_root` 下;日志放在平台自己的"日志目录"里,
  于是"把日志目录整个打包发出去"这件事天然不可能带上凭据 —— 这比"导出时记得排除某个
  文件名"要可靠得多。三平台各走各的惯例,与 ``config_root`` / ``cache_root`` 同一形状。

* **不碰 root logger**。只给 :data:`LOG_ROOT_LOGGER` 挂 handler 并置
  ``propagate = False``,绝不用 ``logging.basicConfig()``:给 root 挂 handler 会把
  ``PySide6`` 等第三方库的日志一起吸进文件,把真正有用的几百行冲成噪声。

* **按天轮转,保留 7 天**。轮转逻辑本身在 :class:`~logging.handlers.TimedRotatingFileHandler`
  里,本模块只负责参数与权限。注意它的滚动检查发生在**写入时**:进程跨天但长时间没有
  日志写入时,文件不会在零点准时切开,而是等到下一条日志落盘那一刻才轮转。这不是缺陷,
  但会让"按日期找昨天那份"的直觉落空 —— 所以每条日志都带毫秒级时间戳,``grep`` 时间比
  认文件名可靠。

  另一个实测结论(CPython 3.11):归档名按"刚结束的那个周期"的日期生成
  (``bilimusic.log.2026-09-14``),而 ``doRollover`` 发现同名归档**已经存在时会直接
  跳过**(源码里的 "Already rolled over")—— 既不覆盖也不截断,日志继续写进当天的
  ``bilimusic.log``。所以磁盘占用是可预测的(最多 ``LOG_KEEP_DAYS + 1`` 个文件),但
  "一个文件正好一天"并不保证:翻日志时按内容里的时间戳找,别只信文件名。

* **写不进去不报错**。:func:`setup_logging` 建档失败(权限、磁盘满)时返回 ``None``,
  由调用方在界面上给一句中文提示。GUI 没有 stderr,静默失败等于让用户白导出一次。
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import zipfile
from collections.abc import Mapping
from io import TextIOWrapper
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 包根只有一句 docstring 与 __version__,它不 import 任何子模块,所以 core 反向引用它
# 不会构成循环导入,也不改变"只允许向下依赖"的分层方向(包根不是分层中的一层)
from .. import __version__
from .redact import install_redaction

__all__ = [
    "DEFAULT_LOG_LEVEL",
    "ENVIRONMENT_FILE_NAME",
    "LOG_ARCHIVE_PATTERN",
    "LOG_FILE_NAME",
    "LOG_KEEP_DAYS",
    "LOG_ROOT_LOGGER",
    "environment_summary",
    "export_logs",
    "get_logger",
    "log_root",
    "normalize_level",
    "setup_logging",
]

#: 应用在系统日志目录下使用的子目录名。
_APP_DIR_NAME = "BiliMusic"

#: 日志文件名。按天轮转的归档形如 ``bilimusic.log.2026-09-14``。
LOG_FILE_NAME = "bilimusic.log"

#: 本项目所有 logger 的公共前缀。第三方库的日志不会挂在这棵树上,也因此不会落盘。
LOG_ROOT_LOGGER = "bilibili_music"

#: 默认级别。``DEBUG`` 需要手改 ``config.json`` —— 没有界面入口,见 ``README.md``。
DEFAULT_LOG_LEVEL = "INFO"

#: 日志归档保留份数。当天那份另算,所以磁盘上最多是 ``LOG_KEEP_DAYS + 1`` 个文件。
LOG_KEEP_DAYS = 7

#: 导出时只收这些文件。
#:
#: 是**白名单**而不是"目录里全收":日志目录已经与凭据目录物理隔离(见模块 docstring),
#: 而导出包是要发给别人看的,多一道保险不亏 —— 将来谁往这个目录里放点别的也不会被打包。
LOG_ARCHIVE_PATTERN = "*.log*"

#: 导出包里那份环境摘要的文件名。
ENVIRONMENT_FILE_NAME = "environment.txt"

#: 日志行格式。带毫秒是为了让并发/重试的先后顺序可靠可读;
#: 带 logger 名与行号是为了不用先猜"这条是哪层打的"。
_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s:%(lineno)d "
    "[%(threadName)s] %(message)s"
)
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: ``logging`` 里不允许写进配置的级别名。
#:
#: ``NOTSET`` 会让 logger 退回向父级传播,而父级(root)没有 handler —— 看起来"日志开了"
#: 其实一条都不会落盘,属于最难查的那种配置错误,所以直接拒绝。
_REJECTED_LEVEL_NAMES: frozenset[str] = frozenset({"NOTSET"})

# 项目根 logger 上的"空处理器",stdlib 推荐的库写法。
#
# 它的作用是**把静音做完干净**:没有装配日志时(单测、脚本、别的程序 import 本包),
# 本项目的记录会一路传到 root,而 root 上没有 handler —— 那时 ``logging.lastResort``
# 会把 WARNING 以上直接打到 stderr,污染调用方的输出。挂一个空处理器后,记录"找到了
# handler"(发现空处理器也算),lastResort 就不会接管。
#
# 它在 :func:`setup_logging` 之后仍然留着(空处理器不输出任何东西),所以
# :func:`_detach_handlers` 会**跳过**它 —— 否则一次 ``force=True`` 重配就把这道静音拆了。
logging.getLogger(LOG_ROOT_LOGGER).addHandler(logging.NullHandler())


# ============================== 目录


def log_root() -> Path:
    """返回平台相关的日志目录(不保证已存在)。

    三个平台各走各的惯例,与 :func:`~bilibili_music.core.config.config_root`、
    :func:`~bilibili_music.core.cache.cache_root` 同一形状:

    * Windows 用 ``%LOCALAPPDATA%``(不用 ``%APPDATA%`` —— 日志没有跟着漫游同步的意义)
    * macOS 用 ``~/Library/Logs``(用户与 Console.app 找日志的标准位置)
    * 其余按 XDG 规范用 ``$XDG_STATE_HOME``,未设置时退回 ``~/.local/state``

    Returns:
        日志目录路径;目录本身可能还不存在,由 :func:`setup_logging` 负责创建。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / _APP_DIR_NAME / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / _APP_DIR_NAME
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / _APP_DIR_NAME


def _restrict_permissions(path: str) -> None:
    """把日志文件权限收紧到只有属主可读写(POSIX)。

    日志里会出现搜索关键词与 ``bvid``,默认的 ``0o644`` 对同机其他用户是可读的,没有
    理由给这份便利。Windows 没有这个语义(``os.chmod`` 只能改只读位),直接跳过;
    权限设置失败也**不能**让日志失效 —— 它是排障的最后一道保险,不该因为收权限反而消失。

    Args:
        path: 日志文件路径。
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class _DailyFileHandler(TimedRotatingFileHandler):
    """按天轮转的日志处理器,额外把新建文件的权限收紧到 ``0o600``。

    权限必须在**每次打开文件**时设置:轮转之后 Python 会重新 ``open`` 一份新文件,只在
    启动时 ``chmod`` 一次的话,第二天起新文件又变回默认权限了。

    继承而不是"建完再 chmod":后者拿不到"轮转后新建"这个时机,除非把 handler 的私有
    方法整个抄一遍 —— 那才是真的脆弱。
    """

    def _open(self) -> TextIOWrapper:
        """打开日志文件,并把它的权限收紧到只有属主可读写。

        Returns:
            底层 :class:`~logging.handlers.TimedRotatingFileHandler` 打开的文本流。
        """
        stream = super()._open()
        _restrict_permissions(self.baseFilename)
        return stream


# ============================== 级别


def normalize_level(value: object) -> str:
    """把配置里的级别收敛成一个可用的大写级别名。

    配置是不可信输入:``log_level`` 可能被手改成 ``"verbose"``、``123``、``null``。
    非法值一律退回 :data:`DEFAULT_LOG_LEVEL` 而不是报错 —— 为一句写错的配置让应用起不来,
    比"少记几条 DEBUG"严重得多。

    ``WARN`` 是 ``WARNING`` 的常见别名,照收;``NOTSET`` 见
    :data:`_REJECTED_LEVEL_NAMES` 的说明,拒绝。

    Args:
        value: 配置里的原始值。

    Returns:
        合法的大写级别名(如 ``"DEBUG"``);不合法时返回 :data:`DEFAULT_LOG_LEVEL`。
    """
    if not isinstance(value, str):
        return DEFAULT_LOG_LEVEL
    name = value.strip().upper()
    if name == "WARN":
        name = "WARNING"
    if name in _REJECTED_LEVEL_NAMES:
        return DEFAULT_LOG_LEVEL
    # 用 logging 自己的映射表校验,而不是抄一份级别名清单 —— 抄的那份迟早会漂移
    if name not in logging.getLevelNamesMapping():
        return DEFAULT_LOG_LEVEL
    return name


def get_logger(name: str) -> logging.Logger:
    """取得本项目命名空间下的 logger。

    各模块统一用 ``get_logger(__name__)`` 取 logger,于是"哪些日志会落盘"这件事只由
    :data:`LOG_ROOT_LOGGER` 一处决定:名字不在这个前缀下的 logger 不挂在同一棵树上,
    也就不会进日志文件。

    Args:
        name: 模块名(通常直接传 ``__name__``)。

    Returns:
        对应的 :class:`logging.Logger`;``name`` 不在项目命名空间下时自动补上前缀。
    """
    if name == LOG_ROOT_LOGGER or name.startswith(LOG_ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOG_ROOT_LOGGER}.{name}")


# ============================== 装配


def _detach_handlers(logger: logging.Logger) -> None:
    """摘掉并关闭 logger 上已经挂着的 handler。

    只在 ``force=True`` 重配时用到(测试里反复装配同一棵 logger 会走到这里)。
    ``NullHandler`` 例外:它是"未装配时不往 stderr 噪音"的兼底,摘了就得自己记得装回来。

    Args:
        logger: 目标 logger。
    """
    for handler in list(logger.handlers):
        if isinstance(handler, logging.NullHandler):
            continue
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关不掉也不影响"已从 logger 上摘下"这个事实
            pass


def _configured_handler(logger: logging.Logger) -> logging.Handler | None:
    """取当前装配好的文件处理器(把静音用的 ``NullHandler`` 排除在外)。

    Args:
        logger: 项目根 logger。

    Returns:
        已装配的文件处理器;只装了 ``NullHandler`` 时返回 ``None``。
    """
    for handler in logger.handlers:
        if not isinstance(handler, logging.NullHandler):
            return handler
    return None


def _active_log_path(logger: logging.Logger, *, fallback: Path) -> Path:
    """取当前已经装配好的日志文件路径。

    已经装配过时,``setup_logging`` 必须返回**实际生效**的那个路径,而不是这次调用
    传进来的候选路径:否则调用方(界面)会拿着一个并不存在的路径去"打开日志目录",
    用户看到的是空目录。

    Args:
        logger: 项目根 logger。
        fallback: 找不到任何 handler 时的兜底路径。

    Returns:
        已有 handler 正在写的文件路径;没有则返回 ``fallback``。
    """
    for handler in logger.handlers:
        name = getattr(handler, "baseFilename", None)
        if name:
            return Path(name)
    return fallback


def setup_logging(
    *,
    root: Path | None = None,
    level: str = DEFAULT_LOG_LEVEL,
    force: bool = False,
) -> Path | None:
    """装配日志落盘,返回实际使用的日志文件路径。

    幂等:同一个 logger 已经装过就**直接返回实际生效的路径**,不会挂上第二个文件处理器
    (否则每条日志会被写两遍)。测试要反复装配时传 ``force=True``,它会先摘掉旧的处理器。

    只在 ``ui/main_window.py::run()`` 里调用一次 —— ``core`` 模块在 import 期不允许有
    文件系统副作用,所以本函数不会被任何模块的模块级代码调用。

    Args:
        root: 日志目录;``None`` 表示用平台默认的 :func:`log_root`。可注入是为了让单测
            落在临时目录里,不必碰用户真实的日志目录。
        level: 级别名(不区分大小写,``WARN`` 视作 ``WARNING``);非法值退回
            :data:`DEFAULT_LOG_LEVEL`。
        force: 为真时先摘掉已挂的 handler 再重新装配。

    Returns:
        日志文件路径;目录建不出来或文件打不开时返回 ``None``(调用方应据此在界面上
        提示"日志未启用",而不是让应用起不来)。已经装配过时返回**实际生效**的那个
        路径,忽略本次传入的 ``root``。
    """
    directory = Path(root) if root is not None else log_root()
    path = directory / LOG_FILE_NAME
    logger = logging.getLogger(LOG_ROOT_LOGGER)
    if _configured_handler(logger) is not None and not force:
        return _active_log_path(logger, fallback=path)
    if force:
        _detach_handlers(logger)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = _DailyFileHandler(
            path,
            when="midnight",
            interval=1,
            backupCount=LOG_KEEP_DAYS,
            encoding="utf-8",  # 必须显式:Windows 默认 cp936 会让中文日志乱码甚至抛异常
            delay=False,  # 启动即建档:空文件也比"用户找不到文件"好
        )
    except (OSError, ValueError):
        return None
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
    install_redaction(handler)
    logger.setLevel(logging.getLevelName(normalize_level(level)))
    # 不再向 root 传播:root 上没有 handler,但传播会让日志去走 logging.lastResort
    # (把 WARNING 以上打到 stderr),GUI 里那是不可见也无意义的
    logger.propagate = False
    logger.addHandler(handler)
    _write_banner(logger, path=path, level=level)
    return path


def _write_banner(logger: logging.Logger, *, path: Path, level: str) -> None:
    """写一条启动横幅,给日志加上"这是哪一次运行"的分界。

    除了分隔作用,它同时也是环境摘要的落盘副本:用户把日志发过来时,不需要再额外问
    "你是什么版本、什么系统"。

    Args:
        logger: 项目根 logger。
        path: 本次使用的日志文件路径。
        level: 本次生效的级别(可能是非法值回退后的结果)。
    """
    logger.info("%s 启动,级别 %s", "=" * 24, normalize_level(level))
    summary = environment_summary(level=level, log_dir=path.parent)
    logger.info("环境 %s", " ".join(f"{key}={value}" for key, value in summary.items()))


def environment_summary(
    *,
    level: str = DEFAULT_LOG_LEVEL,
    log_dir: Path | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """汇总一份"出问题时需要先知道"的环境信息。

    刻意**不含**任何账号标识(``uname`` / ``mid``):用户把日志和这份摘要一起发出来时,
    里面不该有"这是谁"的信息 —— 排障需要的是版本与平台,不是身份。

    Qt 的版本号由调用方通过 ``extra`` 传进来:``core`` 不许 import Qt(见
    ``AGENTS.md`` 第 4.1 节),而 Qt 版本恰恰只有界面层拿得到。

    Args:
        level: 当前生效的日志级别名。
        log_dir: 实际使用的日志目录;``None`` 表示按平台默认目录报告。
        extra: 额外条目(如 ``{"Qt": "6.8.3"}``),同名键会覆盖默认值。

    Returns:
        条目名(中文,面向用户可读)到取值的映射。
    """
    summary = {
        "应用版本": __version__,
        "平台": f"{sys.platform} {platform.release()} {platform.machine()}",
        "Python": f"{platform.python_implementation()} {platform.python_version()}",
        "日志级别": normalize_level(level),
        "日志目录": str(Path(log_dir) if log_dir is not None else log_root()),
        "日志保留": f"{LOG_KEEP_DAYS} 天",
    }
    if extra:
        summary.update(extra)
    return summary


def export_logs(
    target: Path,
    *,
    log_dir: Path | None = None,
    summary: Mapping[str, str] | None = None,
) -> Path:
    """把日志打包成一个 zip,里面顺带放一份环境摘要。

    打包本身不需要先 flush:``StreamHandler`` 每写一条就 flush 一次,磁盘上的日志
    始终是最新的。

    包里的内容:日志目录下匹配 :data:`LOG_ARCHIVE_PATTERN` 的**普通文件**,加上
    ``environment.txt``。**不含** ``config.json`` / ``session.json`` / ``library.db``
    —— 前两个在配置目录里(与日志目录物理隔离),后一个也不在这个目录下。

    Args:
        target: 压缩包要写到哪个路径;父目录不存在时会自建。
        log_dir: 日志目录;``None`` 表示用平台默认目录。
        summary: 写进 ``environment.txt`` 的条目;``None`` 表示用
            :func:`environment_summary` 的默认结果。

    Returns:
        实际写出的压缩包路径。

    Raises:
        OSError: 目标写不出去(无权限、磁盘满、路径已被同名的目录占住)。
    """
    directory = Path(log_dir) if log_dir is not None else log_root()
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path for path in directory.glob(LOG_ARCHIVE_PATTERN) if path.is_file()
    )
    entries = summary if summary is not None else environment_summary(log_dir=directory)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
        text = "\n".join(f"{key}={value}" for key, value in entries.items())
        archive.writestr(ENVIRONMENT_FILE_NAME, text + "\n")
    return destination
