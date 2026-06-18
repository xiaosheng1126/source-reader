from __future__ import annotations

import json
import os
import pathlib

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / ".source-reader" / "config.json"

_cache: dict[str, object] | None = None


def load() -> dict[str, object]:
    global _cache
    if _cache is not None:
        return _cache
    data: dict[str, object] = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    _cache = data
    return data


def get(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key.upper())
    if val:
        return val
    raw = load().get(key)
    return str(raw) if raw else default
