from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .probe import MediaProbe


class ExtractionError(RuntimeError):
    pass


class FrameExtractor:
    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: int = 300,
        interval_seconds: float = 5.0,
        similarity_threshold: float = 0.92,
        max_dimension: int = 1600,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.similarity_threshold = similarity_threshold
        self.max_dimension = max_dimension
        self.probe = MediaProbe(ffprobe_path, timeout_seconds=min(timeout_seconds, 60))

    @staticmethod
    def interval_for(duration_seconds: float) -> float:
        """Spotted deliberately samples on a predictable five-second cadence."""
        return 5.0

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
        interval = self.interval_seconds
        frames_dir = job_directory / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = frames_dir / "sample-%08d.jpg"
        video_filter = (
            f"fps=fps=1/{interval}:start_time=0:round=down:eof_action=pass,"
            f"scale='min({self.max_dimension},iw)':-2"
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

        sampled_paths = sorted(frames_dir.glob("sample-*.jpg"))
        retained_paths = self._remove_similar_frames(sampled_paths)
        frames: list[dict[str, Any]] = []
        retained_set = set(retained_paths)
        stable_index = 0
        for sample_index, path in enumerate(sampled_paths):
            if path not in retained_set:
                path.unlink(missing_ok=True)
                continue
            stable_index += 1
            timestamp = sample_index * interval
            frame_id = f"frame-{stable_index:04d}"
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
                    "source": "five_second_unique",
                }
            )
        if not frames:
            raise ExtractionError("No frames could be extracted from this video.")
        manifest = {
            "version": 1,
            "durationSec": duration,
            "intervalSec": interval,
            "sampledFrameCount": len(sampled_paths),
            "skippedSimilarFrames": len(sampled_paths) - len(frames),
            "frames": frames,
        }
        manifest_path = frames_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return frames, manifest_path

    def _remove_similar_frames(self, paths: list[Path]) -> list[Path]:
        """Remove near-identical samples before they can consume vision tokens.

        A tiny 16x16 grayscale fingerprint is generated locally with ffmpeg. A
        sample is retained only when it differs meaningfully from every prior
        retained sample, which also catches a scene that returns later.
        """
        if len(paths) < 2:
            return paths
        hashes = self._perceptual_hashes(paths)
        if len(hashes) != len(paths):
            return paths
        retained: list[Path] = []
        retained_hashes: list[int] = []
        bit_count = 16 * 16
        for path, fingerprint in zip(paths, hashes, strict=True):
            maximum_similarity = max(
                (1.0 - ((fingerprint ^ prior).bit_count() / bit_count) for prior in retained_hashes),
                default=0.0,
            )
            if maximum_similarity >= self.similarity_threshold:
                continue
            retained.append(path)
            retained_hashes.append(fingerprint)
        return retained or paths[:1]

    def _perceptual_hashes(self, paths: list[Path]) -> list[int]:
        pattern = paths[0].parent / "sample-%08d.jpg"
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(pattern),
            "-vf",
            "scale=16:16,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        frame_bytes = 16 * 16
        raw = completed.stdout
        if not isinstance(raw, bytes) or len(raw) < frame_bytes:
            return []
        hashes: list[int] = []
        for offset in range(0, len(raw) - frame_bytes + 1, frame_bytes):
            frame = raw[offset : offset + frame_bytes]
            average = sum(frame) / frame_bytes
            fingerprint = 0
            for value in frame:
                fingerprint = (fingerprint << 1) | int(value >= average)
            hashes.append(fingerprint)
        return hashes[: len(paths)]
