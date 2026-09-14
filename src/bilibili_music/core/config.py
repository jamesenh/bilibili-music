"""应用配置的读写(**纯 JSON 落盘**)。

为什么不用 ``QSettings``
-----------------------

见 ``AGENTS.md`` 第 4.1 节:它在 Windows 上默认走 ``NativeFormat``,配置会落进
注册表 —— 用户看不见、删不掉、无法备份,测试也没法像注入目录那样把它沙箱化;
而且 ``core`` 这一层禁止 import Qt。

路径解析交给 :func:`config_root`,而 :class:`ConfigStore` 只认 ``Path``。
这与 :class:`~bilibili_music.core.cache.AudioCache` 收 ``root`` 参数是同一个套路:
测试指向任意目录,不碰用户真实的配置。

配置一律当作**不可信输入**
--------------------------

文件可能被手改坏、可能是旧版本写的、可能写到一半被杀。读失败**一律退回默认值,
不抛异常** —— 配置坏了不该让应用起不来,用户会以为"程序坏了"。写入则走
"先写 ``.part`` 再 ``os.replace``":配置比缓存更不能容忍半截文件,缓存坏了顶多
重下一首,配置坏了会每次启动都退默认值,表现为"设置莫名其妙丢了"。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .queue import PlayMode

__all__ = [
    "AppConfig",
    "CONFIG_FILE_NAME",
    "ConfigStore",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_VOLUME",
    "MAX_HISTORY_LIMIT",
    "config_root",
]

#: 应用在系统配置目录下使用的子目录名。
_APP_DIR_NAME = "BiliMusic"

#: 配置文件名。
CONFIG_FILE_NAME = "config.json"

#: 默认音量(0 ~ 100 的整数,与界面滑动条同一量纲)。
#: 取 80 而不是 100:音乐区素材的响度差异极大,默认拉满会让部分视频削波,
#: 而用户很少主动往回拧(与 ``audio/player.py`` 的 ``DEFAULT_VOLUME`` 保持一致)。
DEFAULT_VOLUME = 80

#: 「最近播放」默认保留的条数。按一条记录几百字节估,200 条的库文件只有几十 KB。
DEFAULT_HISTORY_LIMIT = 200

#: 手改 ``config.json`` 时允许的最大条数。
#:
#: 封顶是为了让"每次开始播放都裁一次"的代价可控:不封顶的话,把它改成一百万就等于
#: 每播一首都让 sqlite 去数一百万行。
MAX_HISTORY_LIMIT = 2000


def config_root() -> Path:
    """返回平台相关的配置目录(不保证已存在)。

    与 :func:`~bilibili_music.core.cache.cache_root` **故意不同**:

    * Windows 用 ``%APPDATA%``(Roaming)—— 配置是用户设置,该跟着漫游;
      缓存则放 ``%LOCALAPPDATA%``,把几百 MB 音频跟着漫游毫无意义
    * macOS 用 ``~/Library/Application Support``(``~/Library/Caches`` 是放缓存的)
    * 其余按 XDG 规范用 ``$XDG_CONFIG_HOME``,未设置时退回 ``~/.config``

    Returns:
        配置目录路径;目录本身可能还不存在,由 :class:`ConfigStore` 负责创建。
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return Path(base) / _APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / _APP_DIR_NAME


def _as_int(value: object, default: int) -> int:
    """把不可信的值转成整数;转不了就用 ``default``。

    配置里的数字可能被手改成 ``"80"``、``"abc"`` 甚至 ``null``。直接 ``int()``
    会抛 ``TypeError`` / ``ValueError``,把整条加载流程炸掉 —— 而这里的目的是
    "尽量救回来",不是"报告用户乱改文件"。

    Args:
        value: 待转换的原始值。
        default: 转换失败时的兜底值。

    Returns:
        转换后的整数,或 ``default``。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _clamp_history_limit(value: object) -> int:
    """把「最近播放」的条数上限收敛到合法区间。

    非正整数(手改成 ``0`` / ``-1`` / ``"abc"``)退回默认值而不是理解成"不限制":
    上限是用户为了"别把库撑大"而设的,填 0 更可能是改错了,零条历史反而更像 bug。

    Args:
        value: 配置里的原始值。

    Returns:
        ``1 ~ MAX_HISTORY_LIMIT`` 之间的整数。
    """
    limit = _as_int(value, DEFAULT_HISTORY_LIMIT)
    if limit <= 0:
        return DEFAULT_HISTORY_LIMIT
    return min(limit, MAX_HISTORY_LIMIT)


def _clean_hidden_ids(value: object) -> list[int]:
    """把「被隐藏的收藏夹」收敛成去重、升序的正整数列表。

    配置是不可信输入:这个键可能被手改成字符串、对象、甚至混着 ``null`` 的数组。
    这里**逐项**兜底而不是整份丢弃 —— 一个坏元素不该让用户其余的隐藏设置一起失效。
    非正数直接丢掉:``media_id`` 从 1 开始(``0`` 是"还没选"的哨兵值,见
    ``MainWindow._fav_media_id``),拿它当隐藏项没有任何意义。

    去重 + 排序不只是好看:结果既会写回配置文件,也会被上层塞进 ``set`` 参与每次重绘的
    过滤判断,稳定顺序让"配置文件被改写"这件事只发生在用户真的改了勾选的时候。

    Args:
        value: 配置里的原始值,期望是整数数组。

    Returns:
        去重且升序的正整数列表;``value`` 不是数组时返回空列表。
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    cleaned: set[int] = set()
    for raw in value:
        media_id = _as_int(raw, 0)
        if media_id > 0:
            cleaned.add(media_id)
    return sorted(cleaned)


@dataclass(slots=True)
class AppConfig:
    """应用配置的**纯数据**部分。

    刻意不做任何 IO:读写的坑(原子替换、坏文件兜底、未知键)集中在
    :class:`ConfigStore`,字段本身因此可以脱离文件系统单测。

    每个字段的默认值就是"用户从没动过设置"时的行为。
    """

    volume: int = DEFAULT_VOLUME
    """音量,0 ~ 100 的整数(界面滑动条的量纲);换算成 ``QAudioOutput`` 的
    0.0 ~ 1.0 由界面负责,``core`` 不认识 Qt。"""

    history_limit: int = DEFAULT_HISTORY_LIMIT
    """「最近播放」最多保留多少条(超出后按播放时间从旧到新丢掉)。

    **没有界面入口**,想改就手改 ``config.json`` —— 这是一个"定了就不再动"的偏好,
    为它做一整套设置界面并不划算。
    """

    play_mode: PlayMode = PlayMode.SEQUENCE
    """播放模式,取值范围由 :class:`~bilibili_music.core.queue.PlayMode` 决定。"""

    last_bvid: str = ""
    """上次播放的视频 BV 号;空串表示没有记录。"""

    last_cid: int = 0
    """上次播放的**分P** cid。

    存分P而不是视频级 cid:视频级 cid 只是第 1P,多P合集会因此续播到错误的歌上。
    """

    last_position_ms: int = 0
    """上次播放到的位置(毫秒),用于续播。"""

    fav_hidden_ids: list[int] = field(default_factory=list)
    """在侧栏「我的歌单」里**被用户手动隐藏**的收藏夹 ``media_id``。

    只影响本机侧栏列不列出来,B站 上的收藏夹不会被改动。存 ``media_id`` 而不是名字:
    名字可以重复、实测还有一个空标题的夹子,只有 id 是唯一键。

    **不按账号分开存**:``media_id`` 在 B站 全局唯一,别的账号的收藏夹不可能撞上同一个
    id;反过来,按账号分表会让"换个账号登录"顺手丢掉上一个账号的隐藏设置。因此列表里
    可能留着**当前账号已经没有的** id(夹子被删了、或属于另一个账号),那只是几字节的
    死数据,不做清理 —— 清理就得先知道"这份列表属于哪个账号",那正是这里要避免的。
    """

    def normalized(self) -> AppConfig:
        """返回一份把非法值收敛到合法范围的**新**配置。

        每个字段都要过一道:音量夹到 0 ~ 100,模式不合法就退回默认值,位置为负就
        归零。收敛放在这里而不是读取时,是因为 :meth:`ConfigStore.save` 也要用它
        —— 保证非法值永远不会落盘。

        Returns:
            新的 :class:`AppConfig`;``self`` 不被修改。
        """
        try:
            mode = PlayMode(self.play_mode)
        except ValueError:
            # 只捕 ValueError 就够:枚举查找对不可哈希的入力(如列表)内部会绕一圈
            # 再归一化成 ValueError,这一点由 test_load_tolerates_wrong_types_*
            # 用 play_mode=["shuffle"] 钉住
            mode = PlayMode.SEQUENCE
        return AppConfig(
            volume=max(0, min(100, _as_int(self.volume, DEFAULT_VOLUME))),
            history_limit=_clamp_history_limit(self.history_limit),
            play_mode=mode,
            last_bvid=str(self.last_bvid or ""),
            last_cid=max(0, _as_int(self.last_cid, 0)),
            last_position_ms=max(0, _as_int(self.last_position_ms, 0)),
            # 新建一份列表而不是原样传递:配置对象在界面里是长期持有的,共享同一个
            # 列表会让"改了归一化结果"顺手改掉原对象
            fav_hidden_ids=_clean_hidden_ids(self.fav_hidden_ids),
        )


class ConfigStore:
    """配置文件的读写。

    典型用法是先读、改字段、再写::

        store = ConfigStore()
        config = store.load()
        config.volume = 60
        store.save(config)

    Args:
        path: 配置文件路径;``None`` 表示用 :func:`config_root` 下的默认位置。
    """

    def __init__(self, path: Path | None = None) -> None:
        """记录配置文件路径(不建目录、不读文件)。

        Args:
            path: 配置文件路径;``None`` 表示 ``config_root() / CONFIG_FILE_NAME``。
                传入自定义路径主要用于测试与多份配置共存。
        """
        self.path: Path = (
            Path(path) if path is not None else config_root() / CONFIG_FILE_NAME
        )

    # ------------------------------------------------------------ 读

    def load(self) -> AppConfig:
        """读取并收敛配置;任何失败都退回默认值。

        以下情况**都算"配置不可用"并静默退回默认值**,不抛异常:文件不存在
        (首次启动)、不是合法 JSON(写到一半被杀)、顶层不是对象(手改成数组)、
        字段类型不对(手改过)。此时按默认设置正常启动,比弹一个用户看不懂的
        错误更有用。

        Returns:
            已做过合法性收敛的 :class:`AppConfig`;读不到文件时是默认配置。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AppConfig()
        if not isinstance(raw, dict):
            return AppConfig()
        return self._from_mapping(raw)

    @staticmethod
    def _from_mapping(raw: dict[str, Any]) -> AppConfig:
        """把 JSON 对象映射成配置对象,**忽略不认识的键**。

        忽略未知键是为了向前兼容:旧版本读到新版本写的文件时,不该因为多出来的
        字段就把整份配置作废(那等于用户每次降级都要重设一遍)。

        Args:
            raw: 已解析的 JSON 对象。

        Returns:
            收敛过合法性的配置;未知键被丢弃。
        """
        known = {f.name for f in fields(AppConfig)}
        return AppConfig(**{key: value for key, value in raw.items() if key in known}).normalized()

    # ------------------------------------------------------------ 写

    def save(self, config: AppConfig) -> Path:
        """原子地把配置写盘。

        先写同目录下的 ``.part`` 再 ``os.replace`` —— 同一文件系统内的 ``replace``
        是原子的,"配置文件存在"因此等价于"它是一份完整配置"。临时文件与正式文件
        同目录,正是为了保住这个前提。

        Args:
            config: 要保存的配置;内部会先 :meth:`AppConfig.normalized` 再写,
                所以非法值不会落盘。

        Returns:
            实际写入的文件路径(:attr:`path`)。

        Raises:
            OSError: 目录建不出来或写盘失败(磁盘满、无权限、文件被占用)。
        """
        path = self.path
        # StrEnum 是 str 的子类,json 会把它直接写成字符串,读回来再用 PlayMode() 校验
        text = json.dumps(asdict(config.normalized()), ensure_ascii=False, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            # 失败必须清掉 .part,否则会在用户目录里留下一个永远不会被复用的残文件
            tmp.unlink(missing_ok=True)
            raise
        return path
