"""应用配置的单元测试:默认值、坏文件兜底、原子写与未知键兼容。

不触网、不碰 Qt。落盘类用例把配置文件指向 ``tests/_scratch/`` 下的自建目录,
**不用** ``tempfile.TemporaryDirectory()`` —— 在 DSH 沙箱里它的清理会走
``tempfile._resetperms`` 的 ``chmod`` 而被拒绝(``WinError 5``),普通
``mkdir`` + ``shutil.rmtree`` 实测可用(见 ``AGENTS.md`` 第 7.1 节)。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.config import (  # noqa: E402
    AppConfig,
    CONFIG_FILE_NAME,
    ConfigStore,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_VOLUME,
    MAX_HISTORY_LIMIT,
    config_root,
)
from bilibili_music.core.queue import PlayMode  # noqa: E402

#: 落盘类用例的临时目录根;每个用例用自己名字的子目录,互不干扰。
_SCRATCH_ROOT = Path(__file__).resolve().parent / "_scratch"


class _ScratchCase(unittest.TestCase):
    """给需要落盘的用例准备一个干净目录。

    基类本身没有 ``test_*`` 方法,所以不会被收集成用例。
    """

    def setUp(self) -> None:
        """建一个空的临时目录,并把 ``self.tmp`` 指向它。"""
        self.tmp = _SCRATCH_ROOT / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """删掉本用例的目录。

        清理失败**不让用例变红**:清理不是被测行为,而且真删不掉时让一堆用例
        集体失败只会掩盖真正的问题。
        """
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAppConfig(unittest.TestCase):
    """配置数据的默认值与合法性收敛(纯内存,不碰文件系统)。"""

    def test_defaults_describe_a_fresh_install(self) -> None:
        """默认值必须是"用户从没动过设置"时的行为,而不是某种半初始化状态。"""
        config = AppConfig()
        self.assertEqual(config.volume, DEFAULT_VOLUME)
        self.assertIs(config.play_mode, PlayMode.SEQUENCE)
        self.assertEqual(config.last_bvid, "")
        self.assertEqual(config.last_cid, 0)
        self.assertEqual(config.last_position_ms, 0)
        self.assertEqual(config.history_limit, DEFAULT_HISTORY_LIMIT)

    def test_normalized_clamps_volume(self) -> None:
        """音量越界要被夹到 0 ~ 100,否则会把越界值直接喂给音量控件。"""
        self.assertEqual(AppConfig(volume=999).normalized().volume, 100)
        self.assertEqual(AppConfig(volume=-5).normalized().volume, 0)

    def test_normalized_recovers_from_non_numeric_volume(self) -> None:
        """音量被手改成非数字时退回默认值,而不是让加载流程抛异常。"""
        self.assertEqual(AppConfig(volume="abc").normalized().volume, DEFAULT_VOLUME)
        self.assertEqual(AppConfig(volume=None).normalized().volume, DEFAULT_VOLUME)

    def test_normalized_accepts_numeric_string_volume(self) -> None:
        """手写配置里常见 "80" 这种带引号的数字,应当照常识别。"""
        self.assertEqual(AppConfig(volume="60").normalized().volume, 60)

    def test_normalized_accepts_known_mode_string(self) -> None:
        """合法模式名要被转成 PlayMode 枚举,而不是留成裸字符串。"""
        self.assertIs(AppConfig(play_mode="shuffle").normalized().play_mode, PlayMode.SHUFFLE)

    def test_normalized_recovers_from_unknown_mode(self) -> None:
        """未知模式退回顺序播放;直接抛异常会让应用起不来。"""
        self.assertIs(AppConfig(play_mode="bogus").normalized().play_mode, PlayMode.SEQUENCE)
        self.assertIs(AppConfig(play_mode=123).normalized().play_mode, PlayMode.SEQUENCE)

    def test_normalized_clamps_negative_position(self) -> None:
        """负数的续播位置要归零,否则会拿着非法值去 seek。"""
        config = AppConfig(last_cid=-3, last_position_ms=-1000).normalized()
        self.assertEqual(config.last_cid, 0)
        self.assertEqual(config.last_position_ms, 0)

    def test_normalized_keeps_a_sane_history_limit(self) -> None:
        """手改过的条数上限:正数原样保留(上限内)。"""
        self.assertEqual(AppConfig(history_limit=50).normalized().history_limit, 50)
        self.assertEqual(AppConfig(history_limit="50").normalized().history_limit, 50)

    def test_normalized_recovers_from_bad_history_limit(self) -> None:
        """非正数与非数字都退回默认值。

        不回退成"0 条历史":上限本来是用户为了"别把库撑大"而设的,填 0 更可能是改错了,
        而"一条记录都不留"在界面上看起来就是个 bug。
        """
        for value in (0, -1, "abc", None):
            with self.subTest(value=value):
                self.assertEqual(
                    AppConfig(history_limit=value).normalized().history_limit,
                    DEFAULT_HISTORY_LIMIT,
                )

    def test_normalized_caps_the_history_limit(self) -> None:
        """超大值要封顶:否则每播一首都让 sqlite 去数十百万行。"""
        self.assertEqual(
            AppConfig(history_limit=10_000_000).normalized().history_limit,
            MAX_HISTORY_LIMIT,
        )

    def test_normalized_does_not_mutate_original(self) -> None:
        """收敛必须返回新对象:就地改会让"原始输入"和"合法化结果"无法对照。"""
        original = AppConfig(volume=999, play_mode="bogus")
        original.normalized()
        self.assertEqual(original.volume, 999)
        self.assertEqual(original.play_mode, "bogus")

    def test_hidden_folders_default_to_empty(self) -> None:
        """默认一个收藏夹都不隐藏 —— 全新安装不该凭空藏掉用户的歌单。"""
        self.assertEqual(AppConfig().fav_hidden_ids, [])

    def test_normalized_cleans_hidden_folders(self) -> None:
        """手改过的隐藏列表要去重、升序,并逐项丢掉非法值。

        ``media_id`` 从 1 开始(``0`` 是界面里"还没选收藏夹"的哨兵值),所以 ``0`` / 负数
        都不该被当成隐藏项。
        """
        cleaned = AppConfig(fav_hidden_ids=[3, "5", 3, None, -1, 0, "abc", 2]).normalized()
        self.assertEqual(cleaned.fav_hidden_ids, [2, 3, 5])

    def test_normalized_recovers_from_non_list_hidden_folders(self) -> None:
        """整个键被改成非数组时退回空列表,而不是让加载流程抛异常。"""
        for value in ("169038169", 42, None, {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(AppConfig(fav_hidden_ids=value).normalized().fav_hidden_ids, [])

    def test_normalized_returns_a_fresh_hidden_list(self) -> None:
        """归一化要给出新列表:共享同一个列表会让"改结果"顺手把原对象也改了。"""
        original = AppConfig(fav_hidden_ids=[3])
        normalized = original.normalized()
        normalized.fav_hidden_ids.append(9)
        self.assertEqual(original.fav_hidden_ids, [3])

    def test_normalized_is_idempotent(self) -> None:
        """已合法的配置再收敛一次不应发生变化(保存路径会重复调用它)。"""
        once = AppConfig(volume=999, play_mode="shuffle").normalized()
        twice = once.normalized()
        self.assertEqual(once, twice)


class TestConfigStore(_ScratchCase):
    """配置文件的读写、坏文件兜底与原子落盘。"""

    def _store(self, name: str = CONFIG_FILE_NAME) -> ConfigStore:
        """在本用例的临时目录下建一个 store。"""
        return ConfigStore(self.tmp / name)

    def test_missing_file_yields_defaults(self) -> None:
        """首次启动没有配置文件,必须静默用默认值,而不是报错。"""
        store = self._store()
        self.assertFalse(store.path.exists())
        self.assertEqual(store.load(), AppConfig())

    def test_save_then_load_round_trip(self) -> None:
        """存下去再读回来必须完全一致,包括枚举字段的类型。"""
        store = self._store()
        saved = AppConfig(
            volume=55,
            play_mode=PlayMode.REPEAT_ONE,
            last_bvid="BV1xx411c7mD",
            last_cid=998877,
            last_position_ms=42000,
            history_limit=88,
        )
        store.save(saved)
        loaded = store.load()
        self.assertEqual(loaded, saved)
        self.assertIsInstance(loaded.play_mode, PlayMode)

    def test_history_limit_is_handeditable_in_the_config_file(self) -> None:
        """「最近播放」的上限要真的落进 ``config.json`` 并可手改(没有界面入口)。"""
        store = self._store()
        store.save(AppConfig(history_limit=88))
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["history_limit"], 88)
        self.assertEqual(store.load().history_limit, 88)

    def test_hidden_folders_survive_a_round_trip(self) -> None:
        """手动隐藏的收藏夹要能落盘并原样读回来。

        它是纯本机偏好(见 ``AppConfig.fav_hidden_ids``):丢了用户就得每次启动重新勾一遍,
        而这恰恰是"隐藏"这种设置最招人烦的失败方式。
        """
        store = self._store()
        store.save(AppConfig(fav_hidden_ids=[3756, 169038169]))
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["fav_hidden_ids"], [3756, 169038169])
        self.assertEqual(store.load().fav_hidden_ids, [3756, 169038169])

    def test_save_returns_written_path(self) -> None:
        """保存要返回真正落盘的路径,方便调用方记日志或断言。"""
        store = self._store()
        self.assertEqual(store.save(AppConfig()), store.path)

    def test_save_creates_parent_directories(self) -> None:
        """目录不存在时要自动建出来,否则首次启动就写不了配置。"""
        store = ConfigStore(self.tmp / "nested" / "deeper" / CONFIG_FILE_NAME)
        store.save(AppConfig(volume=10))
        self.assertTrue(store.path.exists())
        self.assertEqual(store.load().volume, 10)

    def test_save_leaves_no_part_file(self) -> None:
        """原子写的临时文件必须被 replace 掉,不能在用户目录里留下 .part 残骸。"""
        store = self._store()
        store.save(AppConfig())
        self.assertEqual(list(self.tmp.glob("*.part")), [])

    def test_save_normalizes_before_writing(self) -> None:
        """非法值不能落盘:否则坏数据会被固化,下次启动还得再救一次。"""
        store = self._store()
        store.save(AppConfig(volume=999, play_mode="bogus"))
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["volume"], 100)
        self.assertEqual(raw["play_mode"], PlayMode.SEQUENCE.value)

    def test_saved_json_keeps_play_mode_as_plain_string(self) -> None:
        """模式要写成普通字符串,配置文件才是人能看懂、也能手改的。"""
        store = self._store()
        store.save(AppConfig(play_mode=PlayMode.SHUFFLE))
        text = store.path.read_text(encoding="utf-8")
        self.assertIn('"play_mode": "shuffle"', text)
        self.assertNotIn("PlayMode", text)

    def test_load_recovers_from_broken_json(self) -> None:
        """写到一半被杀的半截 JSON 必须能兜住,退回默认值。"""
        store = self._store()
        store.path.write_text('{"volume": 30', encoding="utf-8")
        self.assertEqual(store.load(), AppConfig())

    def test_load_recovers_from_non_object_json(self) -> None:
        """顶层不是对象(被手改成数组/数字)时同样退回默认值。"""
        store = self._store()
        store.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(store.load(), AppConfig())

    def test_load_ignores_unknown_keys(self) -> None:
        """多出来的字段要被忽略而不是让整份配置作废(降级场景)。"""
        store = self._store()
        store.path.write_text(
            json.dumps({"volume": 50, "future_field": {"a": 1}}), encoding="utf-8"
        )
        self.assertEqual(store.load().volume, 50)

    def test_load_normalizes_invalid_values(self) -> None:
        """手改坏的值在读入时就被收敛,调用方拿到的永远合法。"""
        store = self._store()
        store.path.write_text(
            json.dumps({"volume": 999, "play_mode": "bogus", "last_cid": -1}),
            encoding="utf-8",
        )
        loaded = store.load()
        self.assertEqual(loaded.volume, 100)
        self.assertIs(loaded.play_mode, PlayMode.SEQUENCE)
        self.assertEqual(loaded.last_cid, 0)

    def test_load_tolerates_wrong_types_for_every_field(self) -> None:
        """字段类型全被改坏时也要能起得来 —— 这是"配置不可信"的极端情形。"""
        store = self._store()
        store.path.write_text(
            json.dumps(
                {
                    "volume": {"nested": 1},
                    "play_mode": ["shuffle"],
                    "last_bvid": None,
                    "last_cid": "abc",
                    "last_position_ms": [],
                }
            ),
            encoding="utf-8",
        )
        loaded = store.load()
        self.assertEqual(loaded, AppConfig())

    def test_config_path_defaults_to_platform_root(self) -> None:
        """不传路径时要落在平台配置目录下,而不是当前工作目录。"""
        store = ConfigStore()
        self.assertTrue(store.path.is_absolute())
        self.assertEqual(store.path.name, CONFIG_FILE_NAME)
        self.assertEqual(store.path.parent, config_root())


if __name__ == "__main__":
    unittest.main()
