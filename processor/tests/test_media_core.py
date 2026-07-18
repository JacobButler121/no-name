from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from processor.media.extract import FrameExtractor
from processor.media.platforms import classify_url
from processor.media.probe import MediaProbe
from processor.media.retrieval import YtDlpRetriever
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


class RetrieverTests(unittest.TestCase):
    def test_caption_failure_does_not_discard_downloaded_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            video.write_bytes(b"video")
            completed = subprocess.CompletedProcess(
                args=["yt-dlp"], returncode=0, stdout=f"{video}\n", stderr=""
            )
            captions_blocked = subprocess.CompletedProcess(
                args=["yt-dlp"], returncode=1, stdout="", stderr="HTTP Error 429"
            )
            with patch(
                "processor.media.retrieval.subprocess.run",
                side_effect=[completed, captions_blocked],
            ) as run:
                media, subtitles = YtDlpRetriever("yt-dlp").retrieve(
                    "https://youtu.be/example", root
                )

            self.assertEqual(media, video.resolve())
            self.assertEqual(subtitles, [])
            self.assertEqual(run.call_count, 2)
            self.assertIn("--js-runtimes", run.call_args_list[0].args[0])
            self.assertIn("--socket-timeout", run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[0].kwargs["timeout"], None)
            self.assertEqual(run.call_args_list[1].kwargs["timeout"], 90)


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
                    "testsrc2=size=320x180:rate=12:duration=11",
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
            self.assertAlmostEqual(metadata["durationSec"], 11.0, delta=0.25)
            self.assertEqual(metadata["width"], 320)
            extractor = FrameExtractor(
                shutil.which("ffmpeg") or "ffmpeg",
                shutil.which("ffprobe") or "ffprobe",
                similarity_threshold=1.1,
            )
            frames, manifest_path = extractor.extract(
                video,
                root,
                job_id="test-job",
                metadata=metadata,
            )
            self.assertEqual(
                [frame["timestampSec"] for frame in frames],
                [float(second) for second in range(11)],
            )
            self.assertTrue(all(Path(frame["path"]).exists() for frame in frames))
            self.assertEqual(frames[0]["timestampSec"], 0.0)
            self.assertEqual(frames[0]["thumbnailUrl"], "/api/jobs/test-job/frames/frame-0001.jpg")
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(manifest["intervalSec"], 1.0)
            self.assertEqual(manifest["sampledFrameCount"], 11)
            self.assertEqual(manifest["frames"], frames)

    def test_visually_identical_samples_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "static.mp4"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:size=320x180:rate=12:duration=11",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
                timeout=30,
            )
            metadata = MediaProbe(shutil.which("ffprobe") or "ffprobe").inspect(video)
            extractor = FrameExtractor(
                shutil.which("ffmpeg") or "ffmpeg",
                shutil.which("ffprobe") or "ffprobe",
            )
            frames, manifest_path = extractor.extract(
                video, root, job_id="static-job", metadata=metadata
            )
            self.assertEqual(
                [frame["timestampSec"] for frame in frames],
                [0.0, 4.0, 8.0],
            )
            self.assertEqual(
                frames[0]["similarTimestamps"],
                [float(second) for second in range(1, 4)],
            )
            self.assertEqual(
                frames[1]["similarTimestamps"],
                [float(second) for second in range(5, 8)],
            )
            self.assertEqual(
                frames[2]["similarTimestamps"],
                [9.0, 10.0],
            )
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["sampledFrameCount"], 11)
            self.assertEqual(manifest["skippedSimilarFrames"], 8)

    def test_later_similar_scene_is_not_deduplicated_globally(self) -> None:
        paths = [
            Path("/tmp/sample-00000000.jpg"),
            Path("/tmp/sample-00000001.jpg"),
            Path("/tmp/sample-00000002.jpg"),
            Path("/tmp/sample-00000003.jpg"),
        ]
        extractor = FrameExtractor(similarity_threshold=0.92)
        # A, A, B, A: only the consecutive A frame may collapse. The final A
        # returns after another scene and therefore needs a new bounding box.
        with patch.object(
            extractor,
            "_perceptual_hashes",
            return_value=[0b0000, 0b0000, (1 << 256) - 1, 0b0000],
        ):
            retained, duplicates = extractor._cluster_similar_frames(paths)

        self.assertEqual(retained, [paths[0], paths[2], paths[3]])
        self.assertEqual(duplicates, {paths[1]: paths[0]})

    def test_timestamp_uses_frame_pts_filename(self) -> None:
        sample = Path("/tmp/sample-00000007.jpg")
        self.assertEqual(
            FrameExtractor._timestamp_for_sample(sample, 99, 1.0, 20.0),
            7.0,
        )
        self.assertEqual(
            FrameExtractor._timestamp_for_sample(sample, 99, 0.5, 20.0),
            3.5,
        )


if __name__ == "__main__":
    unittest.main()
