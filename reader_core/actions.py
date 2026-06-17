from __future__ import annotations

from typing import Any


def needs_auth_assistance(result: Any) -> bool:
    """Return whether a read result should expose the login assistance action."""
    read_quality = str(getattr(result, "read_quality", "") or "")
    metadata = getattr(result, "metadata", {}) or {}
    if read_quality in {"blocked", "failed"}:
        return True
    return bool(metadata.get("auth_assistance_reason") or metadata.get("blocked_by") == "auth_wall")
