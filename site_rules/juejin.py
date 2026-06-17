from __future__ import annotations

import urllib.parse


DOMAINS = {"juejin.cn", "juejin.im"}


def matches(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    return host in DOMAINS


def detect_access_limitation(title: str, content: str) -> str:
    joined = f"{title}\n{content}".lower()
    strong_auth_markers = (
        "登录后查看",
        "请登录后",
        "登录后才能",
        "登录掘金",
        "扫码登录",
        "验证码登录",
        "第三方账号登录",
        "账号密码登录",
    )
    if any(marker in joined for marker in strong_auth_markers):
        return "auth_wall"
    return ""
