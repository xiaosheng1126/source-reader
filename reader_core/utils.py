# reader_core/utils.py
from __future__ import annotations

import re


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cap_text(text: str, max_chars: int) -> tuple[str, bool]:
    text = normalize_text(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    head_chars = max(1, int(max_chars * 0.72))
    tail_chars = max(1, max_chars - head_chars)
    clipped = (
        text[:head_chars].rstrip()
        + "\n\n[... content clipped by source-reader to save tokens ...]\n\n"
        + text[-tail_chars:].lstrip()
    )
    return clipped, True


def token_policy(max_chars: int, clipped: bool) -> str:
    suffix = "clipped_head_tail" if clipped else "full_within_budget"
    return f"max_chars={max_chars}; {suffix}"
