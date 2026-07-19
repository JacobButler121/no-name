from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from processor.api.routes import _read_source_metadata, _read_vtt_segments, create_router
    from processor.config import Settings
    from processor.storage import JobStore

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@unittest.skipUnless(
    HAS_FASTAPI and shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FastAPI and ffmpeg are required",
)
class MediaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.video = root / "fixture.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=12:duration=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(self.video),
            ],
            check=True,
            timeout=30,
        )
        self.settings = Settings(
            job_root=root / "jobs",
            ffmpeg_path=shutil.which("ffmpeg") or "ffmpeg",
            ffprobe_path=shutil.which("ffprobe") or "ffprobe",
            ytdlp_path=shutil.which("yt-dlp") or "yt-dlp",
        )
        self.store = JobStore(self.settings.job_root, ttl_seconds=3600)
        app = FastAPI()
        app.include_router(create_router(self.store, self.settings))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_upload_snapshot_frames_range_and_delete(self) -> None:
        with patch("processor.api.routes._try_ai_handoff", return_value=False):
            with self.video.open("rb") as source:
                response = self.client.post(
                    "/api/jobs/upload",
                    files={"file": ("fixture.mp4", source, "video/mp4")},
                    data={"focus": "  Find all   the lamps  "},
                )
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["jobId"]
            snapshot = self._wait_for_terminal(job_id)

        self.assertEqual(snapshot["status"], "completed")
        self.assertGreaterEqual(len(snapshot["frames"]), 1)
        manifest = json.loads(
            (self.store.get(job_id).directory / "frames" / "manifest.json").read_text()
        )
        self.assertEqual(manifest["searchFocus"], "Find all the lamps")
        thumbnail = snapshot["frames"][0]["thumbnailUrl"]
        self.assertEqual(self.client.get(thumbnail).status_code, 200)
        crop_dir = self.store.get(job_id).directory / "frames" / "crops"
        crop_dir.mkdir(parents=True, exist_ok=True)
        crop_name = "frame-0001-100-200-300-400.jpg"
        (crop_dir / crop_name).write_bytes(b"temporary-crop")
        crop_response = self.client.get(f"/api/jobs/{job_id}/crops/{crop_name}")
        self.assertEqual(crop_response.status_code, 200)
        self.assertEqual(crop_response.content, b"temporary-crop")

        ranged = self.client.get(
            f"/api/jobs/{job_id}/media", headers={"Range": "bytes=0-31"}
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(len(ranged.content), 32)
        self.assertTrue(ranged.headers["content-range"].startswith("bytes 0-31/"))

        deleted = self.client.delete(f"/api/jobs/{job_id}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)

    def test_rejects_unsupported_link(self) -> None:
        response = self.client.post(
            "/api/jobs", json={"url": "https://example.com/video.mp4"}
        )
        self.assertEqual(response.status_code, 422)

    def test_caption_parser_preserves_timestamps(self) -> None:
        captions = Path(self.temporary.name) / "captions.vtt"
        captions.write_text(
            "WEBVTT\n\n00:00:02.000 --> 00:00:05.000\nThis is the reading lamp.\n",
            encoding="utf-8",
        )
        self.assertEqual(
            _read_vtt_segments([captions]),
            [{"startSec": 2.0, "endSec": 5.0, "text": "This is the reading lamp."}],
        )

    def test_source_metadata_reads_ytdlp_title_and_channel(self) -> None:
        metadata = Path(self.temporary.name) / "source.info.json"
        metadata.write_text(
            json.dumps({"title": "Design Trends", "channel": "Studio McGee"}),
            encoding="utf-8",
        )
        self.assertEqual(
            _read_source_metadata(Path(self.temporary.name)),
            {"title": "Design Trends", "channel": "Studio McGee"},
        )

    def _wait_for_terminal(self, job_id: str) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            snapshot = response.json()
            if snapshot["status"] in {"completed", "failed", "retrieval_blocked"}:
                return snapshot
            time.sleep(0.05)
        self.fail("media job did not reach a terminal status")


if __name__ == "__main__":
    unittest.main()
