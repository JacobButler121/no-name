from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


class MediaProbe:
    def __init__(self, ffprobe_path: str = "ffprobe", timeout_seconds: int = 60) -> None:
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds

    def inspect(self, media_path: Path) -> dict[str, Any]:
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            payload = json.loads(completed.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise ProbeError(f"Could not inspect the uploaded video: {exc}") from exc

        video = next(
            (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"),
            None,
        )
        if not video:
            raise ProbeError("The file does not contain a video stream.")
        audio = next(
            (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"),
            None,
        )
        raw_duration = payload.get("format", {}).get("duration") or video.get("duration") or 0
        try:
            duration = max(float(raw_duration), 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        return {
            "durationSec": round(duration, 3),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "videoCodec": video.get("codec_name"),
            "audioCodec": audio.get("codec_name") if audio else None,
            "format": payload.get("format", {}).get("format_name"),
            "sizeBytes": media_path.stat().st_size,
        }
