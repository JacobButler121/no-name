from __future__ import annotations

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

    from processor.api.routes import create_router
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
                )
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["jobId"]
            snapshot = self._wait_for_terminal(job_id)

        self.assertEqual(snapshot["status"], "completed")
        self.assertGreaterEqual(len(snapshot["frames"]), 2)
        thumbnail = snapshot["frames"][0]["thumbnailUrl"]
        self.assertEqual(self.client.get(thumbnail).status_code, 200)

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
