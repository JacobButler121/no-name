from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from processor.media.extract import FrameExtractor
from processor.media.platforms import classify_url
from processor.media.probe import MediaProbe
from processor.storage import JobStore


class PlatformTests(unittest.TestCase):
    def test_supported_urls(self) -> None:
        self.assertEqual(classify_url("https://youtu.be/example"), "youtube")
        self.assertEqual(classify_url("https://www.youtube.com/watch?v=example"), "youtube")
        self.assertEqual(classify_url("https://vm.tiktok.com/example"), "tiktok")
        self.assertEqual(classify_url("https://www.instagram.com/reel/example/"), "instagram")

    def test_rejects_lookalike_and_credentials(self) -> None:
        with self.assertRaises(ValueError):
            classify_url("https://youtube.com.attacker.example/video")
        with self.assertRaises(ValueError):
            classify_url("https://user:password@youtube.com/video")


class JobStoreTests(unittest.TestCase):
    def test_lifecycle_events_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary), ttl_seconds=1)
            job = store.create(source_url="https://youtu.be/example", platform="youtube")
            event = store.emit(job.id, "retrieving_video", message="working")
            self.assertEqual(event.sequence, 1)
            self.assertEqual(store.get(job.id).status, "retrieving_video")
            state = json.loads((job.directory / "job.json").read_text())
            self.assertEqual(state["jobId"], job.id)
            store.get(job.id).updated_at = time.time() - 10
            self.assertEqual(store.cleanup_expired(), [job.id])
            self.assertFalse(job.directory.exists())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class MediaToolTests(unittest.TestCase):
    def test_probe_and_extract_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "fixture.mp4"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=12:duration=5",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
                timeout=30,
            )
            probe = MediaProbe(shutil.which("ffprobe") or "ffprobe")
            metadata = probe.inspect(video)
            self.assertAlmostEqual(metadata["durationSec"], 5.0, delta=0.25)
            self.assertEqual(metadata["width"], 320)
            extractor = FrameExtractor(
                shutil.which("ffmpeg") or "ffmpeg",
                shutil.which("ffprobe") or "ffprobe",
            )
            frames, manifest_path = extractor.extract(
                video,
                root,
                job_id="test-job",
                metadata=metadata,
            )
            self.assertGreaterEqual(len(frames), 2)
            self.assertTrue(all(Path(frame["path"]).exists() for frame in frames))
            self.assertEqual(frames[0]["timestampSec"], 0.0)
            self.assertEqual(frames[0]["thumbnailUrl"], "/api/jobs/test-job/frames/frame-0001.jpg")
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(manifest["frames"], frames)


if __name__ == "__main__":
    unittest.main()
