from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT_DIR / ".source-reader" / "vendor"
MODELS_DIR = ROOT_DIR / ".source-reader" / "models"
WHISPER_MODEL_NAME = "faster-whisper-medium"


def _run_check(
    command: list[str],
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = proc.stdout.strip() or proc.stderr.strip()
    return proc.returncode == 0, output


def yt_dlp_vendor_python_installed() -> bool:
    return (VENDOR_DIR / "yt_dlp").exists()


def yt_dlp_vendor_bin() -> pathlib.Path:
    suffix = ".exe" if os.name == "nt" else ""
    return VENDOR_DIR / "bin" / f"yt-dlp{suffix}"


def yt_dlp_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(VENDOR_DIR) if not existing else f"{VENDOR_DIR}{os.pathsep}{existing}"
    return env


def resolve_yt_dlp_command() -> tuple[list[str], dict[str, str] | None, str] | None:
    local_bin = yt_dlp_vendor_bin()
    if local_bin.exists():
        return [str(local_bin)], yt_dlp_env(), "project_vendor_bin"
    if yt_dlp_vendor_python_installed():
        return [sys.executable, "-m", "yt_dlp"], yt_dlp_env(), "project_vendor_python"
    if shutil.which("yt-dlp"):
        return ["yt-dlp"], None, "path"
    return None


def yt_dlp_status() -> dict[str, object]:
    resolved = resolve_yt_dlp_command()
    if not resolved:
        return {
            "installed": False,
            "source": "missing",
            "version": None,
            "vendor_dir": str(VENDOR_DIR),
        }
    command, env, source = resolved
    ok, output = _run_check(command + ["--version"], cwd=ROOT_DIR, env=env)
    return {
        "installed": ok,
        "source": source,
        "version": output if ok else None,
        "vendor_dir": str(VENDOR_DIR),
        "message": "" if ok else output,
    }


def whisper_vendor_installed() -> bool:
    return (VENDOR_DIR / "faster_whisper").exists()


def whisper_model_path() -> pathlib.Path | None:
    path = MODELS_DIR / WHISPER_MODEL_NAME
    if path.exists() and any(path.iterdir()):
        return path
    return None


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def whisper_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(VENDOR_DIR) if not existing else f"{VENDOR_DIR}{os.pathsep}{existing}"
    return env


def whisper_status() -> dict[str, object]:
    installed = whisper_vendor_installed()
    model = whisper_model_path()
    ffmpeg = ffmpeg_path()
    version: str | None = None
    if installed:
        ok, output = _run_check(
            [sys.executable, "-c", "import faster_whisper; print(faster_whisper.__version__)"],
            env=whisper_env(),
        )
        version = output if ok else None
    return {
        "installed": installed,
        "model_ready": model is not None,
        "model_path": str(model) if model else None,
        "ffmpeg": ffmpeg,
        "version": version,
    }


def playwright_installed() -> bool:
    return (ROOT_DIR / "node_modules" / "playwright").exists()


def playwright_status() -> dict[str, object]:
    installed = playwright_installed()
    version: str | None = None
    package = ROOT_DIR / "node_modules" / "playwright" / "package.json"
    if package.exists():
        try:
            version = json.loads(package.read_text(encoding="utf-8")).get("version")
        except (OSError, json.JSONDecodeError):
            version = None
    return {"installed": installed, "version": version}


def scrapling_installed() -> bool:
    return importlib.util.find_spec("scrapling") is not None


def groq_api_key() -> str | None:
    from reader_core.config import get
    return get("groq_api_key")


def groq_status() -> dict[str, object]:
    key = groq_api_key()
    if not key:
        return {"configured": False, "source": "missing"}
    source = "env" if os.environ.get("GROQ_API_KEY") else "config"
    return {"configured": True, "source": source}
