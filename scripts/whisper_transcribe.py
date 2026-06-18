#!/usr/bin/env python3
"""Standalone Whisper transcription wrapper. Called via subprocess only."""

from __future__ import annotations

import argparse
import pathlib
import sys


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Transcribe audio with faster-whisper")
    parser.add_argument("--audio", required=True, help="Path to audio file")
    parser.add_argument("--model-dir", required=True, help="Path to faster-whisper model directory")
    parser.add_argument("--output", required=True, help="Path to write transcript text")
    args = parser.parse_args(argv)

    vendor_dir = pathlib.Path(__file__).resolve().parents[1] / ".source-reader" / "vendor"
    if str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))

    try:
        from faster_whisper import WhisperModel  # type: ignore[import]
    except ImportError as exc:
        print(f"faster-whisper not found in vendor ({vendor_dir}): {exc}", file=sys.stderr)
        return 1

    try:
        model = WhisperModel(str(pathlib.Path(args.model_dir).resolve()), device="cpu", compute_type="int8")
        segments, _ = model.transcribe(args.audio, beam_size=5)
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    except Exception as exc:
        print(f"transcription failed: {exc}", file=sys.stderr)
        return 1

    pathlib.Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
