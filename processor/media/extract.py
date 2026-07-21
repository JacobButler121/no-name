from __future__ import annotations

import json
import re
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
        interval_seconds: float = 1.0,
        similarity_threshold: float = 0.92,
        similarity_window_seconds: float = 3.0,
        max_dimension: int = 1600,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.similarity_threshold = similarity_threshold
        self.similarity_window_seconds = similarity_window_seconds
        self.max_dimension = max_dimension
        self.probe = MediaProbe(ffprobe_path, timeout_seconds=min(timeout_seconds, 60))

    @staticmethod
    def interval_for(duration_seconds: float) -> float:
        """Spotted samples every second before local visual deduplication."""
        return 1.0

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
            "setpts=PTS-STARTPTS,"
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
            "-fps_mode",
            "passthrough",
            "-frame_pts",
            "1",
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
        retained_paths, duplicate_representatives = self._cluster_similar_frames(
            sampled_paths
        )
        frames: list[dict[str, Any]] = []
        retained_set = set(retained_paths)
        timestamps_by_path = {
            path: self._timestamp_for_sample(path, sample_index, interval, duration)
            for sample_index, path in enumerate(sampled_paths)
        }
        similar_timestamps: dict[Path, list[float]] = {
            path: [] for path in retained_paths
        }
        for duplicate, representative in duplicate_representatives.items():
            similar_timestamps[representative].append(timestamps_by_path[duplicate])
        stable_index = 0
        for sample_index, path in enumerate(sampled_paths):
            if path not in retained_set:
                path.unlink(missing_ok=True)
                continue
            stable_index += 1
            timestamp = timestamps_by_path[path]
            frame_id = f"frame-{stable_index:04d}"
            stable_path = frames_dir / f"{frame_id}.jpg"
            path.replace(stable_path)
            frames.append(
                {
                    "id": frame_id,
                    "timestampSec": timestamp,
                    "path": str(stable_path.resolve()),
                    "thumbnailUrl": f"/api/jobs/{job_id}/frames/{stable_path.name}",
                    "width": int(metadata.get("width") or 0),
                    "height": int(metadata.get("height") or 0),
                    "source": "one_second_unique",
                    **(
                        {
                            "similarTimestamps": sorted(similar_timestamps[path])
                        }
                        if similar_timestamps[path]
                        else {}
                    ),
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

    @staticmethod
    def _timestamp_for_sample(
        path: Path,
        fallback_index: int,
        interval: float,
        duration: float,
    ) -> float:
        """Convert ffmpeg's output-frame PTS filename to the video timeline.

        ``-frame_pts 1`` names each image with its filtered output PTS. The fps
        filter uses a time base equal to the requested cadence, so multiplying
        that PTS by the interval preserves the timestamp ffmpeg assigned. The
        list index remains a defensive fallback for unusual image2 builds.
        """
        match = re.fullmatch(r"sample-(\d+)\.jpg", path.name)
        pts = int(match.group(1)) if match else fallback_index
        timestamp = pts * interval
        return round(min(timestamp, duration or timestamp), 3)

    def _remove_similar_frames(self, paths: list[Path]) -> list[Path]:
        retained, _ = self._cluster_similar_frames(paths)
        return retained

    def _cluster_similar_frames(
        self, paths: list[Path]
    ) -> tuple[list[Path], dict[Path, Path]]:
        """Remove near-identical samples before they can consume vision tokens.

        A tiny 16x16 grayscale fingerprint is generated locally with ffmpeg.
        Only consecutive, nearby samples can share a representative. This is
        deliberately not a global nearest-neighbour search: a room or product
        returning later in the video needs a fresh frame and bounding box.

        Even an unchanged shot is refreshed after ``similarity_window_seconds``
        so slowly moving objects do not inherit a stale bounding box forever.
        Skipped samples retain a representative mapping so the AI result still
        includes their one-second timestamps.
        """
        if len(paths) < 2:
            return paths, {}
        hashes = self._perceptual_hashes(paths)
        if len(hashes) != len(paths):
            return paths, {}
        retained: list[Path] = [paths[0]]
        duplicate_representatives: dict[Path, Path] = {}
        bit_count = 16 * 16
        previous_hash = hashes[0]
        representative = paths[0]
        representative_pts = self._sample_pts(paths[0], 0)
        for sample_index, (path, fingerprint) in enumerate(
            zip(paths[1:], hashes[1:], strict=True), start=1
        ):
            similarity = 1.0 - (
                (fingerprint ^ previous_hash).bit_count() / bit_count
            )
            sample_pts = self._sample_pts(path, sample_index)
            elapsed = (sample_pts - representative_pts) * self.interval_seconds
            if (
                similarity >= self.similarity_threshold
                and 0 <= elapsed <= self.similarity_window_seconds
            ):
                duplicate_representatives[path] = representative
            else:
                retained.append(path)
                representative = path
                representative_pts = sample_pts
            previous_hash = fingerprint
        return (retained or paths[:1]), duplicate_representatives

    @staticmethod
    def _sample_pts(path: Path, fallback_index: int) -> int:
        match = re.fullmatch(r"sample-(\d+)\.jpg", path.name)
        return int(match.group(1)) if match else fallback_index

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
