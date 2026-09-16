"""日志装配的单元测试:目录解析、级别收敛、落盘、权限与按天轮转。

不碰 Qt、不触网。落盘类用例一律把日志目录注入到 ``tests/_scratch/<用例名>``,
**不用** ``tempfile.TemporaryDirectory()``(见 ``AGENTS.md`` 第 7.1 节的沙箱陷阱),
也因此**永远不会碰到**用户真实的 ``~/Library/Logs``。
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music import __version__  # noqa: E402
from bilibili_music.core.logging_setup import (  # noqa: E402
    DEFAULT_LOG_LEVEL,
    ENVIRONMENT_FILE_NAME,
    LOG_FILE_NAME,
    LOG_KEEP_DAYS,
    LOG_ROOT_LOGGER,
    environment_summary,
    export_logs,
    get_logger,
    log_root,
    normalize_level,
    setup_logging,
)

#: 落盘类用例的临时目录根;每个用例用自己名字的子目录,互不干扰。
_SCRATCH = Path(__file__).resolve().parent / "_scratch"

#: 轮转实验里"一天"的秒数。
_DAY = 86400


def _detach_project_handlers() -> None:
    """摘掉项目 logger 上挂着**文件**处理器(空处理器留着)。

    用例之间必须互相隔离:上一个用例的 handler 指向的是已经被删掉的临时目录,
    留着它会让后续用例的日志静默写进黑洞。
    ``NullHandler`` 跳过的理由与 ``core.logging_setup._detach_handlers`` 一致:
    它是"未装配时不往 stderr 噪音"的兼底,摘了下一个用例就会开始喷日志。
    """
    logger = logging.getLogger(LOG_ROOT_LOGGER)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.NullHandler):
            continue
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)


def _file_handlers() -> list[logging.Handler]:
    """取项目根 logger 上已装配的文件处理器(便于断言"只有一个")。

    Returns:
        非 ``NullHandler`` 的处理器列表。
    """
    return [
        handler
        for handler in logging.getLogger(LOG_ROOT_LOGGER).handlers
        if not isinstance(handler, logging.NullHandler)
    ]


class TestLogRoot(unittest.TestCase):
    """验证三个平台的日志目录落点(与 config_root / cache_root 同一形状)。"""

    def test_macos_uses_library_logs(self) -> None:
        """macOS 用 ``~/Library/Logs``:用户与 Console.app 找日志的标准位置。"""
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(log_root(), Path.home() / "Library" / "Logs" / "BiliMusic")

    def test_windows_uses_local_appdata(self) -> None:
        """Windows 用 ``%LOCALAPPDATA%``(日志没有跟着漫游同步的意义)。"""
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {"LOCALAPPDATA": "/fake/Local"}),
        ):
            self.assertEqual(log_root(), Path("/fake/Local") / "BiliMusic" / "Logs")

    def test_linux_honours_xdg_state_home(self) -> None:
        """其余平台按 XDG 规范用 ``$XDG_STATE_HOME``。"""
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/fake/state"}),
        ):
            self.assertEqual(log_root(), Path("/fake/state") / "BiliMusic")

    def test_linux_falls_back_to_local_state(self) -> None:
        """``$XDG_STATE_HOME`` 未设置时退回 ``~/.local/state``。"""
        env = dict(os.environ)
        env.pop("XDG_STATE_HOME", None)
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            self.assertEqual(log_root(), Path.home() / ".local" / "state" / "BiliMusic")

    def test_log_directory_differs_from_config_directory(self) -> None:
        """日志目录与配置目录必须不同。

        ``config.json`` / ``session.json``(明文凭据)都在配置目录下;"日志与凭据物理隔离"
        是整套方案的前提,也是"打开日志目录"这个入口敢直接给用户用的原因。
        """
        from bilibili_music.core.config import config_root

        with mock.patch.object(sys, "platform", "darwin"):
            self.assertNotEqual(log_root(), config_root())


class TestNormalizeLevel(unittest.TestCase):
    """验证级别名的收敛:非法值必须回退,而不是让应用起不来。"""

    def test_accepts_standard_names_case_insensitively(self) -> None:
        """标准级别名不区分大小写,前后空白也容忍。"""
        self.assertEqual(normalize_level("debug"), "DEBUG")
        self.assertEqual(normalize_level("  Warning "), "WARNING")

    def test_accepts_warn_alias(self) -> None:
        """``WARN`` 是 ``WARNING`` 的常见别名,照收。"""
        self.assertEqual(normalize_level("WARN"), "WARNING")

    def test_rejects_notset(self) -> None:
        """``NOTSET`` 被拒绝。

        它会让 logger 退回向父级传播,而父级(root)没有 handler —— 看起来"日志开了"其实
        一条都不落盘,是最难查的那种配置错误。
        """
        self.assertEqual(normalize_level("NOTSET"), DEFAULT_LOG_LEVEL)

    def test_invalid_values_fall_back_to_default(self) -> None:
        """手改成乱七八糟的值一律退回默认级别。"""
        for value in ("verbose", "", "   ", 123, None, [], "10"):
            with self.subTest(value=value):
                self.assertEqual(normalize_level(value), DEFAULT_LOG_LEVEL)


class TestSetupLogging(unittest.TestCase):
    """验证落盘:建目录、建文件、级别过滤、权限与幂等。"""

    def setUp(self) -> None:
        """准备一块专属临时目录并摘掉残留 handler。"""
        _detach_project_handlers()
        self.root = _SCRATCH / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)

    def tearDown(self) -> None:
        """关掉 handler 再删目录(反过来会往已删除的路径写)。"""
        _detach_project_handlers()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_creates_directory_and_file(self) -> None:
        """目录不存在时自建,并返回实际使用的日志文件路径。"""
        path = setup_logging(root=self.root / "logs")
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.exists())
        self.assertEqual(path.name, LOG_FILE_NAME)

    def test_chinese_messages_survive_utf8(self) -> None:
        """中文日志不乱码 —— ``encoding="utf-8"`` 必须显式传。"""
        path = setup_logging(root=self.root)
        assert path is not None
        get_logger("bilibili_music.tests.logging").info("缓存命中:周杰伦 - 晴天")
        content = path.read_text(encoding="utf-8")
        self.assertIn("缓存命中:周杰伦 - 晴天", content)

    def test_banner_records_version_and_platform(self) -> None:
        """启动横幅带上版本与平台,用户只需要发日志,不必再回答"你是什么版本"。"""
        path = setup_logging(root=self.root)
        assert path is not None
        content = path.read_text(encoding="utf-8")
        self.assertIn(__version__, content)
        self.assertIn(sys.platform, content)

    def test_level_filters_out_lower_levels(self) -> None:
        """级别过滤生效:``WARNING`` 时 ``INFO`` 不落盘。"""
        path = setup_logging(root=self.root, level="WARNING")
        assert path is not None
        logger = get_logger("bilibili_music.tests.logging")
        logger.info("这条不该出现")
        logger.warning("这条该出现")
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("这条不该出现", content)
        self.assertIn("这条该出现", content)

    def test_logger_level_follows_configuration(self) -> None:
        """logger 的生效级别就是收敛后的级别。"""
        setup_logging(root=self.root, level="debug")
        self.assertEqual(logging.getLogger(LOG_ROOT_LOGGER).level, logging.DEBUG)

    def test_logger_does_not_propagate_to_root(self) -> None:
        """不向 root 传播:否则逃到 ``lastResort`` 会把 WARNING 以上打到 stderr。

        GUI 里 stderr 既不可见也无意义,而 root 上挂 handler 会把第三方库日志一起吸进来。
        """
        setup_logging(root=self.root)
        logger = logging.getLogger(LOG_ROOT_LOGGER)
        self.assertFalse(logger.propagate)
        self.assertEqual(len(_file_handlers()), 1)

    def test_unconfigured_logger_stays_silent(self) -> None:
        """没装配日志时,项目命名空间下的 WARNING **不**打到 stderr。

        依据是 stdlib 的 ``lastResort`` 规则:只要往上找得到任意一个 handler(包括
        ``NullHandler``),就不再走最后兼底输出。GUI 没有可看的 stderr,测试与脚本
        的 stdout 也不该被日志污染。
        """
        stream = io.StringIO()
        _detach_project_handlers()
        logger = get_logger("bilibili_music.tests.silent")
        with mock.patch.object(logging, "lastResort", logging.StreamHandler(stream)):
            logger.warning("这条不该出现在任何地方")
        self.assertEqual(stream.getvalue(), "")
        self.assertTrue(
            any(
                isinstance(item, logging.NullHandler)
                for item in logging.getLogger(LOG_ROOT_LOGGER).handlers
            )
        )

    @unittest.skipIf(os.name == "nt", "Windows 没有 POSIX 权限位")
    def test_file_permissions_are_owner_only(self) -> None:
        """日志文件权限收紧到 ``0o600``:里面会出现搜索关键词与 bvid。"""
        path = setup_logging(root=self.root)
        assert path is not None
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "Windows 没有 POSIX 权限位")
    def test_archives_are_also_owner_only(self) -> None:
        """轮转产生的新文件同样是 ``0o600``。

        只在启动时 chmod 一次是不够的 —— 归档是轮转时新打开的文件。
        """
        path = setup_logging(root=self.root)
        assert path is not None
        handler = _file_handlers()[0]
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
        handler.rolloverAt = int(time.time()) + _DAY
        handler.doRollover()
        archives = sorted(self.root.glob(f"{LOG_FILE_NAME}.*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)

    def test_second_call_is_a_no_op(self) -> None:
        """重复装配不会挂上第二个文件处理器(否则每条日志写两遍)。"""
        first = setup_logging(root=self.root)
        second = setup_logging(root=self.root / "另一个目录")
        self.assertEqual(first, second)
        self.assertEqual(len(_file_handlers()), 1)

    def test_force_reconfigures_and_switches_directory(self) -> None:
        """``force=True`` 会摘掉旧 handler 并换到新目录(测试与将来重配都靠它)。"""
        setup_logging(root=self.root / "旧")
        other = self.root / "新"
        path = setup_logging(root=other, force=True)
        self.assertEqual(len(_file_handlers()), 1)
        assert path is not None
        self.assertEqual(path.parent, other)
        get_logger("bilibili_music.tests.logging").info("新目录的日志")
        self.assertIn("新目录的日志", path.read_text(encoding="utf-8"))

    def test_unusable_directory_returns_none(self) -> None:
        """目录建不出来时返回 ``None`` 而不是抛异常。

        调用方据此在界面上提示"日志未启用"。GUI 没有 stderr,建档失败若静默,用户会
        白导出一次。
        """
        blocked = self.root / "其实是个文件"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("not a directory", encoding="utf-8")
        self.assertIsNone(setup_logging(root=blocked))


class TestRotation(unittest.TestCase):
    """验证按天轮转与保留份数。"""

    def setUp(self) -> None:
        """准备临时目录并装配日志。"""
        _detach_project_handlers()
        self.root = _SCRATCH / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        path = setup_logging(root=self.root)
        assert path is not None
        self.path = path
        handler = _file_handlers()[0]
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
        self.handler = handler

    def tearDown(self) -> None:
        """关掉 handler 再删目录。"""
        _detach_project_handlers()
        shutil.rmtree(self.root, ignore_errors=True)

    def _roll(self, day_offset: int) -> None:
        """把轮转点推到 ``day_offset`` 天,写一条日志并触发轮转。

        直接调 ``doRollover`` 而不是等零点:等待真实跨天的用例在 CI 上等于永远不跑。
        """
        base = int(time.time()) // _DAY * _DAY
        self.handler.rolloverAt = base + day_offset * _DAY
        get_logger("bilibili_music.tests.logging").info("第 %d 天", day_offset)
        self.handler.doRollover()

    def test_rollover_keeps_at_most_backup_count_archives(self) -> None:
        """轮转很多次后,归档份数封顶在 :data:`LOG_KEEP_DAYS`。

        实测(CPython 3.11):``doRollover`` 先把当前文件改名成归档、再裁掉多余的,
        所以稳态是"7 个归档 + 1 个当天文件 = 8 个文件"。
        """
        for offset in range(LOG_KEEP_DAYS + 3):
            self._roll(offset)
        archives = sorted(self.root.glob(f"{LOG_FILE_NAME}.*"))
        self.assertEqual(len(archives), LOG_KEEP_DAYS)
        self.assertTrue(self.path.exists())

    def test_rollover_moves_previous_content_into_archive(self) -> None:
        """轮转把旧内容搬进归档,新文件从零开始。"""
        get_logger("bilibili_music.tests.logging").info("轮转前的内容")
        self._roll(1)
        archives = sorted(self.root.glob(f"{LOG_FILE_NAME}.*"))
        self.assertEqual(len(archives), 1)
        self.assertIn("轮转前的内容", archives[0].read_text(encoding="utf-8"))
        self.assertNotIn("轮转前的内容", self.path.read_text(encoding="utf-8"))

    def test_same_day_rollover_is_skipped(self) -> None:
        """同一天再次触发轮转会被跳过,既**不覆盖**也不丢日志。

        这条钉住的是 CPython 的行为(``doRollover`` 里的 "Already rolled over"):
        归档名按日期生成,同名已存在时直接返回,当天日志继续写进 ``bilimusic.log``。
        因此"一个文件正好一天"并不保证 —— 翻日志要按内容时间戳找。
        """
        get_logger("bilibili_music.tests.logging").info("第一次")
        self._roll(1)
        get_logger("bilibili_music.tests.logging").info("第二次")
        self._roll(1)  # 同一天
        archives = sorted(self.root.glob(f"{LOG_FILE_NAME}.*"))
        self.assertEqual(len(archives), 1)
        self.assertNotIn("第二次", archives[0].read_text(encoding="utf-8"))
        self.assertIn("第二次", self.path.read_text(encoding="utf-8"))


class TestGetLogger(unittest.TestCase):
    """验证 logger 命名空间:不在这棵树下就落不了盘。"""

    def test_project_module_name_is_used_as_is(self) -> None:
        """项目内的模块名原样使用,日志里能看到到底哪一层打的。"""
        self.assertIs(get_logger("bilibili_music.net.client"), logging.getLogger("bilibili_music.net.client"))

    def test_foreign_name_gets_embedded_into_the_namespace(self) -> None:
        """非项目命名空间的模块(脚本、``__main__``)自动挂到项目树下。"""
        self.assertIs(get_logger("__main__"), logging.getLogger(f"{LOG_ROOT_LOGGER}.__main__"))


class TestEnvironmentSummary(unittest.TestCase):
    """验证环境摘要:够定位问题,又不含身份信息。"""

    def test_contains_version_and_platform(self) -> None:
        """默认条目含版本、平台、Python 与日志保留天数。"""
        summary = environment_summary()
        self.assertEqual(summary["应用版本"], __version__)
        self.assertIn(sys.platform, summary["平台"])
        self.assertIn("Python", summary["Python"])
        self.assertIn(str(LOG_KEEP_DAYS), summary["日志保留"])

    def test_reports_injected_log_directory(self) -> None:
        """日志目录取注入值,而不是永远报告平台默认目录。"""
        summary = environment_summary(log_dir=Path("/tmp/某个目录"))
        self.assertEqual(summary["日志目录"], "/tmp/某个目录")

    def test_extra_entries_are_merged(self) -> None:
        """``extra`` 用来补只有界面层才知道的信息(Qt 版本)。"""
        summary = environment_summary(extra={"Qt": "6.8.3"})
        self.assertEqual(summary["Qt"], "6.8.3")

    def test_contains_no_account_identity(self) -> None:
        """摘要里不出现任何账号标识。

        面向用户收集日志时,里面不该有"这是谁"的信息 —— 排障要的是版本与平台。
        """
        summary = environment_summary()
        self.assertNotIn("uname", summary)
        self.assertNotIn("mid", summary)
        self.assertFalse([key for key in summary if key.lower() in {"uid", "user", "cookie"}])


class TestExportLogs(unittest.TestCase):
    """验证导出包:只收日志文件、附环境摘要、不碰别的东西。"""

    def setUp(self) -> None:
        """准备一块专属临时目录,并放好"假日志"与"不该被打包的文件"。"""
        self.root = _SCRATCH / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.logs = self.root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        (self.logs / LOG_FILE_NAME).write_text("当前日志\n", encoding="utf-8")
        (self.logs / f"{LOG_FILE_NAME}.2026-09-14").write_text("昨天的日志\n", encoding="utf-8")
        # 这两份文件**不该**出现在包里。它们平时住在配置目录(与日志目录物理隔离),
        # 但万一有人把日志目录改到了配置目录下,白名单就是最后一道保险
        (self.logs / "session.json").write_text('{"cookies": {}}', encoding="utf-8")
        (self.logs / "config.json").write_text("{}", encoding="utf-8")
        self.target = self.root / "out" / "BiliMusic-logs.zip"

    def tearDown(self) -> None:
        """清掉临时目录。"""
        shutil.rmtree(self.root, ignore_errors=True)

    def test_archive_contains_logs_and_environment(self) -> None:
        """包里是日志文件 + ``environment.txt``。"""
        path = export_logs(self.target, log_dir=self.logs)
        self.assertEqual(path, self.target)
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
        self.assertEqual(
            names, sorted([LOG_FILE_NAME, f"{LOG_FILE_NAME}.2026-09-14", ENVIRONMENT_FILE_NAME])
        )

    def test_archive_never_contains_credentials(self) -> None:
        """凭据与配置不会被顺手打包进去(白名单是最后一道防线)。"""
        path = export_logs(self.target, log_dir=self.logs)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        self.assertNotIn("session.json", names)
        self.assertNotIn("config.json", names)

    def test_environment_file_holds_the_summary(self) -> None:
        """``environment.txt`` 的内容就是传进去的那份环境摘要。"""
        path = export_logs(
            self.target, log_dir=self.logs, summary={"应用版本": "9.9.9", "Qt": "6.8.3"}
        )
        with zipfile.ZipFile(path) as archive:
            text = archive.read(ENVIRONMENT_FILE_NAME).decode("utf-8")
        self.assertIn("应用版本=9.9.9", text)
        self.assertIn("Qt=6.8.3", text)

    def test_missing_target_directory_is_created(self) -> None:
        """目标父目录不存在时自建(用户可能直接写成 ``~/桌面/新文件夹/x.zip``)。"""
        path = export_logs(self.target, log_dir=self.logs)
        self.assertTrue(path.exists())

    def test_empty_log_directory_still_produces_a_valid_archive(self) -> None:
        """日志目录里一个日志都没时也能导出一个合法压缩包。

        日志还没生成(或刚被清掉)时导出不该让用户看到一个坏文件。
        """
        empty = self.root / "空目录"
        empty.mkdir(parents=True, exist_ok=True)
        path = export_logs(self.target, log_dir=empty)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), [ENVIRONMENT_FILE_NAME])


if __name__ == "__main__":
    unittest.main()
