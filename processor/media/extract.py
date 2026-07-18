from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .probe import MediaProbe


_TIMESTAMP_PATTERN = re.compile(r"frame_(\d+)\.jpg$")


class ExtractionError(RuntimeError):
    pass


class FrameExtractor:
    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: int = 300,
        scene_threshold: float = 0.32,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.timeout_seconds = timeout_seconds
        self.scene_threshold = scene_threshold
        self.probe = MediaProbe(ffprobe_path, timeout_seconds=min(timeout_seconds, 60))

    @staticmethod
    def interval_for(duration_seconds: float) -> float:
        if duration_seconds <= 180:
            return 2.0
        if duration_seconds <= 1200:
            return 5.0
        return 10.0

    def extract(
        self,
        media_path: Path,
        job_directory: Path,
        *,
        job_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], Path]:
        metadata = metadata or self.probe.inspect(media_path)
        duration = float(metadata.get("durationSec") or 0)
        interval = self.interval_for(duration)
        frames_dir = job_directory / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = frames_dir / "frame_%012d.jpg"
        selection = (
            f"isnan(prev_selected_t)+gt(scene,{self.scene_threshold})+"
            f"gte(t-prev_selected_t,{interval})"
        )
        video_filter = (
            f"select='{selection}',"
            "scale='min(1280,iw)':-2"
        )
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vf",
            video_filter,
            "-fps_mode",
            "vfr",
            "-frame_pts",
            "1",
            "-q:v",
            "3",
            str(output_pattern),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ExtractionError("ffmpeg is not installed on the processor.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError("Frame extraction timed out.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "Frame extraction failed.").strip()
            raise ExtractionError(detail[-1000:]) from exc

        paths = sorted(frames_dir.glob("frame_*.jpg"))
        frames: list[dict[str, Any]] = []
        time_base = self._video_time_base(media_path)
        for index, path in enumerate(paths, start=1):
            timestamp = self._timestamp_from_name(path, time_base, fallback=(index - 1) * interval)
            frame_id = f"frame-{index:04d}"
            stable_path = frames_dir / f"{frame_id}.jpg"
            path.replace(stable_path)
            frames.append(
                {
                    "id": frame_id,
                    "timestampSec": round(min(timestamp, duration or timestamp), 3),
                    "path": str(stable_path.resolve()),
                    "thumbnailUrl": f"/api/jobs/{job_id}/frames/{stable_path.name}",
                    "width": int(metadata.get("width") or 0),
                    "height": int(metadata.get("height") or 0),
                    "source": "scene_or_interval",
                }
            )
        if not frames:
            raise ExtractionError("No frames could be extracted from this video.")
        manifest = {
            "version": 1,
            "durationSec": duration,
            "intervalSec": interval,
            "frames": frames,
        }
        manifest_path = frames_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return frames, manifest_path

    def _video_time_base(self, media_path: Path) -> float:
        command = [
            self.probe.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=time_base",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            numerator, denominator = completed.stdout.strip().split("/", 1)
            return float(numerator) / float(denominator)
        except (OSError, subprocess.SubprocessError, ValueError, ZeroDivisionError):
            return 1.0

    @staticmethod
    def _timestamp_from_name(path: Path, time_base: float, fallback: float) -> float:
        match = _TIMESTAMP_PATTERN.search(path.name)
        if not match:
            return fallback
        try:
            return float(match.group(1)) * time_base
        except ValueError:
            return fallback
