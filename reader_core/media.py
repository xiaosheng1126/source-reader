from __future__ import annotations

import pathlib
import platform
import re
import subprocess
import sys
import tempfile

from reader_core.models import ReaderOutput
from reader_core.online import groq_transcribe
from reader_core.optional import (
    ffmpeg_path,
    groq_api_key,
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


def _download_audio_clip(
    yt_dlp_cmd: list[str],
    url: str,
    env: dict[str, str] | None,
    out_path: str,
    max_seconds: int,
) -> tuple[bool, str]:
    """Pipe yt-dlp stdout → ffmpeg stdin, keeping only the first max_seconds."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return False, "ffmpeg not found"
    yt_proc = subprocess.Popen(
        [*yt_dlp_cmd, "-f", "bestaudio", "-o", "-", "--quiet", url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        ff_proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-t", str(max_seconds), "-f", "mp3", "-q:a", "5", "-y", out_path],
            stdin=yt_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max_seconds + 90,
        )
        if yt_proc.stdout:
            yt_proc.stdout.close()
        yt_proc.wait(timeout=10)
        if ff_proc.returncode != 0:
            return False, ff_proc.stderr.decode("utf-8", errors="replace")[-300:]
        return True, ""
    except subprocess.TimeoutExpired:
        yt_proc.kill()
        return False, f"audio clip download timed out (>{max_seconds + 90}s)"
    except Exception as exc:
        yt_proc.kill()
        return False, str(exc)


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

        api_key = groq_api_key()
        model_dir = whisper_model_path()

        if not whisper_vendor_installed() and not api_key:
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_subtitle_attempt",
                token_policy=token_policy(max_chars, False),
                content="没有找到可用字幕；Whisper 未安装，且未配置在线转写（Groq）。运行 --install-video 或在 .source-reader/config.json 中设置 groq_api_key。",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=["subtitle not found", "whisper not installed", "groq not configured"],
            )

        if model_dir is None and not api_key:
            return ReaderOutput(
                input_type="url",
                source_type="video",
                title=title,
                url=url,
                read_quality="partial",
                strategy="video_subtitle_attempt",
                token_policy=token_policy(max_chars, False),
                content="没有找到可用字幕；Whisper 模型未下载，且未配置在线转写（Groq）。运行 --install-video 或设置 groq_api_key。",
                metadata={"yt_dlp_source": yt_dlp_source},
                errors=["subtitle not found", "whisper model not found", "groq not configured"],
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

        transcription_errors: list[str] = []

        # --- Groq path: clip download (bounded size, fast) ---
        if api_key:
            clip_seconds = 300 if max_chars <= 6000 else 900 if max_chars <= 24000 else 1800
            clip_path = str(pathlib.Path(tmp) / "clip.mp3")
            clip_ok, clip_err = _download_audio_clip(yt_dlp_cmd, url, yt_dlp_run_env, clip_path, clip_seconds)
            if clip_ok:
                groq_ok, groq_result = groq_transcribe(clip_path, api_key)
                if groq_ok:
                    content, clipped = cap_text(groq_result, max_chars)
                    return ReaderOutput(
                        input_type="url",
                        source_type="video",
                        title=title,
                        url=url,
                        read_quality="transcript",
                        strategy="video_audio_groq",
                        token_policy=token_policy(max_chars, clipped),
                        content=content,
                        metadata={
                            "yt_dlp_source": yt_dlp_source,
                            "clip_seconds": clip_seconds,
                            "groq_model": "whisper-large-v3-turbo",
                        },
                    )
                transcription_errors.append(f"groq: {groq_result}")
            else:
                transcription_errors.append(f"clip download: {clip_err}")

        # --- Whisper path: full audio download ---
        if whisper_vendor_installed() and model_dir is not None:
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
            audio_files = [f for f in pathlib.Path(tmp).iterdir() if f.name.startswith("audio.")]
            if audio_files and audio_proc.returncode == 0:
                success, text_or_error = _whisper_transcribe(str(audio_files[0]), model_dir)
                if success:
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
                transcription_errors.append(f"whisper: {text_or_error}")
            else:
                err = audio_proc.stderr.strip()[-300:] if audio_proc.stderr else "audio download failed"
                transcription_errors.append(f"whisper audio download: {err}")

        return ReaderOutput(
            input_type="url",
            source_type="video",
            title=title,
            url=url,
            read_quality="partial",
            strategy="video_audio_transcription_failed",
            token_policy=token_policy(max_chars, False),
            content="字幕和音频转写均失败，无法获取内容。",
            metadata={"yt_dlp_source": yt_dlp_source},
            errors=transcription_errors or ["no transcription method succeeded"],
        )
