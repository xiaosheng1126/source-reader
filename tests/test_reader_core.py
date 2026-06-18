from __future__ import annotations

import json
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

    def test_build_action_command_uses_read_action_entrypoint(self) -> None:
        command = source_reader.build_action_command(
            "extract_outline",
            "README.md",
            "md",
            "fast",
            ".source-reader/profiles/default",
        )
        self.assertIn("python3 scripts/source_reader.py read README.md", command)
        self.assertIn("--action extract_outline", command)

    def test_rewrite_actions_for_service_uses_read_remote_entrypoint(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="url",
            source_type="webpage",
            title="Example",
            actions=[
                {"id": "continue_deep_read", "command": "old"},
                {"id": "extract_code", "command": "old"},
            ],
        )
        rewritten = source_reader.rewrite_actions_for_service(result, "https://example.com/a", "127.0.0.1", 8765)
        commands = [str(action.get("command") or "") for action in rewritten.actions]
        self.assertIn("read https://example.com/a --remote --read-depth full", commands[0])
        self.assertIn("read https://example.com/a --remote --action extract_code", commands[1])

    def test_feedback_actions_use_read_feedback_entrypoint(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="url",
            source_type="webpage",
            title="Example",
            run_id="run-1",
        )
        actions = source_reader.build_next_actions(
            result,
            "https://example.com/a",
            "fast",
            ".source-reader/profiles/default",
            False,
            False,
            180000,
        )
        feedback_commands = {
            action["id"]: action.get("command")
            for action in actions
            if action["id"] in {"mark_result_good", "mark_result_bad"}
        }
        self.assertEqual(
            feedback_commands["mark_result_good"],
            "python3 scripts/source_reader.py read --feedback good --run-id run-1",
        )
        self.assertIn("read --feedback bad --run-id run-1", str(feedback_commands["mark_result_bad"]))

    def test_build_next_actions_offers_jina_for_partial_public_webpage(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="url",
            source_type="webpage",
            title="Example",
            read_quality="partial",
            strategy="html_text_extraction",
            token_policy="max_chars=6000; full_within_budget",
            content="",
            metadata={"maybe_js_rendered": True},
            errors=["Page looks like a JavaScript-rendered shell."],
        )
        actions = source_reader.build_next_actions(
            result,
            "https://example.com/a",
            "fast",
            ".source-reader/profiles/default",
            False,
            False,
            180000,
        )
        self.assertEqual(actions[0]["id"], "read_with_jina")
        self.assertEqual(actions[0]["category"], "external")
        self.assertEqual(actions[0]["adapter"], "jina_reader")


class BackendRoutingTests(unittest.TestCase):
    def test_backend_registry_orders_candidates_by_priority(self) -> None:
        from reader_core.backends import BackendRegistry, FunctionBackend, ReadContext
        from reader_core.models import ReaderOutput

        def _reader(context: ReadContext) -> ReaderOutput:
            return ReaderOutput(
                input_type="url",
                source_type="webpage",
                title=context.source,
            )

        registry = BackendRegistry()
        registry.register(FunctionBackend("late", 20, lambda _source, _mode: True, _reader))
        registry.register(FunctionBackend("early", 10, lambda _source, _mode: True, _reader))

        self.assertEqual(
            [backend.id for backend in registry.candidates("https://example.com", "fast")],
            ["early", "late"],
        )

    def test_source_reader_routes_local_file_to_local_backend(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
            tmp.write("hello backend routing")
            tmp_path = pathlib.Path(tmp.name)
        try:
            result = source_reader.classify_and_read(str(tmp_path), read_depth="preview")
            self.assertEqual(result.metadata.get("backend_id"), "local_file")
            self.assertEqual(result.strategy, "local_text_file")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_source_reader_routes_github_url_to_github_backend(self) -> None:
        candidates = source_reader.BACKEND_REGISTRY.candidates(
            "https://github.com/Panniantong/Agent-Reach",
            "fast",
        )
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0].id, "github")

    def test_github_standard_repo_read_keeps_readme_only(self) -> None:
        calls: list[str] = []
        original_request_url = source_reader.request_url

        def fake_request_url(url: str):
            calls.append(url)
            if "api.github.com" in url:
                raise AssertionError("standard read should not call GitHub API")
            if url.endswith("/README.md"):
                return b"# Tool\n\nREADME only", "text/plain", url
            raise RuntimeError("not found")

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://github.com/acme/tool",
                read_depth="standard",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.strategy, "github_repo_readme_only")
        self.assertEqual(result.metadata.get("backend_id"), "github")
        self.assertIn("README only", result.content)
        self.assertFalse(any("api.github.com" in url for url in calls))

    def test_github_full_repo_read_selects_docs_and_manifests(self) -> None:
        original_request_url = source_reader.request_url

        def fake_json(payload: object, url: str):
            return json.dumps(payload).encode("utf-8"), "application/json", url

        def fake_request_url(url: str):
            if url == "https://api.github.com/repos/acme/tool":
                return fake_json({"default_branch": "main"}, url)
            if url == "https://api.github.com/repos/acme/tool/git/trees/main?recursive=1":
                return fake_json(
                    {
                        "truncated": False,
                        "tree": [
                            {"type": "blob", "path": "README.md"},
                            {"type": "blob", "path": "docs/install.md"},
                            {"type": "blob", "path": "package.json"},
                            {"type": "blob", "path": "src/app.py"},
                        ],
                    },
                    url,
                )
            if url.endswith("/README.md"):
                return b"# Tool\n\nOverview", "text/plain", url
            if url.endswith("/docs/install.md"):
                return b"# Install\n\nUsage details", "text/plain", url
            if url.endswith("/package.json"):
                return b'{"name":"tool"}', "application/json", url
            raise RuntimeError(f"unexpected url: {url}")

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://github.com/acme/tool",
                read_depth="full",
                max_chars=6000,
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.strategy, "github_repo_selected_docs_and_manifests")
        self.assertEqual(result.metadata.get("backend_id"), "github")
        self.assertEqual(result.metadata.get("files_read"), 3)
        self.assertEqual(result.metadata.get("paths"), ["README.md", "docs/install.md", "package.json"])
        self.assertIn("## README.md", result.content)
        self.assertIn("## docs/install.md", result.content)
        self.assertIn("## package.json", result.content)
        self.assertNotIn("src/app.py", result.content)

    def test_source_reader_routes_feed_url_to_feed_backend(self) -> None:
        candidates = source_reader.BACKEND_REGISTRY.candidates(
            "https://example.com/feed.xml",
            "fast",
        )
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0].id, "feed")

    def test_feed_backend_reads_rss_items(self) -> None:
        original_request_url = source_reader.request_url

        def fake_request_url(url: str):
            body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <description>Updates</description>
    <item>
      <title>First Post</title>
      <link>https://example.com/first</link>
      <pubDate>Fri, 19 Jun 2026 00:00:00 GMT</pubDate>
      <description>First summary</description>
    </item>
  </channel>
</rss>"""
            return body, "application/rss+xml; charset=utf-8", url

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/feed.xml",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.strategy, "feed_items_summary")
        self.assertEqual(result.metadata.get("backend_id"), "feed")
        self.assertEqual(result.metadata.get("items_read"), 1)
        self.assertIn("# Example Feed", result.content)
        self.assertIn("First Post", result.content)
        self.assertIn("https://example.com/first", result.content)

    def test_feed_backend_reads_atom_link_href(self) -> None:
        original_request_url = source_reader.request_url

        def fake_request_url(url: str):
            body = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>Atom Entry</title>
    <link href="https://example.com/atom-entry" />
    <updated>2026-06-19T00:00:00Z</updated>
    <summary>Atom summary</summary>
  </entry>
</feed>"""
            return body, "application/atom+xml", url

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/atom",
                read_depth="standard",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.strategy, "feed_items_summary")
        self.assertEqual(result.metadata.get("feed_type"), "feed")
        self.assertIn("Atom Entry", result.content)
        self.assertIn("https://example.com/atom-entry", result.content)

    def test_jina_reader_action_uses_external_reader_url(self) -> None:
        calls: list[str] = []
        original_request_url = source_reader.request_url

        def fake_request_url(url: str):
            calls.append(url)
            return b"Title: Example\n\nMarkdown content", "text/plain", url

        source_reader.request_url = fake_request_url
        try:
            read_depth, focus = source_reader.action_read_depth("read_with_jina")
            result = source_reader.classify_and_read(
                "https://example.com/a",
                mode="jina",
                read_depth=read_depth,
                max_chars=6000,
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(focus, "jina")
        self.assertEqual(calls, ["https://r.jina.ai/https://example.com/a"])
        self.assertEqual(result.strategy, "jina_reader_markdown")
        self.assertEqual(result.metadata.get("backend_id"), "jina_reader")
        self.assertEqual(result.metadata.get("external_service"), "jina_reader")
        self.assertIn("Markdown content", result.content)


class WebExtractionTests(unittest.TestCase):
    def test_extract_html_prefers_article_region(self) -> None:
        html = """
<html>
  <head><title>Example</title></head>
  <body>
    <nav>Home Pricing Login Docs</nav>
    <article>
      <h1>Real Article</h1>
      <p>This is the article body with enough detail to be selected as the preferred region.</p>
      <p>It should not be polluted by the surrounding navigation links.</p>
    </article>
  </body>
</html>
"""
        title, content, metadata = source_reader.extract_html_details(html)

        self.assertEqual(title, "Example")
        self.assertIn("Real Article", content)
        self.assertNotIn("Home Pricing Login Docs", content)
        self.assertEqual(metadata.get("html_preferred_region"), "article_or_main")

    def test_read_basic_url_exposes_meta_and_json_ld(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html lang="zh-CN">
  <head>
    <title>Fallback Title</title>
    <meta property="og:title" content="OpenGraph Title">
    <meta property="og:site_name" content="Example Site">
    <meta property="og:image" content="https://example.com/cover.jpg">
    <meta property="article:modified_time" content="2026-06-20T08:00:00Z">
    <meta name="description" content="Short page description">
    <link rel="canonical" href="https://example.com/canonical">
    <script type="application/ld+json">
      {
        "@type": "Article",
        "headline": "Structured Headline",
        "datePublished": "2026-06-19",
        "author": {"name": "Andy"}
      }
    </script>
  </head>
  <body>
    <main>
      <h1>Structured Headline</h1>
      <p>This article body has enough words to trigger preferred extraction and keep the useful page content.</p>
      <p>The parser should retain this content while exposing structured metadata for callers.</p>
    </main>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", url

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/article",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.metadata.get("backend_id"), "fast_web")
        self.assertEqual(result.title, "OpenGraph Title")
        self.assertEqual(result.metadata.get("html_title"), "Fallback Title")
        self.assertEqual(result.metadata.get("html_meta_title"), "OpenGraph Title")
        self.assertEqual(result.metadata.get("html_description"), "Short page description")
        self.assertEqual(result.metadata.get("html_canonical_url"), "https://example.com/canonical")
        self.assertEqual(result.metadata.get("html_site_name"), "Example Site")
        self.assertEqual(result.metadata.get("html_preview_image"), "https://example.com/cover.jpg")
        self.assertEqual(result.metadata.get("html_language"), "zh-CN")
        self.assertEqual(result.metadata.get("html_modified_at"), "2026-06-20T08:00:00Z")
        self.assertEqual(result.metadata.get("json_ld", {}).get("headline"), "Structured Headline")
        self.assertEqual(result.published_at, "2026-06-19")
        self.assertEqual(result.author, "Andy")

    def test_read_basic_url_resolves_relative_metadata_urls(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head>
    <title>Relative URLs</title>
    <base href="https://cdn.example.com/site/articles/current/">
    <meta property="og:image" content="/assets/cover.jpg">
    <link rel="canonical" href="../canonical">
  </head>
  <body>
    <article>
      <h1>Relative URLs</h1>
      <p>This body is long enough to keep the preferred article extraction path active for the page.</p>
      <p>The metadata URL resolver should use the final response URL as the base URL.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.metadata.get("html_base_url"), "https://cdn.example.com/site/articles/current/")
        self.assertEqual(result.metadata.get("html_canonical_url"), "https://cdn.example.com/site/articles/canonical")
        self.assertEqual(result.metadata.get("html_preview_image"), "https://cdn.example.com/assets/cover.jpg")

    def test_read_basic_url_exposes_main_content_links(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Linked Article</title></head>
  <body>
    <nav><a href="/login">Login</a></nav>
    <main>
      <h1>Linked Article</h1>
      <p>This article body is long enough to keep the preferred region active for extraction.</p>
      <p>Readers can continue with the <a href="../guide">complete guide</a> or inspect
      <a href="https://example.com/reference">reference docs</a>.</p>
      <p><a href="mailto:hi@example.com">Email us</a> <a href="javascript:void(0)">Run script</a></p>
    </main>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(
            result.metadata.get("html_links"),
            [
                {"text": "complete guide", "url": "https://example.com/articles/guide"},
                {"text": "reference docs", "url": "https://example.com/reference"},
            ],
        )

    def test_read_basic_url_exposes_main_content_headings(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Heading Article</title></head>
  <body>
    <aside><h2>Navigation Heading</h2></aside>
    <main>
      <h1>Heading Article</h1>
      <p>This article body is long enough to keep the preferred region active for extraction.</p>
      <h2>Backend <a href="/routing">Routing</a></h2>
      <p>The parser should preserve headings as structured metadata for downstream agents.</p>
      <h3>Fallbacks</h3>
    </main>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(
            result.metadata.get("html_headings"),
            [
                {"level": 1, "text": "Heading Article"},
                {"level": 2, "text": "Backend Routing"},
                {"level": 3, "text": "Fallbacks"},
            ],
        )

    def test_read_basic_url_exposes_main_content_images(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Image Article</title></head>
  <body>
    <header><img src="/logo.png" alt="Site logo"></header>
    <article>
      <h1>Image Article</h1>
      <p>This article body is long enough to keep the preferred region active for extraction.</p>
      <img src="../images/diagram.png" alt="System diagram">
      <img data-src="https://cdn.example.com/photo.jpg">
      <img src="data:image/png;base64,abc" alt="Inline">
      <p>The parser should expose useful article images without fetching binary assets.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(
            result.metadata.get("html_images"),
            [
                {"url": "https://example.com/articles/images/diagram.png", "alt": "System diagram"},
                {"url": "https://cdn.example.com/photo.jpg"},
            ],
        )

    def test_read_basic_url_uses_srcset_for_main_content_images(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Srcset Article</title></head>
  <body>
    <article>
      <h1>Srcset Article</h1>
      <p>This article body is long enough to keep the preferred region active for extraction.</p>
      <img srcset="../small.jpg 640w, ../large.jpg 1280w" alt="Responsive diagram">
      <p>The parser should keep the most useful responsive image candidate for downstream agents.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(
            result.metadata.get("html_images"),
            [{"url": "https://example.com/articles/large.jpg", "alt": "Responsive diagram"}],
        )

    def test_read_basic_url_exposes_figure_captions_for_images(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Figure Article</title></head>
  <body>
    <article>
      <h1>Figure Article</h1>
      <p>This article body is long enough to keep the preferred region active for extraction.</p>
      <figure>
        <img src="../images/architecture.png" alt="Architecture diagram">
        <figcaption>Architecture overview with request routing and fallback readers.</figcaption>
      </figure>
      <p>The parser should preserve figure captions as image metadata for downstream agents.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(
            result.metadata.get("html_images"),
            [
                {
                    "url": "https://example.com/articles/images/architecture.png",
                    "alt": "Architecture diagram",
                    "caption": "Architecture overview with request routing and fallback readers.",
                }
            ],
        )

    def test_read_basic_url_exposes_main_content_tables(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Table Article</title></head>
  <body>
    <article>
      <h1>Table Article</h1>
      <p>This article body is long enough to keep the preferred region active for extraction.</p>
      <table>
        <caption>Reader backend comparison</caption>
        <tr><th>Backend</th><th>Use case</th></tr>
        <tr><td>fast_web</td><td>Static pages</td></tr>
        <tr><td>browser_web</td><td>Logged-in pages</td></tr>
      </table>
      <p>The parser should preserve compact table structure for downstream agents.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(
            result.metadata.get("html_tables"),
            [
                {
                    "caption": "Reader backend comparison",
                    "rows": [
                        ["Backend", "Use case"],
                        ["fast_web", "Static pages"],
                        ["browser_web", "Logged-in pages"],
                    ],
                }
            ],
        )

    def test_read_basic_url_ignores_tables_outside_main_content(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head><title>Navigation Table</title></head>
  <body>
    <aside>
      <table><tr><td>Navigation</td></tr></table>
    </aside>
    <main>
      <h1>Navigation Table</h1>
      <p>This body is long enough to keep the preferred region active for extraction.</p>
      <p>The parser should avoid collecting layout tables outside the preferred region.</p>
    </main>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/articles/current/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/redirect",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertNotIn("html_tables", result.metadata)

    def test_read_basic_url_uses_html_meta_author_and_published_at(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head>
    <title>Meta Article</title>
    <meta name="author" content="Meta Author">
    <meta property="article:published_time" content="2026-06-18T10:00:00Z">
  </head>
  <body>
    <article>
      <h1>Meta Article</h1>
      <p>This article body has enough words to use the preferred region and avoid unrelated chrome.</p>
      <p>The metadata fallback should populate author and publication time without JSON-LD.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", url

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/meta-article",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        self.assertEqual(result.title, "Meta Article")
        self.assertEqual(result.metadata.get("html_author"), "Meta Author")
        self.assertEqual(result.metadata.get("html_published_at"), "2026-06-18T10:00:00Z")
        self.assertEqual(result.author, "Meta Author")
        self.assertEqual(result.published_at, "2026-06-18T10:00:00Z")

    def test_read_basic_url_exposes_richer_meta_and_json_ld(self) -> None:
        original_request_url = source_reader.request_url
        html = b"""
<html>
  <head>
    <title>Rich Article</title>
    <meta name="keywords" content="AI, Reader">
    <meta property="article:tag" content="Web Extraction">
    <script type="application/ld+json">
      {
        "@type": "TechArticle",
        "headline": "Rich Article",
        "description": "Structured description",
        "datePublished": "2026-06-19",
        "dateModified": "2026-06-20",
        "author": {"name": "Andy"},
        "url": "/articles/rich",
        "image": "/images/rich.jpg",
        "articleSection": "Engineering",
        "keywords": ["AI", "Reading"]
      }
    </script>
  </head>
  <body>
    <article>
      <h1>Rich Article</h1>
      <p>This article body is long enough to activate the preferred content region for extraction.</p>
      <p>The parser should expose richer metadata without adding extra network requests.</p>
    </article>
  </body>
</html>
"""

        def fake_request_url(url: str):
            return html, "text/html; charset=utf-8", "https://example.com/docs/page"

        source_reader.request_url = fake_request_url
        try:
            result = source_reader.classify_and_read(
                "https://example.com/docs/page",
                read_depth="preview",
            )
        finally:
            source_reader.request_url = original_request_url

        json_ld = result.metadata.get("json_ld", {})
        self.assertEqual(result.metadata.get("html_tags"), ["AI", "Reader", "Web Extraction"])
        self.assertEqual(result.metadata.get("html_canonical_url"), "https://example.com/articles/rich")
        self.assertEqual(result.metadata.get("html_preview_image"), "https://example.com/images/rich.jpg")
        self.assertEqual(json_ld.get("description"), "Structured description")
        self.assertEqual(json_ld.get("modified_at"), "2026-06-20")
        self.assertEqual(json_ld.get("section"), "Engineering")
        self.assertEqual(json_ld.get("keywords"), ["AI", "Reading"])

    def test_json_ld_summary_prefers_article_node_in_graph(self) -> None:
        summary = source_reader._json_ld_summary(
            [
                """
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "WebSite",
      "name": "Example Site",
      "url": "https://example.com"
    },
    {
      "@type": "Article",
      "headline": "Actual Article",
      "datePublished": "2026-06-19",
      "author": {"name": "Andy"},
      "image": "https://example.com/article.jpg"
    }
  ]
}
"""
            ]
        )

        self.assertEqual(summary.get("type"), "Article")
        self.assertEqual(summary.get("headline"), "Actual Article")
        self.assertEqual(summary.get("published_at"), "2026-06-19")
        self.assertEqual(summary.get("author"), "Andy")


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

    def test_failure_suggestions_group_by_domain_and_error_type(self) -> None:
        failures = [
            {
                "source": "https://example.com/a",
                "domain": "example.com",
                "error_type": "js_shell",
            },
            {
                "source": "https://example.com/b",
                "domain": "example.com",
                "error_type": "js_shell",
            },
            {
                "source": "scan.pdf",
                "domain": ".pdf",
                "error_type": "pdf_no_text",
            },
        ]
        suggestions = source_reader.failure_suggestions(failures)
        self.assertEqual(suggestions[0]["domain"], "example.com")
        self.assertEqual(suggestions[0]["failure_type"], "js_shell")
        self.assertEqual(suggestions[0]["count"], 2)

    def test_classify_failure_type_detects_pdf_no_text(self) -> None:
        result = source_reader.classify_failure_type({}, ["pdf has no extractable text"], "local_pdf_no_extractable_text")
        self.assertEqual(result, "unknown")

    def test_classify_failure_type_detects_broken_yt_dlp_vendor(self) -> None:
        result = source_reader.classify_failure_type({}, ["ModuleNotFoundError: No module named 'yt_dlp'"], "video_audio_download_failed")
        self.assertEqual(result, "missing_dependency")

    def test_classify_failure_type_detects_video_anti_bot(self) -> None:
        result = source_reader.classify_failure_type({}, ["HTTP Error 403: Forbidden"], "video_audio_download_failed")
        self.assertEqual(result, "http_error")


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
        ), patch("reader_core.media.whisper_vendor_installed", return_value=False), patch(
            "reader_core.media.groq_api_key",
            return_value=None,
        ):
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
    def test_gather_status_includes_backend_capabilities(self) -> None:
        report = source_reader.gather_status(recent_limit=0)
        self.assertIn("backend_capabilities", report)
        capabilities = report["backend_capabilities"]
        self.assertIsInstance(capabilities, list)
        ids = {item.get("id") for item in capabilities if isinstance(item, dict)}
        self.assertIn("fast_web", ids)
        self.assertIn("github", ids)

    def test_status_markdown_includes_backend_capabilities_section(self) -> None:
        report = source_reader.gather_status(recent_limit=0)
        md = source_reader.status_to_markdown(report)
        self.assertIn("## Backend Capabilities", md)
        self.assertIn("fast_web", md)

    def test_doctor_includes_backend_capabilities(self) -> None:
        report = source_reader.source_reader_doctor()
        self.assertIn("backend_capabilities", report)
        md = source_reader.doctor_to_markdown(report)
        self.assertIn("## Backend Capabilities", md)

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
        self.assertIn("## Whisper (heavy optional)", md)
        self.assertIn("Installed:", md)

    def test_build_next_actions_includes_heavy_whisper_when_whisper_missing(self) -> None:
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
        self.assertIn("install_local_whisper_heavy", action_ids)


class PdfTests(unittest.TestCase):
    def test_read_local_pdf_partial_when_pypdf_missing(self) -> None:
        from unittest.mock import patch

        from reader_core.pdf import read_local_pdf

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            pathlib.Path(tmp.name).write_bytes(b"%PDF-1.4\n")
            with patch("reader_core.pdf._load_pdf_reader", return_value=None):
                result = read_local_pdf(pathlib.Path(tmp.name), 6000)
        self.assertEqual(result.source_type, "pdf")
        self.assertEqual(result.read_quality, "partial")
        self.assertEqual(result.strategy, "local_pdf_missing_pypdf")
        self.assertIn("pypdf not installed", result.errors)

    def test_read_local_pdf_extracts_text_with_pypdf(self) -> None:
        from unittest.mock import patch

        from reader_core.pdf import read_local_pdf

        class Page:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self) -> str:
                return self.text

        class FakePdfReader:
            def __init__(self, path: str) -> None:
                self.pages = [Page("First page"), Page("Second page")]

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            pathlib.Path(tmp.name).write_bytes(b"%PDF-1.4\n")
            with patch("reader_core.pdf._load_pdf_reader", return_value=FakePdfReader):
                result = read_local_pdf(pathlib.Path(tmp.name), 6000)
        self.assertEqual(result.read_quality, "basic")
        self.assertEqual(result.strategy, "local_pdf_pypdf_text_extraction")
        self.assertIn("First page", result.content)
        self.assertEqual(result.metadata["page_count"], 2)

    def test_read_local_pdf_no_extractable_text_is_partial(self) -> None:
        from unittest.mock import patch

        from reader_core.pdf import read_local_pdf

        class Page:
            def extract_text(self) -> str:
                return ""

        class FakePdfReader:
            def __init__(self, path: str) -> None:
                self.pages = [Page()]

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            pathlib.Path(tmp.name).write_bytes(b"%PDF-1.4\n")
            with patch("reader_core.pdf._load_pdf_reader", return_value=FakePdfReader):
                result = read_local_pdf(pathlib.Path(tmp.name), 6000)
        self.assertEqual(result.read_quality, "partial")
        self.assertEqual(result.strategy, "local_pdf_no_extractable_text")
        self.assertIn("pdf has no extractable text", result.errors)

    def test_gather_status_includes_pdf_key(self) -> None:
        report = source_reader.gather_status(recent_limit=0)
        self.assertIn("pdf", report)
        self.assertIn("installed", report["pdf"])

    def test_status_markdown_includes_pdf_section(self) -> None:
        report = source_reader.gather_status(recent_limit=0)
        md = source_reader.status_to_markdown(report)
        self.assertIn("## PDF", md)

    def test_build_next_actions_includes_install_pdf_reader_when_missing(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="file",
            source_type="pdf",
            title="sample",
            read_quality="partial",
            strategy="local_pdf_missing_pypdf",
            token_policy="max_chars=6000; full_within_budget",
            content="",
            errors=["pypdf not installed"],
        )
        actions = source_reader.build_next_actions(
            result,
            "sample.pdf",
            "fast",
            ".source-reader/profiles/default",
            False,
            False,
            180000,
        )
        action_ids = [action["id"] for action in actions]
        self.assertIn("install_pdf_reader", action_ids)

    def test_online_pdf_action_requires_external_upload(self) -> None:
        result = source_reader.ReaderOutput(
            input_type="file",
            source_type="pdf",
            title="scan",
            read_quality="partial",
            strategy="local_pdf_no_extractable_text",
            token_policy="max_chars=6000; full_within_budget",
            content="",
            errors=["pdf has no extractable text"],
        )
        actions = source_reader.build_next_actions(
            result,
            "scan.pdf",
            "fast",
            ".source-reader/profiles/default",
            False,
            False,
            180000,
        )
        upload_actions = [action for action in actions if action["id"] == "online_pdf_parse_explicit_upload"]
        self.assertEqual(len(upload_actions), 1)
        self.assertTrue(upload_actions[0]["requires_external_upload"])


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


class FailureTypeTests(unittest.TestCase):
    def test_auth_wall_classified(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {"blocked_by": "auth_wall"}, ["Page requires login"], "html_text_extraction"
        )
        self.assertEqual(result, "auth_wall")

    def test_js_shell_classified(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {"maybe_js_rendered": True}, [], "html_text_extraction"
        )
        self.assertEqual(result, "js_shell")

    def test_cloudflare_classified(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {"blocked_by": "cloudflare"}, ["cloudflare challenge"], "html_text_extraction"
        )
        self.assertEqual(result, "cloudflare_block")

    def test_missing_dependency_yt_dlp(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, ["yt-dlp not found"], "video_yt_dlp"
        )
        self.assertEqual(result, "missing_dependency")

    def test_missing_dependency_pypdf(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, ["pypdf not installed"], "local_pdf"
        )
        self.assertEqual(result, "missing_dependency")

    def test_http_error_classified(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, ["HTTP request failed: 403"], "html_text_extraction"
        )
        self.assertEqual(result, "http_error")

    def test_no_content_classified(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, [], "html_text_extraction"
        )
        self.assertEqual(result, "no_content")

    def test_unknown_fallback(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, ["something completely unexpected"], "html_text_extraction"
        )
        self.assertEqual(result, "unknown")

    def test_auth_wall_via_auth_assistance_reason(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {"auth_assistance_reason": "limited_logged_out_view"}, [], "html_text_extraction"
        )
        self.assertEqual(result, "auth_wall")

    def test_cloudflare_challenge_solving_failed(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, ["challenge solving failed"], "html_text_extraction"
        )
        self.assertEqual(result, "cloudflare_block")

    def test_js_shell_priority_over_no_content(self) -> None:
        from source_reader import classify_failure_type
        # maybe_js_rendered=True + no errors → should be js_shell not no_content
        result = classify_failure_type(
            {"maybe_js_rendered": True}, [], "html_text_extraction"
        )
        self.assertEqual(result, "js_shell")

    def test_auth_wall_priority_over_http_error(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {"blocked_by": "auth_wall"}, ["HTTP request failed"], "html_text_extraction"
        )
        self.assertEqual(result, "auth_wall")

    def test_url_with_403_not_misclassified(self) -> None:
        from source_reader import classify_failure_type
        result = classify_failure_type(
            {}, ["Failed fetching https://example.com/article/top-403-errors-explained"], "html_text_extraction"
        )
        self.assertNotEqual(result, "http_error")


class LogGCTests(unittest.TestCase):
    def _make_logs(self, directory: pathlib.Path, count: int) -> None:
        import os, time
        directory.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            p = directory / f"run_{i:04d}.json"
            p.write_text('{"run_id": "x", "recorded_at": "2026-01-01T00:00:00"}')
            # stagger mtime so sorting is deterministic
            os.utime(p, (i, i))

    def test_gc_count_mode_trims_to_max(self) -> None:
        import os
        from source_reader import _gc_logs
        with tempfile.TemporaryDirectory() as tmp:
            runs = pathlib.Path(tmp) / "runs"
            failures = pathlib.Path(tmp) / "failures"
            self._make_logs(runs, 10)
            self._make_logs(failures, 10)
            _gc_logs(runs, failures, {"mode": "count", "max_runs": 5, "keep_failures": False})
            self.assertEqual(len(list(runs.glob("*.json"))), 5)
            self.assertEqual(len(list(failures.glob("*.json"))), 5)

    def test_gc_keep_failures_doubles_limit(self) -> None:
        import os
        from source_reader import _gc_logs
        with tempfile.TemporaryDirectory() as tmp:
            runs = pathlib.Path(tmp) / "runs"
            failures = pathlib.Path(tmp) / "failures"
            self._make_logs(runs, 10)
            self._make_logs(failures, 20)
            _gc_logs(runs, failures, {"mode": "count", "max_runs": 5, "keep_failures": True})
            self.assertEqual(len(list(runs.glob("*.json"))), 5)
            # keep_failures doubles the limit for failures dir
            self.assertEqual(len(list(failures.glob("*.json"))), 10)

    def test_gc_days_mode_removes_old(self) -> None:
        import os, time
        from source_reader import _gc_logs
        with tempfile.TemporaryDirectory() as tmp:
            runs = pathlib.Path(tmp) / "runs"
            runs.mkdir()
            old = runs / "old.json"
            new = runs / "new.json"
            old.write_text('{"run_id":"old","recorded_at":"2020-01-01T00:00:00"}')
            new.write_text('{"run_id":"new","recorded_at":"2099-01-01T00:00:00"}')
            # set mtime: old = 100 days ago, new = now
            now = time.time()
            os.utime(old, (now - 100 * 86400, now - 100 * 86400))
            os.utime(new, (now, now))
            failures = pathlib.Path(tmp) / "failures"
            failures.mkdir()
            _gc_logs(runs, failures, {"mode": "days", "max_days": 30, "keep_failures": False})
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())


class ReadSummaryTests(unittest.TestCase):
    def _make_result(self, quality: str, strategy: str = "html", errors=None, metadata=None):
        from reader_core.models import ReaderOutput
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title="Test",
            url="https://example.com",
            read_quality=quality,
            strategy=strategy,
            token_policy="max_chars=6000; full_within_budget",
            content="hello world " * 100,
            errors=errors or [],
            metadata=metadata or {},
        )

    def test_read_summary_present_in_preview(self) -> None:
        from source_reader import attach_interaction
        result = self._make_result("good")
        result = attach_interaction(result, "https://example.com", "preview", "fast", "", False, False, 180000)
        self.assertIn("read_summary", result.preview)

    def test_read_summary_blocked_has_failure_type(self) -> None:
        from source_reader import attach_interaction
        result = self._make_result("blocked", metadata={"blocked_by": "auth_wall"})
        result = attach_interaction(result, "https://example.com", "preview", "fast", "", False, False, 180000)
        summary = result.preview["read_summary"]
        self.assertEqual(summary["failure_type"], "auth_wall")

    def test_read_summary_good_quality_no_failure_type(self) -> None:
        from source_reader import attach_interaction
        result = self._make_result("good")
        result = attach_interaction(result, "https://example.com", "preview", "fast", "", False, False, 180000)
        summary = result.preview["read_summary"]
        self.assertIsNone(summary.get("failure_type"))

    def test_read_summary_token_used_positive(self) -> None:
        from source_reader import attach_interaction
        result = self._make_result("good")
        result = attach_interaction(result, "https://example.com", "preview", "fast", "", False, False, 180000)
        summary = result.preview["read_summary"]
        self.assertGreater(summary["token_used"], 0)


class FailureTypeSummaryTests(unittest.TestCase):
    def _make_failures(self) -> list[dict]:
        return [
            {"domain": "juejin.cn", "failure_type": "auth_wall"},
            {"domain": "juejin.cn", "failure_type": "auth_wall"},
            {"domain": "juejin.cn", "failure_type": "auth_wall"},
            {"domain": "x.com",     "failure_type": "auth_wall"},
            {"domain": "x.com",     "failure_type": "auth_wall"},
            {"domain": "notion.so", "failure_type": "js_shell"},
            {"domain": "notion.so", "failure_type": "js_shell"},
            {"domain": "medium.com","failure_type": "cloudflare_block"},
        ]

    def test_failure_type_summary_counts(self) -> None:
        from source_reader import _failure_type_summary
        summary = _failure_type_summary(self._make_failures())
        self.assertEqual(summary["auth_wall"]["count"], 5)
        self.assertEqual(summary["js_shell"]["count"], 2)
        self.assertEqual(summary["cloudflare_block"]["count"], 1)

    def test_failure_type_summary_domain_breakdown(self) -> None:
        from source_reader import _failure_type_summary
        summary = _failure_type_summary(self._make_failures())
        # domains for auth_wall should include juejin.cn(3) and x.com(2)
        domains = summary.get("auth_wall", {}).get("top_domains", {})
        self.assertEqual(domains.get("juejin.cn"), 3)
        self.assertEqual(domains.get("x.com"), 2)

    def test_failure_suggestions_uses_failure_type(self) -> None:
        from source_reader import failure_suggestions
        failures = self._make_failures()
        suggestions = failure_suggestions(failures, limit=3)
        top = suggestions[0]
        self.assertIn("failure_type", top)
        self.assertEqual(top["failure_type"], "auth_wall")
        self.assertGreater(top["count"], 0)


if __name__ == "__main__":
    unittest.main()
