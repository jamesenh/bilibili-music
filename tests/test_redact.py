"""日志脱敏的单元测试:URL 取值、凭据字段、请求头与 handler 层兜底。

纯逻辑,不碰 Qt、不触网、不落盘(handler 兜底那组用 ``io.StringIO`` 收日志)。

凭据字段那一组**直接遍历** :data:`CREDENTIAL_FIELD_NAMES` 生成用例:往清单里加一个
字段就会自动获得覆盖。这是把"清单"和"验证"钉在一起的办法 —— 靠人工记得补测试,
迟早会漏。
"""

from __future__ import annotations

import io
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bilibili_music.core.models import AudioTrack  # noqa: E402
from bilibili_music.core.redact import (  # noqa: E402
    CREDENTIAL_FIELD_NAMES,
    REDACTED,
    RedactionFilter,
    install_redaction,
    redact_headers,
    redact_text,
    redact_url,
)

#: 测试用的 `假凭据取值`。用一个不可能与真实凭据撞车的串,断言"它没有出现在结果里"。
_SECRET = "SECRET-VALUE-MUST-NOT-SURVIVE"

#: 一条形态真实的音频直链(带签名参数),取自本项目实际会遇到的 CDN 地址形态。
_MEDIA_URL = (
    "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/12/34/5678.m4s"
    f"?deadline=1789000000&gen=playurlv3&upsig={_SECRET}&oi=1&mid=1234567"
)


class TestRedactUrl(unittest.TestCase):
    """验证 URL 只留 host + path + 参数名,取值一律抹掉。"""

    def test_plain_url_without_query_is_unchanged(self) -> None:
        """没有 query 的地址原样保留:它没有任何可泄露的取值。"""
        url = "https://api.bilibili.com/x/web-interface/view"
        self.assertEqual(redact_url(url), url)

    def test_query_values_are_replaced_by_names(self) -> None:
        """query 取值被抹掉,参数名按升序拼在一起仍可读。"""
        cleaned = redact_url("https://api.bilibili.com/x/search?keyword=x&pn=2&ps=20")
        self.assertEqual(cleaned, f"https://api.bilibili.com/x/search?keyword,pn,ps={REDACTED}")

    def test_media_direct_link_loses_its_signature(self) -> None:
        """音频直链的签名取值不落盘,但看得出打到了哪个 CDN、路径是什么。"""
        cleaned = redact_url(_MEDIA_URL)
        self.assertNotIn(_SECRET, cleaned)
        self.assertNotIn("1789000000", cleaned)
        self.assertIn("upos-sz-mirrorcos.bilivideo.com", cleaned)
        self.assertIn("/upgcxcode/12/34/5678.m4s", cleaned)

    def test_redaction_is_idempotent(self) -> None:
        """脱敏是幂等的。

        handler 层兜底会对已经脱敏过的 URL 再过一遍,不幂等就会被二次加工成乱码。
        """
        once = redact_url(_MEDIA_URL)
        self.assertEqual(redact_url(once), once)

    def test_idempotent_through_the_full_text_pass(self) -> None:
        """经 ``redact_text`` 再过一遍也不能变样 —— 这才是真实的双重脱敏路径。

        调用点先用 :func:`redact_url` 洗一遍,handler 层再用 :func:`redact_text`
        洗一遍。只测 ``redact_url(redact_url(u))`` **测不出**这条路径上的问题:
        第一版的 URL 正则把 ``<`` 当成结束符,于是已脱敏的地址被从 ``=`` 处截断,
        ``<redacted>`` 被留在原地又追加一个 —— 日志里真的出现过 ``=<redacted><redacted>``
        (实测跑一次冒烟才看到)。
        """
        for url in (_MEDIA_URL, "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD"):
            with self.subTest(url=url):
                once = redact_url(url)
                self.assertEqual(redact_text(once), once)
                # 掺在整行日志里也一样
                line = f"请求 后端=qt 地址={once} 头名=Accept"
                self.assertEqual(redact_text(line), line)

    def test_fragment_query_is_also_redacted(self) -> None:
        """fragment 里的 query 同样抹掉取值。"""
        cleaned = redact_url("https://www.bilibili.com/video/BV1xx#p=1&t=30")
        self.assertNotIn("p=1", cleaned)
        self.assertNotIn("t=30", cleaned)

    def test_plain_anchor_fragment_is_kept(self) -> None:
        """纯锚点(没有取值)原样保留,抹掉它只是白丢信息。"""
        url = "https://www.bilibili.com/video/BV1xx#comments"
        self.assertEqual(redact_url(url), url)

    def test_duplicate_names_are_deduplicated(self) -> None:
        """重复参数名只出现一次,避免日志里出现一长串同样的名字。"""
        cleaned = redact_url("https://x.com/a?p=1&p=2&p=3")
        self.assertEqual(cleaned, f"https://x.com/a?p={REDACTED}")

    def test_empty_url_is_returned_as_is(self) -> None:
        """空串原样返回:日志里"没有 URL"是常见情况,不该变成一句占位文本。"""
        self.assertEqual(redact_url(""), "")

    def test_malformed_url_falls_back_to_placeholder(self) -> None:
        """畸形到拆不开的地址整体替换成占位串,而不是冒险原样输出。"""
        cleaned = redact_url("https://[not-an-ipv6/path?token=1")
        self.assertIn(REDACTED, cleaned)
        self.assertNotIn("token=1", cleaned)


class TestCredentialFields(unittest.TestCase):
    """逐项验证凭据字段清单:每个名字都必须被抹掉。"""

    def test_every_credential_field_is_scrubbed(self) -> None:
        """清单里每个字段名出现在 ``名字=取值`` 里时取值都被抹掉。"""
        for name in sorted(CREDENTIAL_FIELD_NAMES):
            with self.subTest(field=name):
                self.assertNotIn(_SECRET, redact_text(f"{name}={_SECRET}"))

    def test_every_credential_field_is_scrubbed_in_json(self) -> None:
        """同一批字段出现在 JSON 形态里时也要被抹掉。

        JSON 形态单独一遍是必要的:冒号前面隔着引号,``名字=取值`` 那条正则匹配不到。
        """
        for name in sorted(CREDENTIAL_FIELD_NAMES):
            with self.subTest(field=name):
                self.assertNotIn(_SECRET, redact_text(f'{{"{name}": "{_SECRET}"}}'))

    def test_every_credential_field_is_scrubbed_inside_cookie_header(self) -> None:
        """``Cookie`` 头形态(多个字段用 ``;`` 分隔)同样被逐个抹掉。"""
        for name in sorted(CREDENTIAL_FIELD_NAMES):
            with self.subTest(field=name):
                self.assertNotIn(_SECRET, redact_text(f"Cookie: {name}={_SECRET}; other=1"))

    def test_matching_is_case_insensitive(self) -> None:
        """大小写不敏感:接口与手写日志里两种写法都出现过。"""
        self.assertNotIn(_SECRET, redact_text(f"SESSDATA={_SECRET}"))
        self.assertNotIn(_SECRET, redact_text(f"sessdata={_SECRET}"))

    def test_separator_is_preserved(self) -> None:
        """冒号形态保留冒号,便于一眼看出原来写的是哪种分隔符。"""
        cleaned = redact_text(f"SESSDATA: {_SECRET}")
        self.assertEqual(cleaned, f"SESSDATA: {REDACTED}")

    def test_lookalike_field_names_still_scrubbed(self) -> None:
        """以合法字段名开头的长名字也要被抹掉。

        ``csrf_token`` 会先被 ``csrf`` 这个更短的候选匹配上,靠 ``\\b`` 才回到正确分支
        —— 顺序或边界写错就会把 ``_token`` 留在原地。
        """
        cleaned = redact_text(f"csrf_token={_SECRET}")
        self.assertNotIn(_SECRET, cleaned)
        self.assertIn("csrf_token", cleaned)

    def test_ordinary_parameters_are_kept(self) -> None:
        """反向用例:``bvid`` / ``cid`` / 搜索关键词必须**保留**。

        过度脱敏会把日志变成废纸 —— 那等于白记一场。
        """
        cleaned = redact_text("搜索 keyword=周杰伦 bvid=BV1xx411c7mD cid=12345 pn=2")
        for kept in ("周杰伦", "BV1xx411c7mD", "12345", "pn=2"):
            self.assertIn(kept, cleaned)


class TestRedactHeaders(unittest.TestCase):
    """验证请求头只保留名字与条目数。"""

    def test_cookie_header_keeps_only_the_entry_count(self) -> None:
        """``Cookie`` 头换成"有几项",取值一个字都不留。"""
        cleaned = redact_headers({"Cookie": f"SESSDATA={_SECRET}; bili_jct=abc"})
        self.assertEqual(cleaned["Cookie"], f"{REDACTED}(2 项)")
        self.assertNotIn(_SECRET, cleaned["Cookie"])

    def test_header_name_matching_is_case_insensitive(self) -> None:
        """小写 ``cookie`` 同样被识别(不同调用点大小写不一致)。"""
        cleaned = redact_headers({"cookie": f"SESSDATA={_SECRET}"})
        self.assertNotIn(_SECRET, cleaned["cookie"])

    def test_authorization_header_is_fully_replaced(self) -> None:
        """``Authorization`` 没有"条目数"的概念,整体替换。"""
        cleaned = redact_headers({"Authorization": f"Bearer {_SECRET}"})
        self.assertEqual(cleaned["Authorization"], REDACTED)

    def test_referer_is_kept(self) -> None:
        """``Referer`` 保留原值。

        它不是凭据,而且"请求有没有带 Referer"正是音频 CDN 返回 403 时的第一排查线索
        (见 README 的接口实测笔记),抹掉等于自断证据。
        """
        headers = {"Referer": "https://www.bilibili.com/", "User-Agent": "Mozilla/5.0"}
        self.assertEqual(redact_headers(headers), headers)

    def test_original_mapping_is_not_modified(self) -> None:
        """返回新字典,不改调用方传进来的那份(它可能还要用于真实请求)。"""
        headers = {"Cookie": f"SESSDATA={_SECRET}"}
        redact_headers(headers)
        self.assertEqual(headers["Cookie"], f"SESSDATA={_SECRET}")


class TestRedactText(unittest.TestCase):
    """验证任意文本的兜底脱敏。"""

    def test_urls_inside_text_are_redacted(self) -> None:
        """文本里嵌着的 URL 会被就地脱敏,其余文字保持不动。"""
        cleaned = redact_text(f"请求失败 {_MEDIA_URL} 重试中")
        self.assertNotIn(_SECRET, cleaned)
        self.assertTrue(cleaned.startswith("请求失败"))
        self.assertTrue(cleaned.endswith("重试中"))

    def test_audio_track_repr_does_not_leak_the_direct_link(self) -> None:
        """防呆用例:整个 ``AudioTrack`` 对象被 ``%s`` 打进日志时也不泄露直链。

        ``AudioTrack`` 是 ``@dataclass(slots=True)`` 且带 ``url`` 字段,所以
        ``logger.debug("tracks=%s", tracks)`` 这种**最自然**的写法会把完整签名直链
        一起打出来。这条用例就是钉住这个坑。
        """
        track = AudioTrack(
            quality_id=30280,
            codec="mp4a.40.2",
            bandwidth=192000,
            url=_MEDIA_URL,
        )
        cleaned = redact_text(repr([track]))
        self.assertNotIn(_SECRET, cleaned)
        self.assertIn("upos-sz-mirrorcos.bilivideo.com", cleaned)

    def test_empty_text_is_returned_as_is(self) -> None:
        """空串直接返回,不引入额外开销。"""
        self.assertEqual(redact_text(""), "")


class TestRedactionFilter(unittest.TestCase):
    """验证 handler 层兜底:调用点忘了脱敏也不会泄露。"""

    def _capture(self) -> tuple[logging.Logger, io.StringIO]:
        """造一个只写内存、挂了脱敏过滤器的 logger。

        Returns:
            ``(logger, 输出缓冲)``;用例结束后 logger 上的 handler 会被清掉。
        """
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        install_redaction(handler)
        logger = logging.getLogger("bilibili_music.tests.redact")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.addCleanup(logger.handlers.clear)
        return logger, stream

    def test_credential_in_plain_message_is_scrubbed(self) -> None:
        """消息里的凭据被抹掉 —— 这正是"调用点忘了脱敏"的典型场景。"""
        logger, stream = self._capture()
        logger.warning("登录态生效,SESSDATA=%s", _SECRET)
        self.assertNotIn(_SECRET, stream.getvalue())
        self.assertIn(REDACTED, stream.getvalue())

    def test_signature_in_message_is_scrubbed(self) -> None:
        """消息里的直链签名被抹掉。"""
        logger, stream = self._capture()
        logger.debug("音轨地址 %s", _MEDIA_URL)
        self.assertNotIn(_SECRET, stream.getvalue())

    def test_exception_traceback_is_scrubbed(self) -> None:
        """异常栈里的凭据同样被抹掉。

        异常栈是独立于消息的一段文本,``Formatter`` 只在 ``exc_text`` 为空时自己渲染,
        所以过滤器必须主动接管它,否则消息干净了、栈里还是原文。
        """
        logger, stream = self._capture()
        try:
            raise RuntimeError(f"请求失败 SESSDATA={_SECRET}")
        except RuntimeError:
            logger.exception("出错了")
        output = stream.getvalue()
        self.assertNotIn(_SECRET, output)
        self.assertIn("RuntimeError", output)

    def test_broken_format_arguments_do_not_propagate(self) -> None:
        """占位符对不上时**不能**把异常抛回业务代码。

        过滤器跑在 ``Handler.handle`` 里,它一抛异常就会一路冒到 ``logger.info(...)``
        的调用点 —— 那等于"日志把业务炸了",是最不能接受的失败方式。
        """
        logger, stream = self._capture()
        logger.info("两个占位符 %s %s", "只给了一个")
        output = stream.getvalue()
        self.assertIn("已丢弃", output)
        self.assertNotIn(_SECRET, output)

    def test_ordinary_message_passes_through_unchanged(self) -> None:
        """普通消息原样落盘,不做多余加工。"""
        logger, stream = self._capture()
        logger.info("缓存命中 cid=12345")
        self.assertIn("缓存命中 cid=12345", stream.getvalue())

    def test_install_is_idempotent(self) -> None:
        """重复安装不会挂上第二个过滤器(否则每条日志被多洗一遍)。"""
        handler = logging.StreamHandler(io.StringIO())
        install_redaction(handler)
        install_redaction(handler)
        self.assertEqual(
            sum(1 for item in handler.filters if isinstance(item, RedactionFilter)), 1
        )


if __name__ == "__main__":
    unittest.main()
