from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reader_core.actions import needs_auth_assistance
from reader_core.detectors import (
    detect_access_limitation,
    looks_like_cloudflare_block,
    looks_like_js_shell,
)
import source_reader


class ResultStub:
    def __init__(self, read_quality: str, metadata: dict[str, object] | None = None) -> None:
        self.read_quality = read_quality
        self.metadata = metadata or {}


class DetectorTests(unittest.TestCase):
    def test_login_path_is_auth_wall(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://example.com/private",
            "https://example.com/login",
            "Login",
            "Please login to continue",
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "auth_wall")

    def test_redirect_query_with_login_copy_is_auth_wall(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://docs.example.com/a",
            "https://docs.example.com/auth?next=/a",
            "Sign in",
            "Sign in before reading this document",
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "auth_wall")

    def test_short_same_domain_login_copy_is_auth_wall(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://intranet.example.com/doc",
            "https://intranet.example.com/doc",
            "认证",
            "需要登录后查看",
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "auth_wall")

    def test_x_logged_out_chrome_is_limited_view(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://x.com/QingQ77",
            "https://x.com/QingQ77",
            "Geek Lite (@QingQ77) / X",
            "New to X? Sign up now to get your own personalized timeline! Log in Sign up",
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "limited_logged_out_view")

    def test_x_profile_contradiction_is_limited_view(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://x.com/QingQ77",
            "https://x.com/QingQ77",
            "Geek Lite (@QingQ77) / X",
            "Geek Lite 3,884 posts @QingQ77 hasn’t posted When they do, their posts will show up here.",
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "limited_logged_out_view")

    def test_juejin_login_gate_is_auth_wall(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://juejin.cn/post/123",
            "https://juejin.cn/post/123",
            "登录掘金",
            "扫码登录 验证码登录 第三方账号登录 登录后查看完整内容",
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "auth_wall")

    def test_juejin_public_article_is_not_limited(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://juejin.cn/post/123",
            "https://juejin.cn/post/123",
            "一篇公开的掘金文章",
            (
                "登录 注册 这篇文章介绍 Flutter 架构实践，包含状态管理、路由拆分、网络层封装和错误处理。"
                "我们会从页面分层开始，说明业务模块如何拆包，公共能力如何沉淀到 shared 层，"
                "再讨论 repository、use case、view model 的协作边界。文章还会覆盖异常兜底、"
                "埋点位置、单元测试组织方式，以及多人协作时如何控制依赖方向。最后给出一次"
                "迁移过程中的问题清单，包括路由循环依赖、接口字段不稳定和页面生命周期导致的"
                "重复请求。"
                "为了让示例更接近真实公开文章，正文继续补充组件边界、缓存策略、灰度发布、"
                "崩溃回滚、性能采样、日志脱敏和团队代码评审中的检查项，保证页面不是只有"
                "导航栏和少量登录入口文案。"
            ),
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_normal_article_is_not_limited(self) -> None:
        blocked, reason = detect_access_limitation(
            "https://example.com/post",
            "https://example.com/post",
            "Post",
            "This article explains the implementation in enough detail for a normal public page.",
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_js_shell_detection(self) -> None:
        decoded = "<html><div id=\"app\"></div><script></script><script></script></html>"
        self.assertTrue(looks_like_js_shell(decoded, "Loading"))

    def test_cloudflare_detection(self) -> None:
        content = "Just a moment... Cloudflare cf-ray challenge-platform"
        self.assertTrue(looks_like_cloudflare_block(content))


class ActionPolicyTests(unittest.TestCase):
    def test_blocked_result_needs_auth_action(self) -> None:
        self.assertTrue(needs_auth_assistance(ResultStub("blocked")))

    def test_limited_view_metadata_needs_auth_action(self) -> None:
        self.assertTrue(needs_auth_assistance(ResultStub("partial", {"auth_assistance_reason": "limited_logged_out_view"})))

    def test_basic_result_does_not_need_auth_action(self) -> None:
        self.assertFalse(needs_auth_assistance(ResultStub("basic")))


class FailureLogTests(unittest.TestCase):
    def test_failed_result_writes_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = source_reader.RUNS_DIR
            original_failures_dir = source_reader.FAILURES_DIR
            source_reader.RUNS_DIR = pathlib.Path(tmp) / "runs"
            source_reader.FAILURES_DIR = pathlib.Path(tmp) / "failures"
            try:
                result = source_reader.ReaderOutput(
                    input_type="url",
                    source_type="webpage",
                    title="Example",
                    run_id="test-failure",
                    read_quality="failed",
                    confidence=5,
                    strategy="html_text_extraction",
                    token_policy="max_chars=6000; full_within_budget",
                    content="",
                    errors=["HTTP request failed"],
                )
                source_reader.persist_run_log(result, "https://example.com", {"mode": "fast"})
                failure_path = source_reader.FAILURES_DIR / "test-failure.json"
                self.assertTrue(failure_path.exists())
                self.assertIn("failure_log_path", result.metadata)
            finally:
                source_reader.RUNS_DIR = original_runs_dir
                source_reader.FAILURES_DIR = original_failures_dir


if __name__ == "__main__":
    unittest.main()
