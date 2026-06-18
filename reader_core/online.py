from __future__ import annotations

import os
import pathlib


def groq_transcribe(audio_path: str, api_key: str, language: str = "zh") -> tuple[bool, str]:
    audio_bytes = pathlib.Path(audio_path).read_bytes()
    filename = os.path.basename(audio_path)
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {
        "file": (filename, audio_bytes, "audio/mpeg"),
        "model": (None, "whisper-large-v3-turbo"),
        "response_format": (None, "text"),
        "language": (None, language),
    }

    try:
        import requests as _requests
        resp = _requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            files=files,
            timeout=120,
        )
        if resp.status_code != 200:
            return False, f"groq http {resp.status_code}: {resp.text[:300]}"
        return True, resp.text.strip()
    except ImportError:
        pass
    except Exception as exc:
        return False, str(exc)

    # fallback: urllib with manual multipart
    import urllib.error
    import urllib.request
    import uuid

    boundary = f"----SourceReader{uuid.uuid4().hex[:16]}"

    parts: list[bytes] = []

    def _field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    parts.append(_field("model", "whisper-large-v3-turbo"))
    parts.append(_field("response_format", "text"))
    parts.append(_field("language", language))
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/mpeg\r\n\r\n"
        ).encode()
        + audio_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "groq-python/0.11.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return True, resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = str(exc)
        return False, f"groq http {exc.code}: {detail}"
    except Exception as exc:
        return False, str(exc)
