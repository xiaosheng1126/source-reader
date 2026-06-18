from __future__ import annotations

import pathlib
import platform
import re
import subprocess
import sys
import tempfile

from reader_core.models import ReaderOutput
from reader_core.optional import (
    ffmpeg_path,
    resolve_yt_dlp_command,
    whisper_env,
    whisper_model_path,
    whisper_vendor_installed,
)
from reader_core.utils import cap_text, normalize_space, normalize_text, token_policy

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"

VIDEO_HOSTS = frozenset(
    [
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "bilibili.com",
        "www.bilibili.com",
        "douyin.com",
        "www.douyin.com",
        "v.douyin.com",
    ]
)


def matches_video_host(host: str) -> bool:
    return (
        host in VIDEO_HOSTS
        or host.endswith(".youtube.com")
        or host.endswith(".youtu.be")
        or host.endswith(".bilibili.com")
        or host.endswith(".douyin.com")
    )


def vtt_to_text(text: str) -> str:
    lines: list[str] = []
    previous = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or re.match(r"^\d+$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = normalize_space(line)
        if line and line != previous:
            lines.append(line)
            previous = line
    return normalize_text("\n".join(lines))


def _whisper_transcribe(audio_path: str, model_dir: pathlib.Path) -> tuple[bool, str]:
    script = SCRIPTS_DIR / "whisper_transcribe.py"
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as output:
        out_path = output.name
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--audio",
                audio_path,
                "--model-dir",
                str(model_dir),
                "--output",
                out_path,
            ],
            env=whisper_env(),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return False, proc.stderr[:500]
        text = pathlib.Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        return True, text
    except subprocess.TimeoutExpired:
        return False, "whisper transcription timed out (>300s)"
    finally:
        pathlib.Path(out_path).unlink(missing_ok=True)


def _ffmpeg_install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        try:
            version = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            arch = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
            return f"brew install ffmpeg  # macOS {version} {arch}"
        except OSError:
            return "brew install ffmpeg"
    if system == "Linux":
        return "sudo apt install ffmpeg  # or sudo yum install ffmpeg"
    return "Install ffmpeg and ensure it is available in PATH"


def read_video(url: str, max_chars: int) -> ReaderOutput:
    resolved = resolve_yt_dlp_command()
    if not resolved:
        return ReaderOutput(
            input_type="url",
            source_type="video",
            title=url,
            url=url,
            read_quality="partial",
            strategy="video_metadata_stub_no_yt_dlp",
            token_policy=token_policy(max_chars, False),
            content="未安装 yt-dlp，无法自动读取字幕。建议安装后优先读取字幕/章节，而不是下载视频或抓取评论。",
            errors=["yt-dlp not found"],
        )

    with tempfile.TemporaryDirectory() as tmp:
        yt_dlp_cmd, yt_dlp_run_env, yt_dlp_source = resolved
        # Fetch title separately: --print aborts subtitle writing on some yt-dlp versions
        title_proc = subprocess.run(
            [*yt_dlp_cmd, "--skip-download", "--print", "title", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=yt_dlp_run_env,
            text=True,
            timeout=30,
        )
        title = title_proc.stdout.strip().splitlines()[-1] if title_proc.stdout.strip() else url

        output_tpl = str(pathlib.Path(tmp) / "subtitle.%(ext)s")
        cmd = [
            *yt_dlp_cmd,
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "zh-CN,zh-Hans,zh,en.*",
            "--sub-format",
            "vtt",
            "--output",
            output_tpl,
            url,
        ]
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=yt_dlp_run_env,
            text=True,
            timeout=90,
        )
        subtitle_files = sorted(pathlib.Path(tmp).glob("*.vtt"))

        if subtitle_files:
            text = vtt_to_text(subtitle_files[0].read_text(encoding="utf-8", errors="replace"))
            content, clipped = cap_text(text, max_chars)
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="transcript",
                strategy="video_subtitles_only",
                token_policy=token_policy(max_chars, clipped),
                content=content,
                metadata={"subtitle_file": subtitle_files[0].name, "yt_dlp_source": yt_dlp_source},
            )

        if not whisper_vendor_installed():
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_subtitle_attempt",
                token_policy=token_policy(max_chars, False),
                content="没有找到可用字幕；Whisper 未安装，无法转写。运行 --install-video 可启用音频转写。",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=["subtitle not found", "whisper not installed"],
            )

        model_dir = whisper_model_path()
        if model_dir is None:
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_subtitle_attempt",
                token_policy=token_policy(max_chars, False),
                content="没有找到可用字幕；Whisper 模型未下载，运行 --install-video 完成安装。",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=["subtitle not found", "whisper model not found"],
            )

        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            hint = _ffmpeg_install_hint()
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_subtitle_attempt",
                token_policy=token_policy(max_chars, False),
                content=f"没有找到可用字幕；ffmpeg 未安装，音频下载无法进行。请先安装：{hint}",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=["subtitle not found", f"ffmpeg not found; install: {hint}"],
            )

        audio_tpl = str(pathlib.Path(tmp) / "audio.%(ext)s")
        audio_cmd = [
            *yt_dlp_cmd,
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--output",
            audio_tpl,
            url,
        ]
        audio_proc = subprocess.run(
            audio_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=yt_dlp_run_env,
            text=True,
            timeout=120,
        )
        audio_files = [file for file in pathlib.Path(tmp).iterdir() if file.name.startswith("audio.")]
        if not audio_files or audio_proc.returncode != 0:
            err = audio_proc.stderr.strip()[-500:] if audio_proc.stderr else "audio download failed"
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_audio_download_failed",
                token_policy=token_policy(max_chars, False),
                content="字幕和音频下载均失败，无法转写。",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=[err],
            )

        success, text_or_error = _whisper_transcribe(str(audio_files[0]), model_dir)
        if not success:
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_audio_transcription_failed",
                token_policy=token_policy(max_chars, False),
                content="Whisper 转写失败。",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=[text_or_error],
            )

        content, clipped = cap_text(text_or_error, max_chars)
        return ReaderOutput(
            input_type="url",
            source_type="video",
            title=title,
            url=url,
            read_quality="transcript",
            strategy="video_audio_transcribed",
            token_policy=token_policy(max_chars, clipped),
            content=content,
            metadata={"yt_dlp_source": yt_dlp_source, "audio_file": audio_files[0].name},
        )
