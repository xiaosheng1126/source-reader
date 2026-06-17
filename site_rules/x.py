from __future__ import annotations

import re
import urllib.parse


DOMAINS = {"x.com", "twitter.com"}


def matches(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    return host in DOMAINS


def detect_access_limitation(title: str, content: str) -> str:
    joined = f"{title}\n{content}".lower()
    logged_out_markers = (
        "new to x?",
        "sign up now",
        "don't miss what's happening",
        "log in",
        "sign up",
    )
    has_logged_out_chrome = sum(1 for marker in logged_out_markers if marker in joined) >= 2
    profile_contradiction = bool(re.search(r"\b[\d,.]+\s*posts\b", content, re.I)) and bool(
        re.search(r"hasn.?t posted", content, re.I)
    )
    if has_logged_out_chrome or profile_contradiction:
        return "limited_logged_out_view"
    return ""
