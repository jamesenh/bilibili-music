"""播放器"跟随系统默认输出设备"的单元测试。

**只测纯判断逻辑,不构造真实 ``QAudioOutput``。** 真实的输出对象需要有音频设备、还要有
人去系统菜单里切设备,这种用例放不进单元测试(``AGENTS.md`` 第 6 节:不触网、不依赖环境)。
所以"要不要迁移输出"被抽成纯函数 :func:`_should_migrate_output` 与 :func:`_device_key`,
在这里逐条断言;``PlayerController`` 只负责在信号与轮询回调里调它们。

本文件会导入 ``bilibili_music.audio.player``(它顶层就 import ``QtMultimedia``),但**不创建
任何 Qt 对象**,因此不需要 ``QApplication``,也不需要离屏平台。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.audio.player import (  # noqa: E402
    _device_key,
    _should_migrate_output,
)


# ====================================================================== 替身


class _FakeId:
    """``QAudioDevice.id()`` 返回值的替身:只提供 ``data()``。"""

    def __init__(self, raw: bytes) -> None:
        """记录原始字节。

        Args:
            raw: 假装是设备 id 的字节串。
        """
        self._raw = raw

    def data(self) -> bytes:
        """返回原始字节(与 ``QByteArray.data()`` 一致)。"""
        return self._raw


class _FakeDevice:
    """``QAudioDevice`` 的替身:只需要 ``isNull()`` 与 ``id()`` 两个方法。"""

    def __init__(self, raw: bytes | None = None, *, raises: bool = False) -> None:
        """构造一个空设备或带 id 的设备。

        Args:
            raw: 设备 id 的字节串;``None`` 表示"空设备"。
            raises: 为 ``True`` 时 ``id()`` 直接抛异常,用来验证兜底分支。
        """
        self._raw = raw
        self._raises = raises

    def isNull(self) -> bool:  # noqa: N802 —— 对齐 Qt 的命名
        """空设备:``raw`` 为 ``None``。"""
        return self._raw is None

    def id(self) -> _FakeId:
        """返回设备 id;``raises`` 为真时模拟底层读取失败。"""
        if self._raises:
            raise RuntimeError("模拟 CoreAudio 读取失败")
        return _FakeId(self._raw or b"")


# ====================================================================== 用例


class TestShouldMigrateOutput(unittest.TestCase):
    """验证"什么时候才该把输出迁到新设备"这道闸门。"""

    def test_same_device_is_no_op(self) -> None:
        """同一个设备键不算变化:否则 2 秒一次的兜底轮询会不停重建音频流。"""
        self.assertFalse(_should_migrate_output("8C-7A-AA:output", "8C-7A-AA:output"))

    def test_different_device_triggers_migration(self) -> None:
        """换了设备就该迁移 —— 这正是"播放中切输出设备导致无声"的修法。"""
        self.assertTrue(_should_migrate_output("BuiltInSpeakerDevice", "8C-7A-AA:output"))
        self.assertTrue(_should_migrate_output("8C-7A-AA:output", "BuiltInSpeakerDevice"))

    def test_empty_target_is_never_migrated(self) -> None:
        """系统没有可用输出设备时不许动:把输出指到空设备等于自断声音。

        拔掉最后一副耳机时就属于这种情况(设备列表清了,但暂时没有新默认设备)。
        """
        self.assertFalse(_should_migrate_output("8C-7A-AA:output", ""))
        self.assertFalse(_should_migrate_output("", ""))

    def test_initial_binding_without_device_is_migrated(self) -> None:
        """启动时本来就没有设备(空键),后来插上耳机 → 需要迁移。"""
        self.assertTrue(_should_migrate_output("", "BuiltInSpeakerDevice"))


class TestDeviceKey(unittest.TestCase):
    """验证设备键的归一化:拿不到设备时要给出空串,而不是抛异常或给出假键。"""

    def test_decode_device_id(self) -> None:
        """设备 id 的字节串按 utf-8 解出来,前后保持一致可比。"""
        device = _FakeDevice(b"BuiltInSpeakerDevice")
        same = _FakeDevice(b"BuiltInSpeakerDevice")
        self.assertEqual(_device_key(device), "BuiltInSpeakerDevice")
        # 两个 id 相同的**不同对象**必须被判定为同一台设备,否则轮询会反复重建音频流
        self.assertFalse(_should_migrate_output(_device_key(device), _device_key(same)))

    def test_none_device_is_empty(self) -> None:
        """``None`` 当作"没有设备"。"""
        self.assertEqual(_device_key(None), "")

    def test_null_device_is_empty(self) -> None:
        """``isNull()`` 为真的设备同样算"没有设备"。"""
        self.assertEqual(_device_key(_FakeDevice(None)), "")

    def test_undecodable_id_does_not_raise(self) -> None:
        """id 不是合法 utf-8 时也要给出可比较的键(用替代字符解),不能崩。"""
        key = _device_key(_FakeDevice(b"\xff\xfeABC"))
        self.assertTrue(key)
        self.assertEqual(key, _device_key(_FakeDevice(b"\xff\xfeABC")))

    def test_broken_id_falls_back_to_empty(self) -> None:
        """底层读 id 抛异常时退化成空串,避免把播放器整条链路带崩。"""
        self.assertEqual(_device_key(_FakeDevice(raises=True)), "")


if __name__ == "__main__":
    unittest.main()
