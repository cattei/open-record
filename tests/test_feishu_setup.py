"""
飞书一键创建：校验逻辑与配置回填的回归测试
==========================================
这些测试不依赖真实飞书账号，也不发真实网络请求（除一处显式联网用例被跳过）。
重点覆盖三件容易出错的事：
  1. 空值/畸形 App ID 必须在本地说清楚，而不是发请求等 8 秒
  2. 错误码要翻译成人话，用户不该拿着 10003 去搜
  3. 回填配置时 enabled 的判定必须和 gui.on_save 一致，不能二次漂移

运行：runtime\\python\\python.exe -m pytest tests/test_feishu_setup.py -q
或   runtime\\python\\python.exe tests/test_feishu_setup.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import feishu_setup as fs  # noqa: E402


class TestAppIdShape(unittest.TestCase):
    def test_accepts_typical_id(self):
        self.assertTrue(fs.looks_like_app_id("cli_a1b2c3d4e5f6g7h8"))

    def test_rejects_non_cli(self):
        self.assertFalse(fs.looks_like_app_id("app_123"))
        self.assertFalse(fs.looks_like_app_id("cli_short"))
        self.assertFalse(fs.looks_like_app_id(""))

    def test_strips_whitespace(self):
        # 用户从网页复制常带上首尾空白，不应因此判失败
        self.assertTrue(fs.looks_like_app_id("  cli_a1b2c3d4e5f6g7h8  "))


class TestVerifyLocalShortCircuit(unittest.TestCase):
    """本地能判定的情况绝不应该发网络请求。"""

    def test_empty_both(self):
        r = fs.verify_credentials("", "")
        self.assertFalse(r.ok)
        self.assertIn("都要填写", r.error)

    def test_empty_secret_only(self):
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "")
        self.assertFalse(r.ok)
        self.assertIn("都要填写", r.error)

    def test_bad_shape_mentions_cli_prefix(self):
        r = fs.verify_credentials("abc", "secret")
        self.assertFalse(r.ok)
        self.assertIn("cli_", r.error)


class TestErrorTranslation(unittest.TestCase):
    """错误码 -> 人话。用打桩替换网络层，保证测试离线可跑。"""

    def _stub(self, body, bot=None, chats=None):
        """同时替换 POST 与 GET。

        新增的机器人能力/会话列表校验都走 GET，只打桩 POST 会让测试
        带着假 token 去请求真实飞书接口（既慢又不可复现）。
        bot/chats 默认给「正常」返回值，需要异常场景时显式传入。
        """
        orig_post = fs._http_post_json
        orig_get = fs._http_get_error
        fs._http_post_json = lambda url, payload, timeout=8.0: body
        fs._http_get_error = lambda url, token, timeout=8.0: (
            bot if ("bot/v3/info" in url and bot is not None)
            else chats if chats is not None
            else {"code": 0, "data": {"items": [{"chat_id": "oc_fake"}]}}
        )
        self.addCleanup(lambda: setattr(fs, "_http_post_json", orig_post))
        self.addCleanup(lambda: setattr(fs, "_http_get_error", orig_get))

    def test_ok_code(self):
        self._stub({"code": 0, "tenant_access_token": "t-fake"})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertTrue(r.ok)
        self.assertEqual(r.error, "")

    def test_bad_credential_hint(self):
        self._stub({"code": 10003, "msg": "invalid param"})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertFalse(r.ok)
        self.assertIn("10003", r.error)
        self.assertIn("不匹配", r.error)

    def test_disabled_app_hint(self):
        self._stub({"code": 10004, "msg": "app disabled"})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertFalse(r.ok)
        self.assertIn("停用", r.error)


class TestReceivabilityCheck(unittest.TestCase):
    """凭据有效 ≠ 能收到消息。

    真实事故：界面显示「飞书已启用」，但在飞书里发消息完全没反应。
    原因是旧版校验只调 tenant_access_token —— 那只证明 App ID/Secret 对，
    不证明 im:message 权限开了、也不证明事件订阅配成了长连接。
    所以这里要求校验额外查「机器人能力」与「会话列表」，把这类问题
    在启用之前就提示出来。
    """

    def _stub(self, body, bot=None, chats=None):
        orig_post = fs._http_post_json
        orig_get = fs._http_get_error
        fs._http_post_json = lambda url, payload, timeout=8.0: body
        fs._http_get_error = lambda url, token, timeout=8.0: (
            bot if ("bot/v3/info" in url and bot is not None)
            else chats if chats is not None
            else {"code": 0, "data": {"items": [{"chat_id": "oc_fake"}]}}
        )
        self.addCleanup(lambda: setattr(fs, "_http_post_json", orig_post))
        self.addCleanup(lambda: setattr(fs, "_http_get_error", orig_get))

    def test_healthy_app_has_no_warnings(self):
        self._stub({"code": 0, "tenant_access_token": "t-fake"},
                   bot={"code": 0, "bot": {"app_name": "GT-Record"}},
                   chats={"code": 0, "data": {"items": [{"chat_id": "oc_x"}]}})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertTrue(r.ok)
        self.assertEqual(r.warnings, [])
        self.assertEqual(r.app_name, "GT-Record")

    def test_empty_chat_list_is_normal(self):
        """空会话列表是正常状态，绝不能报警。

        真实误报：im/v1/chats 只返回**群聊**，不返回单聊。绝大多数用户
        是跟机器人单聊，items 必然为空。第一版把空列表当故障提示，
        对完全正常的配置（实测 GT-Record）误报了「还没有加入任何会话」。
        能成功返回本身就是 im:message 权限已开的证据。
        """
        self._stub({"code": 0, "tenant_access_token": "t-fake"},
                   chats={"code": 0, "data": {"items": []}})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertTrue(r.ok)
        self.assertEqual(r.warnings, [])

    def test_permission_denied_warns_about_scope(self):
        """权限没开时凭据依然有效——这正是旧版漏掉的情况。"""
        self._stub({"code": 0, "tenant_access_token": "t-fake"},
                   chats={"code": 99991672, "msg": "no permission"})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertTrue(r.ok)                       # 仍然允许启用
        joined = " ".join(r.warnings)
        self.assertIn("im:message", joined)
        self.assertIn("发布", joined)               # 提醒必须发版本

    def test_bot_capability_missing_warns(self):
        self._stub({"code": 0, "tenant_access_token": "t-fake"},
                   bot={"code": 10021, "msg": "bot not enabled"})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertTrue(r.ok)
        joined = " ".join(r.warnings)
        self.assertIn("机器人", joined)

    def test_credential_failure_short_circuits(self):
        """凭据就错的时候不该再去查权限，直接失败返回。"""
        self._stub({"code": 10003, "msg": "invalid param"})
        r = fs.verify_credentials("cli_a1b2c3d4e5f6g7h8", "s")
        self.assertFalse(r.ok)
        self.assertEqual(r.warnings, [])


class TestConfigApply(unittest.TestCase):
    """enabled 的语义必须和 gui.on_save 一致：两个字段都非空才算启用。"""

    def test_both_filled_enables(self):
        cfg = {}
        fs.apply_to_config(cfg, " cli_a1b2c3d4e5f6g7h8 ", " secret ")
        self.assertEqual(cfg["feishu"]["app_id"], "cli_a1b2c3d4e5f6g7h8")
        self.assertEqual(cfg["feishu"]["app_secret"], "secret")
        self.assertTrue(cfg["feishu"]["enabled"])

    def test_missing_secret_disables(self):
        cfg = {"feishu": {"enabled": True, "app_id": "old", "app_secret": "old"}}
        fs.apply_to_config(cfg, "cli_a1b2c3d4e5f6g7h8", "")
        self.assertFalse(cfg["feishu"]["enabled"])

    def test_preserves_other_keys(self):
        cfg = {"feishu": {"enabled": False, "app_id": "", "app_secret": "", "x": 1}}
        fs.apply_to_config(cfg, "cli_a1b2c3d4e5f6g7h8", "s")
        self.assertEqual(cfg["feishu"]["x"], 1)


class TestGuideText(unittest.TestCase):
    def test_lists_required_scopes(self):
        txt = fs.guide_text("录音助手")
        for code, _desc in fs.REQUIRED_SCOPES:
            self.assertIn(code, txt)

    def test_is_honest_about_no_api(self):
        # 文案不能暗示「点一下就自动建好应用」
        txt = fs.guide_text("录音助手")
        self.assertIn("没有开放", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
