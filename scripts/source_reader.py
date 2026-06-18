#!/usr/bin/env python3
"""
Token-aware source reader.

The reader has one job: turn a source into a compact, traceable text payload.
It deliberately prefers cheap, source-specific reads over crawling everything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html.parser
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_MAX_CHARS = 24000
READ_DEPTH_BUDGETS = {
    "preview": 6000,
    "standard": DEFAULT_MAX_CHARS,
    "full": 80000,
}
USER_AGENT = "Mozilla/5.0 source-reader/0.1"
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
RUNS_DIR = ROOT_DIR / ".source-reader" / "runs"
DEFAULT_SERVICE_HOST = "127.0.0.1"
DEFAULT_SERVICE_PORT = 8765
SERVICE_PID_PATH = ROOT_DIR / ".source-reader" / "source-reader.pid"
SERVICE_RUNTIME_PATH = ROOT_DIR / ".source-reader" / "mcp" / "source-reader.runtime.json"
FAILURES_DIR = ROOT_DIR / ".source-reader" / "failures"
PROFILE_WARN_DAYS = 14
PROFILE_CRITICAL_DAYS = 30
CREDENTIAL_WARNING = (
    ".source-reader/profiles/ 含登录态等敏感凭据，禁止提交 Git、禁止分享项目目录给他人。"
)


CONFIDENCE_UPGRADE_THRESHOLD = 40
DEFAULT_BROWSER_PROFILE = ".source-reader/profiles/default"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from reader_core.actions import needs_auth_assistance
from reader_core.detectors import (  # noqa: E402
    detect_access_limitation,
    looks_like_auth_wall,
    looks_like_cloudflare_block,
    looks_like_js_shell,
)
from reader_core.models import ReaderOutput
from reader_core.media import matches_video_host, read_video
from reader_core.optional import (
    ffmpeg_path,
    groq_status,
    playwright_installed,
    playwright_status,
    scrapling_installed,
    whisper_status,
    yt_dlp_status,
)
from reader_core.utils import cap_text, normalize_space, normalize_text, token_policy



class TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in {"article", "section", "main", "p", "li", "br", "h1", "h2", "h3", "h4", "pre", "tr"}:
            self.parts.append("\n")
        if tag == "meta":
            name = attrs_dict.get("name") or attrs_dict.get("property") or ""
            content = attrs_dict.get("content") or ""
            if name in {"og:title", "twitter:title"} and content and not self.title:
                self.title = content

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = normalize_space(data)
        if not text:
            return
        if self._in_title:
            self.title += text
        if not self._skip:
            self.parts.append(text)

    def text(self) -> str:
        return normalize_text("\n".join(self.parts))



def estimate_tokens(text: str) -> int:
    # Mixed Chinese/English docs vary a lot; this is a conservative UI hint, not billing data.
    return max(1, int(len(text) / 2.2))


def extract_headings(text: str, limit: int = 12) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        stripped = normalize_space(line)
        if not stripped:
            continue
        if re.match(r"^#{1,4}\s+\S", stripped):
            headings.append(re.sub(r"^#{1,4}\s+", "", stripped))
        elif re.match(r"^(\d+(\.\d+)*[.、)]\s*|[一二三四五六七八九十]+[、.]\s*)\S", stripped):
            headings.append(stripped)
        if len(headings) >= limit:
            break
    return headings


def extract_lead_points(text: str, limit: int = 5) -> list[str]:
    points: list[str] = []
    for part in re.split(r"\n{2,}", text):
        item = normalize_space(part)
        if len(item) < 20:
            continue
        points.append(item[:220] + ("..." if len(item) > 220 else ""))
        if len(points) >= limit:
            break
    return points


def score_confidence(result: ReaderOutput) -> int:
    """Compute 0-100 confidence for a read. Higher = the content looks usable."""
    if result.read_quality == "failed":
        return 5
    content = (result.content or "").strip()
    content_chars = len(content)
    body_length = result.metadata.get("body_length")
    if isinstance(body_length, int) and body_length > content_chars:
        content_chars = body_length

    score = 100
    if result.read_quality == "blocked":
        score -= 60
    elif result.read_quality == "partial":
        score -= 30
    if result.metadata.get("maybe_js_rendered"):
        score -= 35
    if result.metadata.get("blocked_by") == "auth_wall":
        score -= 25
    if content_chars < 200:
        score -= 40
    elif content_chars < 600:
        score -= 15
    if not extract_headings(content, limit=3):
        score -= 10
    if result.errors:
        score -= min(20, 5 * len(result.errors))
    return max(0, min(100, score))


def resolve_browser_profile(browser_profile: str) -> tuple[str, bool, bool]:
    """Return (profile_path_str, exists, used_default).

    - If caller passed a profile, use it as-is and report whether it exists.
    - If caller passed nothing, fall back to DEFAULT_BROWSER_PROFILE; only report
      'used_default' when the default directory actually exists on disk.
    """
    if browser_profile:
        path = pathlib.Path(browser_profile).expanduser()
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()
        return str(path), path.exists(), False
    default_path = (ROOT_DIR / DEFAULT_BROWSER_PROFILE).resolve()
    return str(default_path), default_path.exists(), True


def build_preview(result: ReaderOutput) -> dict[str, object]:
    content = result.content or ""
    content_chars = len(content)
    body_length = result.metadata.get("body_length")
    if isinstance(body_length, int) and body_length > content_chars:
        content_chars = body_length
    return {
        "title": result.title,
        "source_type": result.source_type,
        "read_quality": result.read_quality,
        "confidence": result.confidence,
        "strategy": result.strategy,
        "content_chars": content_chars,
        "estimated_tokens": estimate_tokens(content),
        "headings": extract_headings(content),
        "lead_points": extract_lead_points(content),
        "is_truncated": "clipped" in result.token_policy,
    }


def build_command(
    source: str,
    read_depth: str,
    fmt: str,
    mode: str,
    browser_profile: str,
    headless: bool,
    interactive_login: bool,
    login_timeout_ms: int,
) -> str:
    parts = [
        "python3",
        "scripts/source_reader.py",
        shell_quote(source),
        "--read-depth",
        read_depth,
        "--format",
        fmt,
        "--mode",
        mode,
    ]
    if browser_profile:
        parts.extend(["--browser-profile", shell_quote(browser_profile)])
    if headless:
        parts.append("--headless")
    if interactive_login:
        parts.extend(["--interactive-login", "--login-timeout-ms", str(login_timeout_ms)])
    return " ".join(parts)


def shell_quote(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+=,-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def slugify_run_part(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    value = value.strip("-")
    return value[:36] or "source"


def build_run_id(source: str) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    digest = hashlib.sha1(source.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{timestamp}-{slugify_run_part(source)}-{digest}"


def action(
    action_id: str,
    label: str,
    description: str,
    *,
    command: str = "",
    prompt: str = "",
    requires_confirmation: bool = True,
    category: str = "read",
    scope: str = "reader",
    adapter: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": action_id,
        "label": label,
        "description": description,
        "requires_confirmation": requires_confirmation,
        "category": category,
        "scope": scope,
    }
    if adapter:
        payload["adapter"] = adapter
    if command:
        payload["command"] = command
    if prompt:
        payload["prompt"] = prompt
    return payload


def build_next_actions(
    result: ReaderOutput,
    source: str,
    mode: str,
    browser_profile: str,
    headless: bool,
    interactive_login: bool,
    login_timeout_ms: int,
) -> list[dict[str, object]]:
    profile = browser_profile or ".source-reader/profiles/default"
    actions = [
        action(
            "continue_deep_read",
            "继续深读",
            "提高读取预算，适合确认这份资料值得继续分析后使用。",
            command=build_command(source, "full", "md", mode, browser_profile, headless, interactive_login, login_timeout_ms),
        ),
        action(
            "extract_outline",
            "提取大纲",
            "只围绕标题、层级、关键概念和内容地图继续整理。",
            command=build_action_command("extract_outline", source, "md", mode, profile),
        ),
        action(
            "extract_code",
            "提取代码",
            "只提取命令、配置、API、代码片段和集成步骤。",
            command=build_action_command("extract_code", source, "md", mode, profile),
        ),
        action(
            "ask_followup",
            "追问细节",
            "针对某个章节、实现、风险或决策点继续提问。",
            prompt="我想继续追问这份资料中的一个具体问题：",
            category="question",
        ),
    ]
    if result.run_id:
        actions.extend(
            [
                action(
                    "mark_result_good",
                    "结果可用",
                    "记录本次读取满足预期，用于后续复盘读取策略。",
                    command=f"python3 scripts/source_reader.py feedback mark_good --run-id {shell_quote(result.run_id)}",
                    requires_confirmation=False,
                    category="feedback",
                ),
                action(
                    "mark_result_bad",
                    "结果不对",
                    "记录本次读取不满足预期，并补充原因帮助后续改进。",
                    command=f"python3 scripts/source_reader.py feedback mark_bad --run-id {shell_quote(result.run_id)} --reason '<reason>'",
                    requires_confirmation=False,
                    category="feedback",
                ),
            ]
        )
    if any("Playwright is not installed" in error for error in result.errors):
        actions.insert(
            0,
            action(
                "install_playwright",
                "安装 browser 运行时",
                "安装 Playwright Chromium（一次性，约 300MB），之后可读取 JS 渲染或登录态页面。",
                command="python3 scripts/install.py --install-browser",
                category="setup",
            ),
        )
    if any("yt-dlp not found" in error for error in result.errors):
        actions.insert(
            0,
            action(
                "install_yt_dlp",
                "安装 yt-dlp",
                "安装项目本地 yt-dlp 到 .source-reader/vendor，用于读取 YouTube 等视频字幕，不修改系统 PATH。",
                command="python3 scripts/install.py --install-yt-dlp",
                category="setup",
            ),
        )
    if any("whisper not installed" in error or "whisper model not found" in error for error in result.errors):
        actions.insert(
            0,
            action(
                "install_video",
                "安装 Whisper 视频转写",
                "安装 faster-whisper 到 .source-reader/vendor，并下载 medium 模型（~769MB），无字幕视频可转写。",
                command="python3 scripts/install.py --install-video",
                category="setup",
            ),
        )
    if (
        result.read_quality in {"blocked", "failed"}
        and result.metadata.get("auto_upgraded")
        and not scrapling_installed()
    ):
        actions.insert(
            0,
            action(
                "install_scrapling",
                "安装 Scrapling 反爬层",
                "安装 Scrapling + Camoufox（约 200MB），可突破 Cloudflare 等反爬保护，作为 browser 模式后的最后一道。",
                command="python3 scripts/install.py --install-scrapling",
                category="setup",
            ),
        )
    if needs_auth_assistance(result):
        actions.insert(
            0,
            action(
                "login_with_browser",
                "登录后重试",
                "打开持久化浏览器 profile，手动登录或授权后继续读取。",
                command=build_command(
                    source,
                    "preview",
                    "md",
                    "browser",
                    browser_profile or ".source-reader/profiles/default",
                    False,
                    True,
                    login_timeout_ms,
                ),
                category="auth",
            ),
        )
    return actions


def build_action_command(action_id: str, source: str, fmt: str, mode: str, browser_profile: str) -> str:
    return (
        "python3 scripts/source_reader.py action "
        f"{shell_quote(action_id)} --source {shell_quote(source)} "
        f"--format {fmt} --mode {mode} --browser-profile {shell_quote(browser_profile)}"
    )


def attach_interaction(
    result: ReaderOutput,
    source: str,
    read_depth: str,
    mode: str,
    browser_profile: str,
    headless: bool,
    interactive_login: bool,
    login_timeout_ms: int,
) -> ReaderOutput:
    if not result.run_id:
        result.run_id = build_run_id(source)
    result.read_depth = read_depth
    if not result.confidence:
        result.confidence = score_confidence(result)
    result.preview = build_preview(result)
    result.actions = build_next_actions(
        result,
        source,
        mode,
        browser_profile,
        headless,
        interactive_login,
        login_timeout_ms,
    )
    result.next_actions = result.actions
    result.metadata["read_depth"] = read_depth
    return result


def command_exists(command: str) -> bool:
    proc = subprocess.run(["/usr/bin/env", "which", command], text=True, capture_output=True, check=False)
    return proc.returncode == 0


def run_check(
    command: list[str],
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (proc.stdout.strip() or proc.stderr.strip())
    return proc.returncode == 0, output


def source_reader_doctor(browser_profile: str = ".source-reader/profiles/default") -> dict[str, object]:
    profile_path = pathlib.Path(browser_profile).expanduser()
    if not profile_path.is_absolute():
        profile_path = (ROOT_DIR / profile_path).resolve()

    node_ok = command_exists("node")
    npm_ok = command_exists("npm")
    package_json = ROOT_DIR / "package.json"
    browser_reader = SCRIPT_DIR / "browser_reader.mjs"
    playwright_ok = False
    playwright_message = ""
    if node_ok:
        playwright_ok, playwright_message = run_check(
            ["node", "-e", "import('playwright').then(()=>console.log('ok')).catch(e=>{console.error(e.message);process.exit(1)})"],
            cwd=ROOT_DIR,
        )

    scrapling_ok = scrapling_installed()
    yt_dlp = yt_dlp_status()
    whisper_s = whisper_status()
    ffmpeg = ffmpeg_path()
    checks = {
        "root": str(ROOT_DIR),
        "node": node_ok,
        "npm": npm_ok,
        "package_json": package_json.exists(),
        "browser_reader": browser_reader.exists(),
        "playwright": playwright_ok,
        "yt_dlp": yt_dlp,
        "whisper_installed": whisper_s["installed"],
        "whisper_model_ready": whisper_s["model_ready"],
        "ffmpeg": ffmpeg or "not found",
        "scrapling": scrapling_ok,
        "browser_profile": profile_path.exists(),
        "browser_profile_path": str(profile_path),
    }
    recommendations: list[str] = []
    if not node_ok:
        recommendations.append("Install Node.js before using browser mode.")
    if not npm_ok:
        recommendations.append("Install npm before using browser mode.")
    if not playwright_ok:
        recommendations.append(
            "Browser mode requires Playwright. Run: python3 scripts/install.py --install-browser"
        )
    if not yt_dlp.get("installed"):
        recommendations.append(
            "Video transcript reading requires yt-dlp. Run: python3 scripts/install.py --install-yt-dlp"
        )
    if not whisper_s["installed"]:
        recommendations.append(
            "Whisper (audio transcription for videos without subtitles) is optional. "
            "Run: python3 scripts/install.py --install-video"
        )
    if not scrapling_ok:
        recommendations.append(
            "Scrapling (anti-bot tier) is optional. Run: python3 scripts/install.py --install-scrapling (~200MB, Camoufox)"
        )
    if not profile_path.exists():
        recommendations.append(f"Create browser profile directory: {profile_path}")

    return {
        "status": "ok" if all([node_ok, npm_ok, package_json.exists(), browser_reader.exists(), playwright_ok]) else "needs_setup",
        "checks": checks,
        "playwright_message": playwright_message,
        "recommendations": recommendations,
    }


def run_log_path(run_id: str) -> pathlib.Path:
    return RUNS_DIR / f"{run_id}.json"


def failure_log_path(run_id: str) -> pathlib.Path:
    return FAILURES_DIR / f"{run_id}.json"


def display_path(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def should_persist_failure_log(result: ReaderOutput) -> bool:
    return result.read_quality in {"blocked", "failed", "partial"} or bool(result.errors)


def persist_failure_log(result: ReaderOutput, source: str, invocation: dict[str, object]) -> pathlib.Path | None:
    if not should_persist_failure_log(result):
        return None
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    path = failure_log_path(result.run_id)
    result.metadata["failure_log_path"] = display_path(path)
    payload = {
        "run_id": result.run_id,
        "source": source,
        "invocation": invocation,
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "read_quality": result.read_quality,
        "confidence": result.confidence,
        "strategy": result.strategy,
        "metadata": result.metadata,
        "errors": result.errors,
        "content_excerpt": (result.content or "")[:2000],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def recent_reads_from_runs(limit: int = 10) -> list[dict[str, object]]:
    """Return the N most recent run logs as summary dicts, ordered newest first.

    Scans .source-reader/runs/*.json by mtime. This replaces the older sqlite-backed
    history table — JSON run logs are the single source of truth."""
    if not RUNS_DIR.exists() or limit <= 0:
        return []
    files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    output: list[dict[str, object]] = []
    for path in files:
        if len(output) >= limit:
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        output.append(
            {
                "run_id": payload.get("run_id"),
                "source": payload.get("source"),
                "source_type": result.get("source_type"),
                "title": result.get("title"),
                "fetched_at": result.get("fetched_at") or payload.get("recorded_at"),
                "mode": invocation.get("mode"),
                "read_depth": result.get("read_depth") or invocation.get("read_depth"),
                "confidence": int(result.get("confidence") or 0),
                "content_chars": len(str(result.get("content") or "")),
                "auto_upgraded": bool(metadata.get("auto_upgraded")),
                "errors": result.get("errors") or [],
            }
        )
    return output


def recent_failures_from_logs(limit: int = 10) -> list[dict[str, object]]:
    if not FAILURES_DIR.exists() or limit <= 0:
        return []
    files = sorted(FAILURES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    output: list[dict[str, object]] = []
    for path in files:
        if len(output) >= limit:
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        output.append(
            {
                "run_id": payload.get("run_id"),
                "source": payload.get("source"),
                "read_quality": payload.get("read_quality"),
                "confidence": int(payload.get("confidence") or 0),
                "strategy": payload.get("strategy"),
                "blocked_by": metadata.get("blocked_by"),
                "auth_assistance_reason": metadata.get("auth_assistance_reason"),
                "errors": payload.get("errors") or [],
                "failure_log_path": display_path(path),
            }
        )
    return output


def persist_run_log(result: ReaderOutput, source: str, invocation: dict[str, object]) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = run_log_path(result.run_id)
    persist_failure_log(result, source, invocation)
    result.metadata["run_log_path"] = display_path(path)
    payload = {
        "run_id": result.run_id,
        "source": source,
        "invocation": invocation,
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "result": result.to_dict(),
        "feedback": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_run_log(run_id: str) -> tuple[pathlib.Path, dict[str, object]]:
    path = run_log_path(run_id)
    if not path.exists():
        raise SystemExit(f"run log not found: {path.relative_to(ROOT_DIR)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"run log is not valid json: {path.relative_to(ROOT_DIR)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"run log has invalid shape: {path.relative_to(ROOT_DIR)}")
    return path, payload


def record_feedback(run_id: str, verdict: str, reason: str = "", expected: str = "") -> pathlib.Path:
    path, payload = load_run_log(run_id)
    feedback = payload.get("feedback")
    if not isinstance(feedback, list):
        feedback = []
        payload["feedback"] = feedback
    feedback.append(
        {
            "verdict": verdict,
            "reason": reason,
            "expected": expected,
            "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summarize_recent_runs(limit: int = 20) -> dict[str, object]:
    if not RUNS_DIR.exists():
        return {"status": "empty", "runs": [], "suggestions": ["还没有 run log。"]}
    paths = sorted(RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
    rows: list[dict[str, object]] = []
    failure_by_domain: dict[str, int] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        result = payload.get("result") if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            continue
        source = str(payload.get("source") or "")
        parsed = urllib.parse.urlparse(source)
        domain = parsed.netloc or "local_file"
        read_quality = str(result.get("read_quality") or "")
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        rows.append(
            {
                "run_id": payload.get("run_id"),
                "source": source,
                "domain": domain,
                "source_type": result.get("source_type"),
                "read_quality": read_quality,
                "strategy": result.get("strategy"),
                "feedback_count": len(payload.get("feedback") or []),
                "errors": errors[:3],
            }
        )
        if read_quality in {"blocked", "failed", "partial"}:
            failure_by_domain[domain] = failure_by_domain.get(domain, 0) + 1
    suggestions = [
        f"{domain}: 最近 blocked/failed/partial 读取 {count} 次，建议评估是否增加域名规则或 browser-first profile。"
        for domain, count in sorted(failure_by_domain.items(), key=lambda item: item[1], reverse=True)
    ]
    return {"status": "ok", "runs": rows, "suggestions": suggestions or ["最近没有明显的重复失败模式。"]}


def _dir_size(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _humanize_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _humanize_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = divmod(seconds, 3600)
        return f"{h}h {m // 60}m"
    days, rem = divmod(seconds, 86400)
    return f"{days}d {rem // 3600}h"


def _short_source(source: str, limit: int = 60) -> str:
    source = source or ""
    if len(source) <= limit:
        return source
    return source[: limit - 3] + "..."


def _service_port() -> int:
    if SERVICE_RUNTIME_PATH.exists():
        try:
            data = json.loads(SERVICE_RUNTIME_PATH.read_text(encoding="utf-8"))
            port = (data.get("service") or {}).get("port")
            if port:
                return int(port)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return DEFAULT_SERVICE_PORT


def service_status() -> dict[str, object]:
    port = _service_port()
    out: dict[str, object] = {
        "running": False,
        "pid": None,
        "port": port,
        "uptime_seconds": None,
        "health_ok": False,
        "pid_file": str(SERVICE_PID_PATH.relative_to(ROOT_DIR)),
        "stale_pid": False,
    }
    if not SERVICE_PID_PATH.exists():
        return out
    try:
        pid = int(SERVICE_PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return out
    out["pid"] = pid
    try:
        os.kill(pid, 0)
        out["running"] = True
    except OSError:
        out["stale_pid"] = True
        return out
    try:
        out["uptime_seconds"] = max(0, int(dt.datetime.now().timestamp() - SERVICE_PID_PATH.stat().st_mtime))
    except OSError:
        pass
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            out["health_ok"] = bool(payload.get("ok"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        out["health_ok"] = False
    return out


def _profile_last_browser_use() -> tuple[str | None, int | None]:
    """Return (fetched_at_iso, age_days) of the most recent browser-mode read.

    Scans .source-reader/runs/*.json by mtime; treats either invocation.mode=='browser'
    or result.metadata.auto_upgraded as a browser use. age_days is None when the
    timestamp can't be parsed."""
    if not RUNS_DIR.exists():
        return None, None
    files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        if invocation.get("mode") != "browser" and not metadata.get("auto_upgraded"):
            continue
        fetched_at = result.get("fetched_at") or payload.get("recorded_at")
        if not fetched_at:
            return None, None
        try:
            last_used = dt.datetime.fromisoformat(str(fetched_at))
            age_days = (dt.datetime.now() - last_used).days
            return str(fetched_at), max(0, age_days)
        except ValueError:
            return str(fetched_at), None
    return None, None


def profile_status() -> dict[str, object]:
    path_str, exists, _used_default = resolve_browser_profile("")
    profile_path = pathlib.Path(path_str)
    last_used, age_days = _profile_last_browser_use()
    if not exists:
        health = "missing"
    elif age_days is None:
        health = "untested"
    elif age_days >= PROFILE_CRITICAL_DAYS:
        health = "critical"
    elif age_days >= PROFILE_WARN_DAYS:
        health = "warning"
    else:
        health = "ok"
    return {
        "path": str(profile_path),
        "exists": exists,
        "size_bytes": _dir_size(profile_path),
        "last_browser_use": last_used,
        "age_days": age_days,
        "health": health,
        "warn_days": PROFILE_WARN_DAYS,
        "critical_days": PROFILE_CRITICAL_DAYS,
        "credential_warning": CREDENTIAL_WARNING,
    }


def runtime_status() -> dict[str, object]:
    sr_dir = ROOT_DIR / ".source-reader"
    return {
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "root": str(ROOT_DIR),
        "data_dir_size_bytes": _dir_size(sr_dir),
    }


def gather_status(recent_limit: int = 10) -> dict[str, object]:
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "service": service_status(),
        "profile": profile_status(),
        "recent_reads": recent_reads_from_runs(recent_limit),
        "recent_failures": recent_failures_from_logs(recent_limit),
        "playwright": playwright_status(),
        "yt_dlp": yt_dlp_status(),
        "whisper": whisper_status(),
        "groq": groq_status(),
        "runtime": runtime_status(),
    }


def status_to_markdown(report: dict[str, object]) -> str:
    service = report.get("service") if isinstance(report.get("service"), dict) else {}
    profile = report.get("profile") if isinstance(report.get("profile"), dict) else {}
    playwright = report.get("playwright") if isinstance(report.get("playwright"), dict) else {}
    yt_dlp = report.get("yt_dlp") if isinstance(report.get("yt_dlp"), dict) else {}
    whisper = report.get("whisper") if isinstance(report.get("whisper"), dict) else {}
    groq = report.get("groq") if isinstance(report.get("groq"), dict) else {}
    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    recent = report.get("recent_reads") if isinstance(report.get("recent_reads"), list) else []
    recent_failures = report.get("recent_failures") if isinstance(report.get("recent_failures"), list) else []

    if service.get("running"):
        uptime = service.get("uptime_seconds") or 0
        service_line = f"running (pid {service.get('pid')}, port {service.get('port')}, uptime {_humanize_duration(uptime)})"
    elif service.get("stale_pid"):
        service_line = f"stopped (stale pid {service.get('pid')})"
    else:
        service_line = "stopped"
    health_line = "ok" if service.get("health_ok") else ("unreachable" if service.get("running") else "n/a")

    size = _humanize_bytes(int(profile.get("size_bytes") or 0))
    age_days = profile.get("age_days")
    if not profile.get("exists"):
        last_line = "profile missing"
    elif age_days is None:
        last_line = "not used for browser reads yet"
    else:
        last_line = f"{profile.get('last_browser_use')} ({age_days}d ago)"

    if recent:
        recent_lines: list[str] = []
        for r in recent:
            if not isinstance(r, dict):
                continue
            stamp = (r.get("fetched_at") or "")[:16]
            parts = [
                stamp,
                _short_source(str(r.get("source") or "")),
                str(r.get("source_type") or "?"),
                f"conf={r.get('confidence', 0)}",
                f"mode={r.get('mode') or '?'}",
            ]
            if r.get("auto_upgraded"):
                parts.append("auto-upgraded")
            errors = r.get("errors") if isinstance(r.get("errors"), list) else []
            if errors:
                parts.append(f"errors={len(errors)}")
            recent_lines.append("- " + " | ".join(parts))
        recent_block = "\n".join(recent_lines)
    else:
        recent_block = "- (none)"

    if recent_failures:
        failure_lines: list[str] = []
        for item in recent_failures:
            if not isinstance(item, dict):
                continue
            parts = [
                _short_source(str(item.get("source") or "")),
                str(item.get("read_quality") or "?"),
                f"conf={item.get('confidence', 0)}",
            ]
            reason = item.get("auth_assistance_reason") or item.get("blocked_by")
            if reason:
                parts.append(f"reason={reason}")
            errors = item.get("errors") if isinstance(item.get("errors"), list) else []
            if errors:
                parts.append(f"errors={len(errors)}")
            parts.append(str(item.get("failure_log_path") or ""))
            failure_lines.append("- " + " | ".join(parts))
        failure_block = "\n".join(failure_lines)
    else:
        failure_block = "- (none)"

    return f"""# Source Reader Status

- Generated at: {report.get('generated_at')}

## Service

- Status: {service_line}
- Health endpoint: {health_line}
- Pid file: {service.get('pid_file')}

## Profile (default)

- Path: {profile.get('path')}
- Exists: {profile.get('exists')}
- Size: {size}
- Last browser read: {last_line}
- Health: {profile.get('health')} (warn >= {profile.get('warn_days')}d, critical >= {profile.get('critical_days')}d)

> {profile.get('credential_warning')}

## Recent Reads (last {len(recent)})

{recent_block}

## Recent Failures (last {len(recent_failures)})

{failure_block}

## Playwright

- Installed: {playwright.get('installed')}
- Version: {playwright.get('version') or 'n/a'}

## yt-dlp

- Installed: {yt_dlp.get('installed')}
- Source: {yt_dlp.get('source') or 'n/a'}
- Version: {yt_dlp.get('version') or 'n/a'}
- Vendor dir: {yt_dlp.get('vendor_dir') or 'n/a'}

## Whisper

- Installed: {whisper.get('installed')}
- Model ready: {whisper.get('model_ready')} ({whisper.get('model_path') or 'n/a'})
- ffmpeg: {whisper.get('ffmpeg') or 'not found'}
- Version: {whisper.get('version') or 'n/a'}

## Groq (online transcription)

- Configured: {groq.get('configured')}
- Source: {groq.get('source') or 'n/a'}

## Runtime

- Python: {runtime.get('python_version')} ({runtime.get('platform')})
- .source-reader/ size: {_humanize_bytes(int(runtime.get('data_dir_size_bytes') or 0))}
"""


def run_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Show source-reader status")
    parser.add_argument("--format", choices=["json", "md"], default="md")
    parser.add_argument("--recent", type=int, default=10)
    args = parser.parse_args(argv)
    report = gather_status(max(0, args.recent))
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(status_to_markdown(report))
    return 0


def _profile_dir(name: str) -> pathlib.Path:
    safe = (name or "default").strip()
    if not safe or "/" in safe or safe.startswith("."):
        raise SystemExit(f"invalid profile name: {name!r}")
    return (ROOT_DIR / ".source-reader" / "profiles" / safe).resolve()


def _count_files(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for entry in path.rglob("*") if entry.is_file())


def profile_info(name: str = "default") -> dict[str, object]:
    profile_path = _profile_dir(name)
    exists = profile_path.exists()
    last_used, age_days = (None, None)
    if name == "default":
        last_used, age_days = _profile_last_browser_use()
    if not exists:
        health = "missing"
    elif age_days is None:
        health = "untested"
    elif age_days >= PROFILE_CRITICAL_DAYS:
        health = "critical"
    elif age_days >= PROFILE_WARN_DAYS:
        health = "warning"
    else:
        health = "ok"
    return {
        "name": name,
        "path": str(profile_path),
        "exists": exists,
        "size_bytes": _dir_size(profile_path),
        "file_count": _count_files(profile_path),
        "last_browser_use": last_used,
        "age_days": age_days,
        "health": health,
        "warn_days": PROFILE_WARN_DAYS,
        "critical_days": PROFILE_CRITICAL_DAYS,
        "credential_warning": CREDENTIAL_WARNING,
    }


def profile_rotate(name: str = "default") -> dict[str, object]:
    profile_path = _profile_dir(name)
    profiles_dir = profile_path.parent
    profiles_dir.mkdir(parents=True, exist_ok=True)
    backup_path: pathlib.Path | None = None
    had_content = profile_path.exists() and any(profile_path.iterdir())
    if had_content:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = profiles_dir / f"{name}.bak-{ts}"
        profile_path.rename(backup_path)
    elif profile_path.exists():
        profile_path.rmdir()
    profile_path.mkdir(parents=True, exist_ok=True)
    return {
        "name": name,
        "profile_path": str(profile_path),
        "backup_path": str(backup_path) if backup_path else None,
        "had_content": had_content,
        "note": "下次浏览器读取需要在打开的窗口重新登录目标站点。",
        "credential_warning": CREDENTIAL_WARNING,
    }


def profile_info_to_markdown(info: dict[str, object]) -> str:
    age_days = info.get("age_days")
    if not info.get("exists"):
        last_line = "profile missing"
    elif age_days is None:
        last_line = "not used for browser reads yet"
    else:
        last_line = f"{info.get('last_browser_use')} ({age_days}d ago)"
    return f"""# Source Reader Profile: {info.get('name')}

- Path: {info.get('path')}
- Exists: {info.get('exists')}
- Size: {_humanize_bytes(int(info.get('size_bytes') or 0))}
- Files: {info.get('file_count')}
- Last browser read: {last_line}
- Health: {info.get('health')} (warn >= {info.get('warn_days')}d, critical >= {info.get('critical_days')}d)

> {info.get('credential_warning')}
"""


def profile_rotate_to_markdown(report: dict[str, object]) -> str:
    backup_line = (
        f"- Backup: {report.get('backup_path')}"
        if report.get("backup_path")
        else "- Backup: (no prior content to back up)"
    )
    return f"""# Source Reader Profile Rotated: {report.get('name')}

- New empty profile: {report.get('profile_path')}
{backup_line}
- {report.get('note')}

> {report.get('credential_warning')}
"""


def run_profile(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Inspect or rotate the source-reader browser profile")
    parser.add_argument("action", choices=["info", "rotate"])
    parser.add_argument("--name", default="default")
    parser.add_argument("--format", choices=["json", "md"], default="md")
    args = parser.parse_args(argv)
    if args.action == "info":
        report = profile_info(args.name)
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(profile_info_to_markdown(report))
        return 0
    report = profile_rotate(args.name)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(profile_rotate_to_markdown(report))
    return 0


def request_url(url: str) -> tuple[bytes, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            final_url = response.geturl()
            content_type = response.headers.get("content-type", "")
            return response.read(), content_type, final_url
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc


def decode_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    return body.decode(charset, errors="replace")


def extract_html(html: str) -> tuple[str, str]:
    extractor = TextExtractor()
    extractor.feed(html)
    return extractor.title.strip(), extractor.text()


def read_basic_url(url: str, max_chars: int) -> ReaderOutput:
    try:
        body, content_type, final_url = request_url(url)
    except RuntimeError as exc:
        err_str = str(exc)
        http_blocked = any(code in err_str for code in ("403", "429", "503", "Forbidden", "Too Many Requests"))
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title=url,
            url=url,
            read_quality="blocked" if http_blocked else "failed",
            strategy="html_text_extraction",
            token_policy=token_policy(max_chars, False),
            content="",
            metadata={"blocked_by": "http_error", "requested_url": url},
            errors=[f"HTTP request failed: {err_str}"],
        )
    decoded = decode_body(body, content_type)
    is_html = "html" in content_type or decoded.lstrip().startswith("<")
    if is_html:
        title, content = extract_html(decoded)
        strategy = "html_text_extraction"
    else:
        title, content = final_url, decoded
        strategy = "plain_text_response"
    content, clipped = cap_text(content, max_chars)
    auth_wall, auth_reason = detect_access_limitation(url, final_url, title, content)
    js_shell = is_html and looks_like_js_shell(decoded, content)
    metadata: dict[str, object] = {
        "content_type": content_type,
        "requested_url": url,
    }
    errors: list[str] = []
    read_quality = "basic" if content else "partial"
    if auth_wall:
        read_quality = "blocked"
        metadata["blocked_by"] = auth_reason
        metadata["auth_assistance_reason"] = auth_reason
        errors.append("Page appears to require login or authorization. Retry with browser/auth reader.")
    elif js_shell:
        read_quality = "partial"
        metadata["maybe_js_rendered"] = True
        errors.append("Page looks like a JavaScript-rendered shell. Retry with browser reader.")
    return ReaderOutput(
        input_type="url",
        source_type="webpage",
        title=title or final_url,
        url=final_url,
        read_quality=read_quality,
        strategy=strategy,
        token_policy=token_policy(max_chars, clipped),
        content=content or "读取结果为空。",
        metadata=metadata,
        errors=errors,
    )


def read_browser_url(
    url: str,
    max_chars: int,
    browser_profile: str,
    headless: bool = False,
    interactive_login: bool = False,
    login_timeout_ms: int = 180000,
) -> ReaderOutput:
    if not playwright_installed():
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title=url,
            url=url,
            read_quality="failed",
            strategy="playwright_persistent_profile",
            token_policy=token_policy(max_chars, False),
            content="",
            metadata={"browser_profile": str(pathlib.Path(browser_profile).expanduser())},
            errors=[
                "Playwright is not installed. Run: python3 scripts/install.py --install-browser",
            ],
        )
    script = SCRIPT_DIR / "browser_reader.mjs"
    command = [
        "node",
        str(script),
        "--url",
        url,
        "--profile",
        browser_profile,
        "--max-chars",
        str(max_chars),
    ]
    if headless:
        command.append("--headless")
    if interactive_login:
        command.extend(["--interactive-login", "--login-timeout-ms", str(login_timeout_ms)])
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"browser reader exited with {proc.returncode}"
        try:
            parsed_error = json.loads(message)
            message = str(parsed_error.get("error") or message)
        except json.JSONDecodeError:
            pass
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title=url,
            url=url,
            read_quality="failed",
            strategy="playwright_persistent_profile",
            token_policy=token_policy(max_chars, False),
            content="",
            metadata={"browser_profile": str(pathlib.Path(browser_profile).expanduser())},
            errors=[message],
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title=url,
            url=url,
            read_quality="failed",
            strategy="playwright_persistent_profile",
            token_policy=token_policy(max_chars, False),
            content="",
            metadata={"browser_profile": str(pathlib.Path(browser_profile).expanduser())},
            errors=[f"browser reader returned non-json output: {exc}"],
        )

    return ReaderOutput(
        input_type="url",
        source_type="webpage",
        title=str(payload.get("title") or url),
        url=str(payload.get("url") or url),
        read_quality=str(payload.get("read_quality") or "browser"),
        strategy=str(payload.get("strategy") or "playwright_persistent_profile"),
        token_policy=str(payload.get("token_policy") or token_policy(max_chars, False)),
        content=str(payload.get("content") or ""),
        metadata=dict(payload.get("metadata") or {}),
        errors=list(payload.get("errors") or []),
    )


def read_scrapling_url(url: str, max_chars: int) -> ReaderOutput:
    try:
        from scrapling.fetchers import StealthyFetcher  # type: ignore[import]
    except ImportError:
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title=url,
            url=url,
            read_quality="failed",
            strategy="scrapling_stealthy_fetcher",
            token_policy=token_policy(max_chars, False),
            content="",
            errors=["Scrapling 未安装。运行: python3 scripts/install.py --install-scrapling"],
        )
    errors: list[str] = []
    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    except Exception as exc:
        return ReaderOutput(
            input_type="url",
            source_type="webpage",
            title=url,
            url=url,
            read_quality="failed",
            strategy="scrapling_stealthy_fetcher",
            token_policy=token_policy(max_chars, False),
            content="",
            errors=[f"Scrapling StealthyFetcher 失败: {exc}"],
        )
    title = ""
    try:
        title_el = page.find("title")
        title = str(title_el.text).strip() if title_el and hasattr(title_el, "text") else ""
    except Exception:
        pass
    try:
        content_text = page.get_all_text(ignore_tags=("script", "style", "head", "noscript"))
    except TypeError:
        try:
            content_text = str(page.get_all_text())
        except Exception as exc2:
            content_text = ""
            errors.append(f"文本提取警告: {exc2}")
    except Exception as exc:
        content_text = ""
        errors.append(f"文本提取失败: {exc}")
    content, clipped = cap_text(content_text or "", max_chars)
    read_quality = "basic" if content and len(content.strip()) > 200 else "partial"
    return ReaderOutput(
        input_type="url",
        source_type="webpage",
        title=title or url,
        url=url,
        read_quality=read_quality,
        strategy="scrapling_stealthy_fetcher",
        token_policy=token_policy(max_chars, clipped),
        content=content or "Scrapling 读取结果为空。",
        metadata={"scrapling_mode": "StealthyFetcher"},
        errors=errors,
    )


def raw_github_url(owner: str, repo: str, branch: str, path: str) -> str:
    quoted_path = "/".join(urllib.parse.quote(part) for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{quoted_path}"


def github_api(url: str) -> object:
    body, content_type, _ = request_url(url)
    if "json" not in content_type:
        raise RuntimeError(f"GitHub API returned non-json content type: {content_type}")
    return json.loads(decode_body(body, content_type))


def read_github_repo_readme(owner: str, repo: str, original_url: str, max_chars: int) -> ReaderOutput:
    errors: list[str] = []
    candidates = [
        ("main", "README.md"),
        ("master", "README.md"),
        ("main", "readme.md"),
        ("master", "readme.md"),
        ("main", "README"),
        ("master", "README"),
    ]
    for branch, path in candidates:
        readme_url = raw_github_url(owner, repo, branch, path)
        try:
            body, content_type, final_url = request_url(readme_url)
        except RuntimeError as exc:
            errors.append(f"{branch}/{path}: {exc}")
            continue
        content = decode_body(body, content_type)
        content, clipped = cap_text(content, max_chars)
        return ReaderOutput(
            input_type="url",
            source_type="github_repo",
            title=f"{owner}/{repo} README",
            url=original_url,
            read_quality="targeted",
            strategy="github_repo_readme_only",
            token_policy=token_policy(max_chars, clipped),
            content=content,
            metadata={"owner": owner, "repo": repo, "read_url": final_url, "branch": branch, "path": path},
            errors=errors,
        )
    return ReaderOutput(
        input_type="url",
        source_type="github_repo",
        title=f"{owner}/{repo}",
        url=original_url,
        read_quality="failed",
        strategy="github_repo_readme_only",
        token_policy=token_policy(max_chars, False),
        content="",
        metadata={"owner": owner, "repo": repo},
        errors=errors or ["README not found"],
    )


def read_github_blob(owner: str, repo: str, parts: list[str], original_url: str, max_chars: int) -> ReaderOutput:
    branch = parts[2]
    path = "/".join(parts[3:])
    url = raw_github_url(owner, repo, branch, path)
    body, content_type, final_url = request_url(url)
    content, clipped = cap_text(decode_body(body, content_type), max_chars)
    return ReaderOutput(
        input_type="url",
        source_type="github_file",
        title=f"{owner}/{repo}/{path}",
        url=original_url,
        read_quality="targeted",
        strategy="github_blob_raw_file_only",
        token_policy=token_policy(max_chars, clipped),
        content=content,
        metadata={"owner": owner, "repo": repo, "branch": branch, "path": path, "read_url": final_url},
    )


def read_github_issue(owner: str, repo: str, issue_number: str, original_url: str, max_chars: int) -> ReaderOutput:
    issue = github_api(f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}")
    comments_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments?per_page=12"
    comments: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        comments_data = github_api(comments_url)
        if isinstance(comments_data, list):
            comments = comments_data[:12]
    except RuntimeError as exc:
        errors.append(f"comments: {exc}")

    lines = [
        f"# {issue.get('title', '')}",
        "",
        f"State: {issue.get('state', '')}",
        f"Author: {(issue.get('user') or {}).get('login', '') if isinstance(issue.get('user'), dict) else ''}",
        "",
        normalize_text(str(issue.get("body") or "")),
    ]
    if comments:
        lines.append("\n## First comments")
    for item in comments:
        user = item.get("user") if isinstance(item, dict) else {}
        login = user.get("login", "") if isinstance(user, dict) else ""
        lines.append(f"\n### {login} at {item.get('created_at', '')}")
        lines.append(normalize_text(str(item.get("body") or "")))

    content, clipped = cap_text("\n".join(lines), max_chars)
    return ReaderOutput(
        input_type="url",
        source_type="github_issue_or_pr",
        title=str(issue.get("title") or f"{owner}/{repo}#{issue_number}"),
        url=original_url,
        author=(issue.get("user") or {}).get("login", "") if isinstance(issue.get("user"), dict) else "",
        published_at=str(issue.get("created_at") or ""),
        read_quality="targeted",
        strategy="github_issue_body_plus_first_12_comments",
        token_policy=token_policy(max_chars, clipped),
        content=content,
        metadata={"owner": owner, "repo": repo, "number": issue_number, "comments_read": len(comments)},
        errors=errors,
    )


def read_github_release(owner: str, repo: str, parts: list[str], original_url: str, max_chars: int) -> ReaderOutput:
    if len(parts) >= 4 and parts[2] == "tag":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(parts[3])}"
    else:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    release = github_api(api_url)
    body = normalize_text(str(release.get("body") or ""))
    content, clipped = cap_text(f"# {release.get('name') or release.get('tag_name')}\n\n{body}", max_chars)
    return ReaderOutput(
        input_type="url",
        source_type="github_release",
        title=str(release.get("name") or release.get("tag_name") or f"{owner}/{repo} release"),
        url=original_url,
        author=(release.get("author") or {}).get("login", "") if isinstance(release.get("author"), dict) else "",
        published_at=str(release.get("published_at") or ""),
        read_quality="targeted",
        strategy="github_release_notes_only",
        token_policy=token_policy(max_chars, clipped),
        content=content,
        metadata={"owner": owner, "repo": repo, "tag": release.get("tag_name", "")},
    )


def read_gist(parts: list[str], original_url: str, max_chars: int) -> ReaderOutput:
    if len(parts) < 2:
        return read_basic_url(original_url, max_chars)
    gist_id = parts[1]
    api = github_api(f"https://api.github.com/gists/{gist_id}")
    files = api.get("files", {}) if isinstance(api, dict) else {}
    selected_name = ""
    selected = ""
    if isinstance(files, dict):
        preferred = sorted(files.values(), key=lambda item: 0 if str(item.get("filename", "")).lower().endswith((".md", ".txt")) else 1)
        if preferred:
            item = preferred[0]
            selected_name = str(item.get("filename") or "")
            selected = str(item.get("content") or "")
    selected, clipped = cap_text(selected, max_chars)
    return ReaderOutput(
        input_type="url",
        source_type="github_gist",
        title=str(api.get("description") or selected_name or gist_id) if isinstance(api, dict) else gist_id,
        url=original_url,
        author=(api.get("owner") or {}).get("login", "") if isinstance(api, dict) and isinstance(api.get("owner"), dict) else "",
        published_at=str(api.get("created_at") or "") if isinstance(api, dict) else "",
        read_quality="targeted" if selected else "partial",
        strategy="gist_first_markdown_or_text_file_only",
        token_policy=token_policy(max_chars, clipped),
        content=selected,
        metadata={"gist_id": gist_id, "selected_file": selected_name, "file_count": len(files) if isinstance(files, dict) else 0},
    )


def read_github(url: str, max_chars: int) -> ReaderOutput:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parsed.netloc == "gist.github.com":
        return read_gist(parts, url, max_chars)
    if len(parts) < 2:
        return read_basic_url(url, max_chars)
    owner, repo = parts[0], parts[1]
    rest = parts[2:]
    if not rest:
        return read_github_repo_readme(owner, repo, url, max_chars)
    if rest[0] == "blob" and len(rest) >= 3:
        return read_github_blob(owner, repo, rest, url, max_chars)
    if rest[0] in {"issues", "pull"} and len(rest) >= 2:
        return read_github_issue(owner, repo, rest[1], url, max_chars)
    if rest[0] == "releases":
        return read_github_release(owner, repo, rest, url, max_chars)
    if rest[0] == "tree":
        result = read_github_repo_readme(owner, repo, url, max_chars)
        result.strategy = "github_tree_fallback_repo_readme_only"
        result.metadata["requested_path"] = "/".join(rest)
        return result
    return read_github_repo_readme(owner, repo, url, max_chars)


def read_discussion(url: str, max_chars: int) -> ReaderOutput:
    result = read_basic_url(url, max_chars)
    result.source_type = "discussion"
    result.strategy = f"{result.strategy}_discussion_page_only"
    result.read_quality = "basic"
    result.metadata["note"] = "Read page text only; comments may be partial depending on page rendering."
    return result


def read_pdf(url: str, max_chars: int) -> ReaderOutput:
    arxiv_match = re.search(r"arxiv\.org/pdf/([^/?#]+)", url)
    if arxiv_match:
        paper_id = arxiv_match.group(1).removesuffix(".pdf")
        abs_url = f"https://arxiv.org/abs/{paper_id}"
        result = read_basic_url(abs_url, max_chars)
        result.source_type = "paper"
        result.url = url
        result.strategy = "arxiv_pdf_url_to_abs_page"
        result.metadata["read_url"] = abs_url
        result.metadata["paper_id"] = paper_id
        return result

    result = read_basic_url(url, max_chars)
    result.source_type = "pdf"
    if result.content.startswith("%PDF"):
        result.read_quality = "partial"
        result.strategy = "pdf_binary_detected_no_extractor"
        result.content = "检测到 PDF 二进制内容。当前轻量版不解析 PDF；建议后续接入 pdftotext 或 pymupdf，只提取标题、摘要、章节和结论。"
    return result


def read_file(path_text: str, max_chars: int) -> ReaderOutput:
    path = pathlib.Path(path_text).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"file does not exist: {path}")
    if not path.is_file():
        raise RuntimeError(f"not a file: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    content, clipped = cap_text(text, max_chars)
    suffix = path.suffix.lower()
    source_type = "local_file"
    if suffix in {".md", ".markdown"}:
        source_type = "markdown"
    elif suffix in {".txt", ".log"}:
        source_type = "text"
    elif suffix in {".html", ".htm"}:
        title, extracted = extract_html(text)
        content, clipped = cap_text(extracted, max_chars)
        source_type = "html"
    else:
        title = path.stem
    return ReaderOutput(
        input_type="file",
        source_type=source_type,
        title=locals().get("title") or path.stem,
        local_path=str(path),
        read_quality="basic",
        strategy="local_text_file",
        token_policy=token_policy(max_chars, clipped),
        content=content,
        metadata={"suffix": suffix, "size_bytes": path.stat().st_size},
    )



def effective_max_chars(max_chars: int, read_depth: str) -> int:
    if max_chars != DEFAULT_MAX_CHARS:
        return max_chars
    return READ_DEPTH_BUDGETS[read_depth]


def _auto_upgrade_reason(result: ReaderOutput) -> str:
    if result.metadata.get("blocked_by") == "auth_wall":
        return "auth_wall"
    if result.metadata.get("maybe_js_rendered"):
        return "js_shell"
    return "low_confidence"


def _should_auto_upgrade(result: ReaderOutput) -> bool:
    if result.confidence < CONFIDENCE_UPGRADE_THRESHOLD:
        return True
    if result.metadata.get("blocked_by") == "auth_wall":
        return True
    if result.metadata.get("maybe_js_rendered"):
        return True
    return False


def _try_auto_upgrade(
    result: ReaderOutput,
    source: str,
    max_chars: int,
    browser_profile: str,
    headless: bool,
    interactive_login: bool,
    login_timeout_ms: int,
) -> ReaderOutput:
    """Try to upgrade a fast read to a browser read. Mutates `result.metadata`
    with skip reasons when upgrade is impossible; returns the new result when
    upgraded."""
    profile_path, profile_exists, used_default = resolve_browser_profile(browser_profile)
    if not playwright_installed():
        result.metadata["auto_upgrade_skipped"] = "playwright_not_installed"
        return result
    if not profile_exists:
        result.metadata["auto_upgrade_skipped"] = "browser_profile_missing"
        result.metadata["auto_upgrade_profile_hint"] = profile_path
        return result
    # When a CLI user (interactive tty) hits an auth wall on the fast path,
    # auto-open a visible browser and wait for login. Skip for MCP / HTTP
    # callers (non-tty) to avoid blocking 180s on a headless service.
    if (
        not interactive_login
        and result.metadata.get("blocked_by") == "auth_wall"
        and sys.stdin.isatty()
    ):
        interactive_login = True
        headless = False
        result.metadata["auto_interactive_login"] = True
    browser_result = read_browser_url(
        source,
        max_chars,
        profile_path,
        headless=headless,
        interactive_login=interactive_login,
        login_timeout_ms=login_timeout_ms,
    )
    browser_result.metadata["fast_reader"] = {
        "read_quality": result.read_quality,
        "confidence": result.confidence,
        "final_url": result.url,
        "maybe_js_rendered": result.metadata.get("maybe_js_rendered", False),
        "errors": result.errors,
    }
    browser_result.metadata["auto_upgraded"] = True
    browser_result.metadata["auto_upgrade_reason"] = _auto_upgrade_reason(result)
    if used_default:
        browser_result.metadata["browser_profile_default"] = True

    # Third tier: scrapling StealthyFetcher when browser also fails (e.g. Cloudflare)
    should_try_scrapling = (
        browser_result.read_quality in {"failed", "blocked"}
        or looks_like_cloudflare_block(browser_result.content)
    )
    if should_try_scrapling and scrapling_installed():
        scrapling_result = read_scrapling_url(source, max_chars)
        scrapling_conf = score_confidence(scrapling_result)
        browser_conf = score_confidence(browser_result)
        if scrapling_conf > browser_conf or browser_result.read_quality in {"failed", "blocked"}:
            scrapling_result.metadata["prev_readers"] = {
                "fast": {"read_quality": result.read_quality, "confidence": result.confidence},
                "browser": {"read_quality": browser_result.read_quality, "confidence": browser_conf},
            }
            scrapling_result.metadata["auto_upgraded"] = True
            scrapling_result.metadata["auto_upgrade_reason"] = "scrapling_anti_bot"
            return scrapling_result

    return browser_result


def classify_and_read(
    source: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    mode: str = "fast",
    browser_profile: str = "",
    headless: bool = False,
    interactive_login: bool = False,
    login_timeout_ms: int = 180000,
    read_depth: str = "standard",
    auto_upgrade: bool = True,
) -> ReaderOutput:
    max_chars = effective_max_chars(max_chars, read_depth)

    if not source.startswith(("http://", "https://")):
        result = read_file(source, max_chars)
    elif mode == "browser":
        if not browser_profile:
            result = ReaderOutput(
                input_type="url",
                source_type="webpage",
                title=source,
                url=source,
                read_quality="failed",
                strategy="playwright_persistent_profile",
                token_policy=token_policy(max_chars, False),
                content="",
                errors=["--browser-profile is required when --mode browser is used."],
            )
        else:
            result = read_browser_url(
                source,
                max_chars,
                browser_profile,
                headless=headless,
                interactive_login=interactive_login,
                login_timeout_ms=login_timeout_ms,
            )
    elif mode == "scrapling":
        result = read_scrapling_url(source, max_chars)
    else:
        parsed = urllib.parse.urlparse(source)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host in {"github.com", "gist.github.com"}:
            result = read_github(source, max_chars)
        elif matches_video_host(host):
            result = read_video(source, max_chars)
        elif host in {"news.ycombinator.com", "www.reddit.com", "reddit.com", "v2ex.com", "www.v2ex.com"} or host.endswith(".reddit.com"):
            result = read_discussion(source, max_chars)
        elif path.endswith(".pdf") or "arxiv.org/pdf/" in source:
            result = read_pdf(source, max_chars)
        else:
            result = read_basic_url(source, max_chars)
            result.confidence = score_confidence(result)
            if auto_upgrade and mode in {"fast", "auto"} and _should_auto_upgrade(result):
                result = _try_auto_upgrade(
                    result,
                    source,
                    max_chars,
                    browser_profile,
                    headless,
                    interactive_login,
                    login_timeout_ms,
                )

    return attach_interaction(
        result,
        source,
        read_depth,
        mode,
        browser_profile,
        headless,
        interactive_login,
        login_timeout_ms,
    )


def to_markdown(result: ReaderOutput) -> str:
    metadata = json.dumps(result.metadata, ensure_ascii=False, indent=2)
    preview = json.dumps(result.preview, ensure_ascii=False, indent=2)
    errors = "\n".join(f"- {error}" for error in result.errors) or "- none"
    actions = "\n".join(
        format_action(action)
        for action in result.next_actions
    ) or "- none"
    return f"""# {result.title}

## Quick Preview

```json
{preview}
```

## Next Operations

{actions}

## Source Reader Metadata

- Input type: {result.input_type}
- Source type: {result.source_type}
- URL: {result.url}
- Local path: {result.local_path}
- Author: {result.author}
- Published: {result.published_at}
- Fetched: {result.fetched_at}
- Reader: {result.reader}
- Read quality: {result.read_quality}
- Confidence: {result.confidence}
- Strategy: {result.strategy}
- Token policy: {result.token_policy}
- Read depth: {result.read_depth}

## Metadata

```json
{metadata}
```

## Errors

{errors}

## Content

{result.content}
"""


def format_action(action: dict[str, str]) -> str:
    scope = action.get("scope", "reader")
    adapter = action.get("adapter", "")
    scope_label = f"{scope}:{adapter}" if adapter else scope
    lines = [
        f"- [{action.get('label', action.get('id', 'action'))}] `{action.get('id', '')}`",
        f"  - Scope: `{scope_label}`",
        f"  - {action.get('description', '')}",
    ]
    if action.get("command"):
        lines.append(f"  - Command: `{action['command']}`")
    if action.get("prompt"):
        lines.append(f"  - Prompt: {action['prompt']}")
    return "\n".join(lines)


def doctor_to_markdown(report: dict[str, object]) -> str:
    checks = report.get("checks", {})
    if not isinstance(checks, dict):
        checks = {}
    recommendations = report.get("recommendations", [])
    if not isinstance(recommendations, list):
        recommendations = []
    check_lines = "\n".join(
        f"- {key}: {value}"
        for key, value in checks.items()
    )
    recommendation_lines = "\n".join(f"- {item}" for item in recommendations) or "- none"
    return f"""# Source Reader Doctor

- Status: {report.get("status", "unknown")}

## Checks

{check_lines}

## Recommendations

{recommendation_lines}
"""


def review_to_markdown(report: dict[str, object]) -> str:
    runs = report.get("runs", [])
    if not isinstance(runs, list):
        runs = []
    suggestions = report.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    run_lines = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        run_lines.append(
            "- "
            f"`{item.get('run_id', '')}` "
            f"{item.get('domain', '')} "
            f"{item.get('source_type', '')} "
            f"{item.get('read_quality', '')} "
            f"strategy={item.get('strategy', '')} "
            f"feedback={item.get('feedback_count', 0)}"
        )
    suggestion_lines = "\n".join(f"- {item}" for item in suggestions) or "- none"
    return f"""# Source Reader Run Review

- Status: {report.get("status", "unknown")}

## Recent Runs

{chr(10).join(run_lines) or "- none"}

## Suggestions

{suggestion_lines}
"""


def action_read_depth(action_id: str) -> tuple[str, str]:
    if action_id in {"continue_deep_read", "deep_read"}:
        return "full", ""
    if action_id == "extract_outline":
        return "preview", "outline"
    if action_id == "extract_code":
        return "standard", "code"
    if action_id in {"login_with_browser", "retry_with_login", "retry_with_profile"}:
        return "preview", "auth"
    raise SystemExit(f"unsupported action: {action_id}")


def apply_focus_hint(result: ReaderOutput, focus: str) -> ReaderOutput:
    if not focus:
        return result
    result.metadata["focus"] = focus
    if focus == "outline":
        headings = extract_headings(result.content, limit=40)
        result.content = "\n".join(f"- {heading}" for heading in headings) or "未提取到明确大纲。"
        result.strategy = f"{result.strategy}_outline_focus"
    elif focus == "code":
        blocks = extract_code_like_blocks(result.content)
        result.content = "\n\n".join(blocks) or "未提取到明显代码、命令、配置或 API 示例。"
        result.strategy = f"{result.strategy}_code_focus"
    return result


def extract_code_like_blocks(text: str, limit: int = 20) -> list[str]:
    blocks: list[str] = []
    in_fence = False
    current: list[str] = []
    command_pattern = re.compile(r"^\s*(\$|npm|pnpm|yarn|python3?|pip|curl|git|docker|kubectl|adb|gradle|mvn)\b")
    config_pattern = re.compile(r"^\s*([A-Za-z_][\w.-]*\s*[:=]|\{|\}|\[.+\])")
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("```"):
            if in_fence:
                current.append(stripped)
                blocks.append("\n".join(current))
                current = []
                in_fence = False
            else:
                current = [stripped]
                in_fence = True
            continue
        if in_fence:
            current.append(stripped)
            continue
        if command_pattern.match(stripped) or config_pattern.match(stripped):
            blocks.append(stripped)
        if len(blocks) >= limit:
            break
    return blocks[:limit]


def run_action(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Execute a source-reader action")
    parser.add_argument("action_id", help="action id from Source Reader output")
    parser.add_argument("--source", required=True, help="URL or local file path")
    parser.add_argument("--format", choices=["json", "md"], default="md")
    parser.add_argument("--mode", choices=["fast", "browser", "auto", "scrapling"], default="auto")
    parser.add_argument("--browser-profile", default=".source-reader/profiles/default")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--login-timeout-ms", type=int, default=180000)
    parser.add_argument("--no-auto-upgrade", action="store_true")
    args = parser.parse_args(argv)

    read_depth, focus = action_read_depth(args.action_id)
    mode = "browser" if focus == "auth" else args.mode
    interactive_login = args.interactive_login or focus == "auth"
    result = classify_and_read(
        args.source,
        DEFAULT_MAX_CHARS,
        mode=mode,
        browser_profile=args.browser_profile,
        headless=args.headless,
        interactive_login=interactive_login,
        login_timeout_ms=args.login_timeout_ms,
        read_depth=read_depth,
        auto_upgrade=not args.no_auto_upgrade,
    )
    result.metadata["action_id"] = args.action_id
    result = apply_focus_hint(result, focus)
    result.preview = build_preview(result)
    result.actions = build_next_actions(
        result,
        args.source,
        mode,
        args.browser_profile,
        args.headless,
        interactive_login,
        args.login_timeout_ms,
    )
    result.next_actions = result.actions
    persist_run_log(
        result,
        args.source,
        {
            "command": "action",
            "action_id": args.action_id,
            "mode": mode,
            "read_depth": read_depth,
            "focus": focus,
        },
    )
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(to_markdown(result))
    return 0


def run_feedback(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Record feedback for a source-reader run")
    parser.add_argument("verdict", choices=["mark_good", "mark_bad"], help="feedback verdict")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--expected", default="")
    args = parser.parse_args(argv)
    path = record_feedback(args.run_id, args.verdict, args.reason, args.expected)
    print(f"feedback recorded: {path.relative_to(ROOT_DIR)}")
    return 0


def run_review_runs(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Review recent source-reader runs")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=["json", "md"], default="md")
    args = parser.parse_args(argv)
    report = summarize_recent_runs(args.limit)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(review_to_markdown(report))
    return 0


def service_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def post_json(url: str, payload: dict[str, object], timeout: int = 300) -> dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        raise RuntimeError("service returned non-object json")
    if parsed.get("ok") is False:
        raise RuntimeError(str(parsed.get("error") or "source-reader service failed"))
    return parsed


def get_json(url: str, timeout: int = 20) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        raise RuntimeError("service returned non-object json")
    return parsed


def read_via_service(
    source: str,
    host: str,
    port: int,
    max_chars: int,
    mode: str,
    read_depth: str,
    browser_profile: str,
    headless: bool,
    interactive_login: bool,
    login_timeout_ms: int,
) -> ReaderOutput:
    response = post_json(
        service_url(host, port, "/read"),
        {
            "source": source,
            "max_chars": max_chars,
            "mode": mode,
            "read_depth": read_depth,
            "browser_profile": browser_profile,
            "headless": headless,
            "interactive_login": interactive_login,
            "login_timeout_ms": login_timeout_ms,
        },
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("service response missing result")
    return ReaderOutput(**result)


class SourceReaderHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class SourceReaderHandler(http.server.BaseHTTPRequestHandler):
    server_version = "SourceReader/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(
            f"{dt.datetime.now().isoformat(timespec='seconds')} "
            f"{self.address_string()} {fmt % args}\n"
        )

    def read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("request body must be a json object")
        return payload

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(200, {"ok": True, "status": "ok", "root": str(ROOT_DIR)})
            return
        if parsed.path == "/review-runs":
            query = urllib.parse.parse_qs(parsed.query)
            limit = int((query.get("limit") or ["20"])[0])
            self.send_json(200, {"ok": True, "report": summarize_recent_runs(limit)})
            return
        self.send_json(404, {"ok": False, "error": f"unknown endpoint: {parsed.path}"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json_body()
            if parsed.path == "/read":
                result = self.handle_read(payload)
                self.send_json(200, {"ok": True, "result": result.to_dict()})
                return
            if parsed.path == "/action":
                result = self.handle_action(payload)
                self.send_json(200, {"ok": True, "result": result.to_dict()})
                return
            if parsed.path == "/feedback":
                run_id = str(payload.get("run_id") or "")
                verdict = str(payload.get("verdict") or "")
                if verdict not in {"mark_good", "mark_bad"}:
                    raise ValueError("verdict must be mark_good or mark_bad")
                path = record_feedback(run_id, verdict, str(payload.get("reason") or ""), str(payload.get("expected") or ""))
                self.send_json(200, {"ok": True, "path": str(path.relative_to(ROOT_DIR))})
                return
            self.send_json(404, {"ok": False, "error": f"unknown endpoint: {parsed.path}"})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})

    def handle_read(self, payload: dict[str, object]) -> ReaderOutput:
        source = str(payload.get("source") or "")
        if not source:
            raise ValueError("source is required")
        result = classify_and_read(
            source,
            int(payload.get("max_chars") or DEFAULT_MAX_CHARS),
            mode=str(payload.get("mode") or "auto"),
            browser_profile=str(payload.get("browser_profile") or ".source-reader/profiles/default"),
            headless=bool(payload.get("headless") or False),
            interactive_login=bool(payload.get("interactive_login") or False),
            login_timeout_ms=int(payload.get("login_timeout_ms") or 180000),
            read_depth=str(payload.get("read_depth") or "preview"),
        )
        host, port = self.server.server_address[:2]
        result = rewrite_actions_for_service(result, source, str(host), int(port))
        persist_run_log(
            result,
            source,
            {
                "command": "service_read",
                "mode": payload.get("mode") or "auto",
                "read_depth": payload.get("read_depth") or "preview",
            },
        )
        return result

    def handle_action(self, payload: dict[str, object]) -> ReaderOutput:
        source = str(payload.get("source") or "")
        action_id = str(payload.get("action_id") or "")
        if not source:
            raise ValueError("source is required")
        if not action_id:
            raise ValueError("action_id is required")
        read_depth, focus = action_read_depth(action_id)
        mode = "browser" if focus == "auth" else str(payload.get("mode") or "auto")
        interactive_login = bool(payload.get("interactive_login") or False) or focus == "auth"
        browser_profile = str(payload.get("browser_profile") or ".source-reader/profiles/default")
        result = classify_and_read(
            source,
            int(payload.get("max_chars") or DEFAULT_MAX_CHARS),
            mode=mode,
            browser_profile=browser_profile,
            headless=bool(payload.get("headless") or False),
            interactive_login=interactive_login,
            login_timeout_ms=int(payload.get("login_timeout_ms") or 180000),
            read_depth=read_depth,
        )
        result.metadata["action_id"] = action_id
        result = apply_focus_hint(result, focus)
        result.preview = build_preview(result)
        result.actions = build_next_actions(
            result,
            source,
            mode,
            browser_profile,
            bool(payload.get("headless") or False),
            interactive_login,
            int(payload.get("login_timeout_ms") or 180000),
        )
        result.next_actions = result.actions
        host, port = self.server.server_address[:2]
        result = rewrite_actions_for_service(result, source, str(host), int(port))
        persist_run_log(
            result,
            source,
            {
                "command": "service_action",
                "action_id": action_id,
                "mode": mode,
                "read_depth": read_depth,
                "focus": focus,
            },
        )
        return result


def run_serve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run local source-reader HTTP service or stdio MCP server")
    parser.add_argument("--mcp", action="store_true", help="run the stdio MCP server instead of the HTTP service")
    parser.add_argument("--host", default=DEFAULT_SERVICE_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_SERVICE_PORT)
    args = parser.parse_args(argv)
    if args.mcp:
        return run_mcp([])
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("source-reader service only supports localhost binding")
    server = SourceReaderHTTPServer((args.host, args.port), SourceReaderHandler)
    print(f"source-reader service listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def run_remote_read(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read through local source-reader service")
    parser.add_argument("source", help="URL or local file path")
    parser.add_argument("--host", default=DEFAULT_SERVICE_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_SERVICE_PORT)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--format", choices=["json", "md"], default="md")
    parser.add_argument("--mode", choices=["fast", "browser", "auto", "scrapling"], default="auto")
    parser.add_argument("--read-depth", choices=["preview", "standard", "full"], default="preview")
    parser.add_argument("--browser-profile", default=".source-reader/profiles/default")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--login-timeout-ms", type=int, default=180000)
    args = parser.parse_args(argv)
    try:
        result = read_via_service(
            args.source,
            args.host,
            args.port,
            args.max_chars,
            args.mode,
            args.read_depth,
            args.browser_profile,
            args.headless,
            args.interactive_login,
            args.login_timeout_ms,
        )
    except Exception as exc:
        print(f"source-reader service unavailable: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(to_markdown(result))
    return 0


def run_remote_action(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Execute action through local source-reader service")
    parser.add_argument("action_id")
    parser.add_argument("--source", required=True)
    parser.add_argument("--host", default=DEFAULT_SERVICE_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_SERVICE_PORT)
    parser.add_argument("--format", choices=["json", "md"], default="md")
    parser.add_argument("--mode", choices=["fast", "browser", "auto"], default="auto")
    parser.add_argument("--browser-profile", default=".source-reader/profiles/default")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--login-timeout-ms", type=int, default=180000)
    args = parser.parse_args(argv)
    try:
        response = post_json(
            service_url(args.host, args.port, "/action"),
            {
                "action_id": args.action_id,
                "source": args.source,
                "mode": args.mode,
                "browser_profile": args.browser_profile,
                "headless": args.headless,
                "interactive_login": args.interactive_login,
                "login_timeout_ms": args.login_timeout_ms,
            },
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("service response missing result")
        output = ReaderOutput(**result)
    except Exception as exc:
        print(f"source-reader service unavailable: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(output.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(to_markdown(output))
    return 0


def rewrite_actions_for_service(result: ReaderOutput, source: str, host: str, port: int) -> ReaderOutput:
    rewritten: list[dict[str, object]] = []
    for item in result.actions:
        copied = dict(item)
        action_id = str(copied.get("id") or "")
        if action_id == "continue_deep_read":
            copied["command"] = (
                f"python3 scripts/source_reader.py remote-read {shell_quote(source)} "
                f"--read-depth full --format md --host {host} --port {port}"
            )
        elif action_id in {"extract_outline", "extract_code", "login_with_browser"}:
            copied["command"] = (
                f"python3 scripts/source_reader.py remote-action {shell_quote(action_id)} "
                f"--source {shell_quote(source)} --format md --host {host} --port {port}"
            )
        rewritten.append(copied)
    result.actions = rewritten
    result.next_actions = rewritten
    return result


MCP_STDIO_MODE = "headers"


def mcp_read_message() -> dict[str, object] | None:
    global MCP_STDIO_MODE
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped_line = line.strip()
        if stripped_line.startswith(b"{"):
            MCP_STDIO_MODE = "jsonl"
            payload = json.loads(stripped_line.decode("utf-8", errors="replace"))
            return payload if isinstance(payload, dict) else None
        decoded = line.decode("utf-8", errors="replace").strip()
        if not decoded:
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length") or "0")
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length).decode("utf-8", errors="replace")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        return None
    return payload


def mcp_send_message(payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if MCP_STDIO_MODE == "jsonl":
        sys.stdout.buffer.write(body + b"\n")
        sys.stdout.buffer.flush()
        return
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def mcp_tool_schema() -> list[dict[str, object]]:
    return [
        {
            "name": "source_reader_read",
            "description": "Read a URL or local file with token-aware source-reader.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "read_depth": {"type": "string", "enum": ["preview", "standard", "full"], "default": "preview"},
                    "mode": {"type": "string", "enum": ["fast", "browser", "auto", "scrapling"], "default": "auto"},
                    "format": {"type": "string", "enum": ["md", "json"], "default": "md"},
                },
                "required": ["source"],
            },
        },
        {
            "name": "source_reader_action",
            "description": "Execute a source-reader action such as continue_deep_read, extract_outline, extract_code, or login_with_browser.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "source": {"type": "string"},
                    "format": {"type": "string", "enum": ["md", "json"], "default": "md"},
                },
                "required": ["action_id", "source"],
            },
        },
        {
            "name": "source_reader_feedback",
            "description": "Record source-reader run feedback.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["mark_good", "mark_bad"]},
                    "reason": {"type": "string"},
                    "expected": {"type": "string"},
                },
                "required": ["run_id", "verdict"],
            },
        },
    ]


def mcp_call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    if name == "source_reader_read":
        source = str(arguments.get("source") or "")
        if not source:
            raise ValueError("source is required")
        result = classify_and_read(
            source,
            DEFAULT_MAX_CHARS,
            mode=str(arguments.get("mode") or "auto"),
            browser_profile=".source-reader/profiles/default",
            headless=False,
            interactive_login=False,
            login_timeout_ms=180000,
            read_depth=str(arguments.get("read_depth") or "preview"),
        )
        persist_run_log(result, source, {"command": "mcp_read", "mode": arguments.get("mode") or "auto"})
        fmt = str(arguments.get("format") or "md")
        text = json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if fmt == "json" else to_markdown(result)
        return {"content": [{"type": "text", "text": text}]}
    if name == "source_reader_action":
        source = str(arguments.get("source") or "")
        action_id = str(arguments.get("action_id") or "")
        if not source or not action_id:
            raise ValueError("source and action_id are required")
        read_depth, focus = action_read_depth(action_id)
        mode = "browser" if focus == "auth" else "auto"
        result = classify_and_read(
            source,
            DEFAULT_MAX_CHARS,
            mode=mode,
            browser_profile=".source-reader/profiles/default",
            headless=False,
            interactive_login=focus == "auth",
            login_timeout_ms=180000,
            read_depth=read_depth,
        )
        result.metadata["action_id"] = action_id
        result = apply_focus_hint(result, focus)
        result.preview = build_preview(result)
        result.actions = build_next_actions(result, source, mode, ".source-reader/profiles/default", False, focus == "auth", 180000)
        result.next_actions = result.actions
        persist_run_log(result, source, {"command": "mcp_action", "action_id": action_id})
        fmt = str(arguments.get("format") or "md")
        text = json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if fmt == "json" else to_markdown(result)
        return {"content": [{"type": "text", "text": text}]}
    if name == "source_reader_feedback":
        path = record_feedback(
            str(arguments.get("run_id") or ""),
            str(arguments.get("verdict") or ""),
            str(arguments.get("reason") or ""),
            str(arguments.get("expected") or ""),
        )
        return {"content": [{"type": "text", "text": f"feedback recorded: {path.relative_to(ROOT_DIR)}"}]}
    raise ValueError(f"unknown tool: {name}")


def run_mcp(argv: list[str]) -> int:
    _parser = argparse.ArgumentParser(description="Run source-reader MCP server")
    _parser.parse_args(argv)
    while True:
        message = mcp_read_message()
        if message is None:
            return 0
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method == "notifications/initialized":
            continue
        try:
            if method == "initialize":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                protocol_version = str(params.get("protocolVersion") or "2024-11-05")
                result = {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "source-reader", "version": "0.1.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tool_schema()}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "tools/call":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                result = mcp_call_tool(name, arguments)
            else:
                raise ValueError(f"unsupported MCP method: {method}")
            if request_id is not None:
                mcp_send_message({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            if request_id is not None:
                mcp_send_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": str(exc)},
                    }
                )
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "action":
        return run_action(argv[1:])
    if argv and argv[0] == "feedback":
        return run_feedback(argv[1:])
    if argv and argv[0] == "review-runs":
        return run_review_runs(argv[1:])
    if argv and argv[0] == "status":
        return run_status(argv[1:])
    if argv and argv[0] == "profile":
        return run_profile(argv[1:])
    if argv and argv[0] == "serve":
        return run_serve(argv[1:])
    if argv and argv[0] == "remote-read":
        return run_remote_read(argv[1:])
    if argv and argv[0] == "remote-action":
        return run_remote_action(argv[1:])
    if argv and argv[0] == "mcp":
        return run_mcp(argv[1:])
    if argv and argv[0] == "read":
        argv = argv[1:]

    parser = argparse.ArgumentParser(description="Read one source with a token-aware strategy")
    parser.add_argument("source", nargs="?", help="URL or local file path")
    parser.add_argument("--doctor", action="store_true", help="check source-reader browser/runtime setup")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="maximum content characters to return")
    parser.add_argument("--format", choices=["json", "md"], default="md", help="output format")
    parser.add_argument("--mode", choices=["fast", "browser", "auto", "scrapling"], default="fast", help="read strategy mode")
    parser.add_argument("--read-depth", choices=["preview", "standard", "full"], default="standard", help="reading budget and interaction depth")
    parser.add_argument("--browser-profile", default="", help="persistent browser profile directory for browser/auto mode")
    parser.add_argument("--headless", action="store_true", help="run browser mode headless")
    parser.add_argument("--interactive-login", action="store_true", help="wait for manual login when browser mode reaches an auth page")
    parser.add_argument("--login-timeout-ms", type=int, default=180000, help="manual login wait timeout in milliseconds")
    parser.add_argument("--no-auto-upgrade", action="store_true", help="disable automatic browser upgrade when fast read confidence is low")
    parser.add_argument("--action", help="run a source-reader action on the source (continue_deep_read | extract_outline | extract_code | login_with_browser)")
    parser.add_argument("--feedback", choices=["good", "bad"], help="record a feedback verdict (requires --run-id)")
    parser.add_argument("--run-id", default="", help="run id for --feedback")
    parser.add_argument("--reason", default="", help="reason text for --feedback bad")
    parser.add_argument("--expected", default="", help="expected outcome for --feedback bad")
    parser.add_argument("--remote", action="store_true", help="route read/action through the local source-reader service")
    parser.add_argument("--service-host", default=DEFAULT_SERVICE_HOST, help="source-reader service host for --remote")
    parser.add_argument("--service-port", type=int, default=DEFAULT_SERVICE_PORT, help="source-reader service port for --remote")
    args = parser.parse_args(argv)

    if args.doctor:
        report = source_reader_doctor(args.browser_profile or ".source-reader/profiles/default")
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(doctor_to_markdown(report))
        return 0 if report.get("status") == "ok" else 1

    if args.feedback:
        if not args.run_id:
            parser.error("--feedback requires --run-id")
        verdict = "mark_good" if args.feedback == "good" else "mark_bad"
        return run_feedback([verdict, "--run-id", args.run_id, "--reason", args.reason, "--expected", args.expected])

    if not args.source:
        parser.error("source is required unless --doctor or --feedback is used")

    profile_for_dispatch = args.browser_profile or ".source-reader/profiles/default"

    if args.action:
        action_argv = [
            args.action,
            "--source", args.source,
            "--format", args.format,
            "--mode", args.mode,
            "--browser-profile", profile_for_dispatch,
            "--login-timeout-ms", str(args.login_timeout_ms),
        ]
        if args.headless:
            action_argv.append("--headless")
        if args.interactive_login:
            action_argv.append("--interactive-login")
        if args.no_auto_upgrade:
            action_argv.append("--no-auto-upgrade")
        if args.remote:
            action_argv = [
                args.action,
                "--source", args.source,
                "--host", args.service_host,
                "--port", str(args.service_port),
                "--format", args.format,
                "--mode", args.mode,
                "--browser-profile", profile_for_dispatch,
                "--login-timeout-ms", str(args.login_timeout_ms),
            ]
            if args.headless:
                action_argv.append("--headless")
            if args.interactive_login:
                action_argv.append("--interactive-login")
            return run_remote_action(action_argv)
        return run_action(action_argv)

    if args.remote:
        remote_argv = [
            args.source,
            "--host", args.service_host,
            "--port", str(args.service_port),
            "--max-chars", str(args.max_chars),
            "--format", args.format,
            "--mode", args.mode,
            "--read-depth", args.read_depth,
            "--browser-profile", profile_for_dispatch,
            "--login-timeout-ms", str(args.login_timeout_ms),
        ]
        if args.headless:
            remote_argv.append("--headless")
        if args.interactive_login:
            remote_argv.append("--interactive-login")
        return run_remote_read(remote_argv)

    try:
        result = classify_and_read(
            args.source,
            args.max_chars,
            mode=args.mode,
            browser_profile=args.browser_profile,
            headless=args.headless,
            interactive_login=args.interactive_login,
            login_timeout_ms=args.login_timeout_ms,
            read_depth=args.read_depth,
            auto_upgrade=not args.no_auto_upgrade,
        )
    except Exception as exc:
        print(f"source-reader failed: {exc}", file=sys.stderr)
        return 1

    persist_run_log(
        result,
        args.source,
        {
            "command": "read",
            "mode": args.mode,
            "read_depth": args.read_depth,
            "max_chars": args.max_chars,
            "browser_profile": args.browser_profile,
            "headless": args.headless,
            "interactive_login": args.interactive_login,
            "login_timeout_ms": args.login_timeout_ms,
            "auto_upgrade": not args.no_auto_upgrade,
        },
    )

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(to_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
