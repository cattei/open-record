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

    def _stub(self, body):
        orig = fs._http_post_json
        fs._http_post_json = lambda url, payload, timeout=8.0: body
        self.addCleanup(lambda: setattr(fs, "_http_post_json", orig))

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
