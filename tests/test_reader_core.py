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

    def test_missing_yt_dlp_adds_install_action(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="url",
            source_type="video",
            title="https://youtube.com/watch?v=x",
            read_quality="partial",
            strategy="video_metadata_stub_no_yt_dlp",
            token_policy="max_chars=6000; full_within_budget",
            content="",
            errors=["yt-dlp not found"],
        )
        actions = source_reader.build_next_actions(
            result,
            "https://youtube.com/watch?v=x",
            "fast",
            ".source-reader/profiles/default",
            False,
            False,
            180000,
        )
        self.assertEqual(actions[0]["id"], "install_yt_dlp")
        self.assertEqual(actions[0]["command"], "python3 scripts/install.py --install-yt-dlp")


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


class ModelsTests(unittest.TestCase):
    def test_reader_output_importable_from_models(self) -> None:
        from reader_core.models import ReaderOutput
        r = ReaderOutput(input_type="url", source_type="webpage", title="test")
        self.assertEqual(r.read_quality, "basic")
        self.assertEqual(r.confidence, 0)
        self.assertEqual(r.errors, [])

    def test_source_reader_still_exposes_reader_output(self) -> None:
        from reader_core.models import ReaderOutput as _ModelsCls
        self.assertIs(source_reader.ReaderOutput, _ModelsCls)


class UtilsTests(unittest.TestCase):
    def test_cap_text_no_clip_when_under_budget(self) -> None:
        from reader_core.utils import cap_text
        text = "hello world"
        result, clipped = cap_text(text, 100)
        self.assertEqual(result, text)
        self.assertFalse(clipped)

    def test_cap_text_clips_and_returns_true(self) -> None:
        from reader_core.utils import cap_text
        long_text = "a" * 1000
        result, clipped = cap_text(long_text, 100)
        self.assertTrue(clipped)
        self.assertIn("[... content clipped", result)
        self.assertLessEqual(len(result), 200)  # clipped text is longer than budget due to marker

    def test_token_policy_not_clipped(self) -> None:
        from reader_core.utils import token_policy
        self.assertEqual(token_policy(6000, False), "max_chars=6000; full_within_budget")

    def test_token_policy_clipped(self) -> None:
        from reader_core.utils import token_policy
        self.assertEqual(token_policy(6000, True), "max_chars=6000; clipped_head_tail")

    def test_normalize_space_collapses_whitespace(self) -> None:
        from reader_core.utils import normalize_space
        self.assertEqual(normalize_space("a  b\t\tc"), "a b c")

    def test_normalize_text_removes_extra_blank_lines(self) -> None:
        from reader_core.utils import normalize_text
        result = normalize_text("a\n\n\n\nb")
        self.assertEqual(result, "a\n\nb")


class OptionalDepsTests(unittest.TestCase):
    def test_yt_dlp_status_returns_required_keys(self) -> None:
        from reader_core.optional import yt_dlp_status

        status = yt_dlp_status()
        self.assertIn("installed", status)
        self.assertIn("source", status)
        self.assertIn("version", status)
        self.assertIn("vendor_dir", status)

    def test_whisper_status_returns_required_keys(self) -> None:
        from reader_core.optional import whisper_status

        status = whisper_status()
        self.assertIn("installed", status)
        self.assertIn("model_ready", status)
        self.assertIn("ffmpeg", status)

    def test_whisper_vendor_installed_is_bool(self) -> None:
        from reader_core.optional import whisper_vendor_installed

        self.assertIsInstance(whisper_vendor_installed(), bool)

    def test_whisper_model_path_none_when_not_downloaded(self) -> None:
        from reader_core.optional import MODELS_DIR, whisper_model_path

        if not (MODELS_DIR / "faster-whisper-medium").exists():
            self.assertIsNone(whisper_model_path())

    def test_ffmpeg_path_returns_str_or_none(self) -> None:
        from reader_core.optional import ffmpeg_path

        result = ffmpeg_path()
        self.assertTrue(result is None or isinstance(result, str))

    def test_playwright_status_returns_required_keys(self) -> None:
        from reader_core.optional import playwright_status

        status = playwright_status()
        self.assertIn("installed", status)
        self.assertIn("version", status)

    def test_scrapling_installed_is_bool(self) -> None:
        from reader_core.optional import scrapling_installed

        self.assertIsInstance(scrapling_installed(), bool)


class MediaTests(unittest.TestCase):
    def test_matches_video_host_includes_douyin_and_bilibili_subdomains(self) -> None:
        from reader_core.media import matches_video_host

        self.assertTrue(matches_video_host("v.douyin.com"))
        self.assertTrue(matches_video_host("m.douyin.com"))
        self.assertTrue(matches_video_host("m.bilibili.com"))

    def test_read_video_partial_when_no_yt_dlp(self) -> None:
        from unittest.mock import patch

        from reader_core.media import read_video

        with patch("reader_core.media.resolve_yt_dlp_command", return_value=None):
            result = read_video("https://www.youtube.com/watch?v=test", 6000)
        self.assertEqual(result.read_quality, "partial")
        self.assertEqual(result.strategy, "video_metadata_stub_no_yt_dlp")
        self.assertIn("yt-dlp not found", result.errors)

    def test_read_video_partial_when_no_subtitle_and_no_whisper(self) -> None:
        from unittest.mock import MagicMock, patch

        from reader_core.media import read_video

        mock_proc = MagicMock()
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        mock_proc.returncode = 0
        with patch("reader_core.media.resolve_yt_dlp_command", return_value=(["yt-dlp"], None, "path")), patch(
            "reader_core.media.subprocess.run",
            return_value=mock_proc,
        ), patch("reader_core.media.whisper_vendor_installed", return_value=False):
            result = read_video("https://www.youtube.com/watch?v=test", 6000)
        self.assertEqual(result.read_quality, "partial")
        self.assertIn("whisper not installed", result.errors)

    def test_vtt_to_text_strips_metadata(self) -> None:
        from reader_core.media import vtt_to_text

        vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello world

00:00:03.000 --> 00:00:05.000
Hello world

00:00:05.000 --> 00:00:07.000
Second line
"""
        result = vtt_to_text(vtt)
        self.assertNotIn("WEBVTT", result)
        self.assertNotIn("-->", result)
        self.assertIn("Hello world", result)
        self.assertIn("Second line", result)
        self.assertEqual(result.count("Hello world"), 1)


class WhisperTranscribeTests(unittest.TestCase):
    def test_exits_1_when_faster_whisper_missing(self) -> None:
        import subprocess as _sp

        result = _sp.run(
            [
                sys.executable,
                "scripts/whisper_transcribe.py",
                "--audio",
                "nonexistent.mp3",
                "--model-dir",
                "/tmp/nomodel",
                "--output",
                "/tmp/out.txt",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertNotEqual(result.returncode, 0)

    def test_exits_1_without_required_args(self) -> None:
        import subprocess as _sp

        result = _sp.run(
            [sys.executable, "scripts/whisper_transcribe.py"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertNotEqual(result.returncode, 0)


class StatusWhisperTests(unittest.TestCase):
    def test_gather_status_includes_whisper_key(self) -> None:
        report = source_reader.gather_status(recent_limit=0)
        self.assertIn("whisper", report)
        whisper = report["whisper"]
        self.assertIn("installed", whisper)
        self.assertIn("model_ready", whisper)
        self.assertIn("ffmpeg", whisper)

    def test_status_markdown_includes_whisper_section(self) -> None:
        report = source_reader.gather_status(recent_limit=0)
        md = source_reader.status_to_markdown(report)
        self.assertIn("## Whisper", md)
        self.assertIn("Installed:", md)

    def test_build_next_actions_includes_install_video_when_whisper_missing(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="url",
            source_type="video",
            title="https://www.youtube.com/watch?v=x",
            read_quality="partial",
            strategy="video_subtitle_attempt",
            token_policy="max_chars=6000; full_within_budget",
            content="",
            errors=["subtitle not found", "whisper not installed"],
        )
        actions = source_reader.build_next_actions(
            result,
            "https://www.youtube.com/watch?v=x",
            "fast",
            ".source-reader/profiles/default",
            False,
            False,
            180000,
        )
        action_ids = [action["id"] for action in actions]
        self.assertIn("install_video", action_ids)


class InstallVideoTests(unittest.TestCase):
    def test_install_video_flag_parseable(self) -> None:
        import scripts.install as install_mod

        args = install_mod.parse_args(["--install-video", "--dry-run"])
        self.assertTrue(args.install_video)
        self.assertTrue(args.dry_run)

    def test_install_video_dry_run_prints_plan(self) -> None:
        import contextlib
        import io

        import scripts.install as install_mod

        installer = install_mod.Installer(root=ROOT, force=False, dry_run=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            installer.install_video()
        output = buf.getvalue()
        self.assertIn("faster-whisper", output)
        self.assertIn("dry-run", output)

    def test_whisper_status_string_not_installed(self) -> None:
        from unittest.mock import patch

        import scripts.install as install_mod

        installer = install_mod.Installer(root=ROOT, force=False, dry_run=False)
        with patch(
            "scripts.install.Installer.whisper_status",
            return_value="not installed (run --install-video for video audio transcription)",
        ):
            status = installer.whisper_status()
        self.assertIn("--install-video", status)


if __name__ == "__main__":
    unittest.main()
