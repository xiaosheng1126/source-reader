from __future__ import annotations

import re
import urllib.parse

from site_rules import juejin as juejin_rule
from site_rules import x as x_rule


def detect_access_limitation(requested_url: str, final_url: str, title: str, content: str) -> tuple[bool, str]:
    parsed_final = urllib.parse.urlparse(final_url)
    parsed_requested = urllib.parse.urlparse(requested_url)
    final_path = parsed_final.path.lower()
    final_query = urllib.parse.parse_qs(parsed_final.query)
    joined = f"{title}\n{content}".lower()
    login_words = ("login", "signin", "sign in", "登录", "登陆", "授权", "认证")

    if any(word in final_path for word in ("/login", "/signin", "/passport")):
        return True, "auth_wall"
    if any(key in final_query for key in ("goto", "redirect", "redirect_uri", "return_url", "next")):
        if any(word in joined for word in login_words):
            return True, "auth_wall"
    if parsed_final.netloc == parsed_requested.netloc and len(content) < 300:
        if any(word in joined for word in login_words):
            return True, "auth_wall"

    for rule in (x_rule, juejin_rule):
        if rule.matches(final_url):
            limitation = rule.detect_access_limitation(title, content)
            if limitation:
                return True, limitation

    return False, ""


def looks_like_auth_wall(requested_url: str, final_url: str, title: str, content: str) -> bool:
    return detect_access_limitation(requested_url, final_url, title, content)[0]


def looks_like_js_shell(decoded: str, content: str) -> bool:
    lowered = decoded.lower()
    script_count = lowered.count("<script")
    app_markers = (
        'id="app"',
        "id='app'",
        'id="root"',
        "id='root'",
        "__next_data__",
        "window.__initial_state__",
        "webpack",
        "vite",
    )
    if len(content) >= 1200:
        return False
    if any(marker in lowered for marker in app_markers) and script_count >= 2:
        return True
    if script_count >= 8 and len(content) < 500:
        return True
    return False


def looks_like_cloudflare_block(content: str) -> bool:
    text = content.lower()
    markers = ("cloudflare", "cf-ray", "just a moment", "challenge-platform", "checking if the site connection")
    return sum(1 for marker in markers if marker in text) >= 2


def has_post_count_with_empty_timeline(content: str) -> bool:
    """Shared helper for tests and future timeline-like site rules."""
    return bool(re.search(r"\b[\d,.]+\s*posts\b", content, re.I)) and bool(
        re.search(r"hasn.?t posted", content, re.I)
    )
