from __future__ import annotations

import subprocess
from pathlib import Path


class RetrievalBlocked(RuntimeError):
    """The platform did not expose downloadable public media."""


class YtDlpRetriever:
    def __init__(
        self,
        ytdlp_path: str = "yt-dlp",
        timeout_seconds: int = 0,
        max_download_bytes: int = 2 * 1024**3,
    ) -> None:
        self.ytdlp_path = ytdlp_path
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes

    def retrieve(self, url: str, directory: Path) -> tuple[Path, list[Path]]:
        output_template = directory / "source.%(ext)s"
        max_size = str(self.max_download_bytes)
        command = [
            self.ytdlp_path,
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--socket-timeout",
            "30",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--concurrent-fragments",
            "4",
            "--js-runtimes",
            "node",
            "--max-filesize",
            max_size,
            "--format",
            "bv*[height<=720]+ba/b[height<=720]/b",
            "--merge-output-format",
            "mp4",
            "--write-info-json",
            "--output",
            str(output_template),
            "--print",
            "after_move:filepath",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                # A wall-clock timeout incorrectly rejects large or slow videos even
                # while bytes are still arriving. yt-dlp's socket timeout and retry
                # settings above handle actual stalled network requests instead.
                timeout=self.timeout_seconds or None,
            )
        except FileNotFoundError as exc:
            raise RetrievalBlocked("yt-dlp is not installed on the processor.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RetrievalBlocked("The platform took too long to return this video.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip().splitlines()
            summary = detail[-1] if detail else "The platform blocked media retrieval."
            if "403" in summary or "forbidden" in summary.lower():
                summary = (
                    "The video platform blocked the direct download. "
                    "Upload the video file to continue the same analysis locally."
                )
            raise RetrievalBlocked(summary[:500]) from exc

        printed = [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
        media_path = next((path for path in reversed(printed) if path.exists()), None)
        if media_path is None:
            candidates = [
                path
                for path in directory.glob("source.*")
                if path.suffix.lower() not in {".vtt", ".part", ".ytdl", ".json"}
            ]
            media_path = max(candidates, key=lambda path: path.stat().st_size, default=None)
        if not media_path or not media_path.exists():
            raise RetrievalBlocked("The platform did not return a playable video file.")
        self._retrieve_subtitles(url, output_template)
        subtitles = sorted(directory.glob("source*.vtt"))
        return media_path.resolve(), subtitles

    def _retrieve_subtitles(self, url: str, output_template: Path) -> None:
        """Captions improve recognition, but rate limits must not block the video."""
        command = [
            self.ytdlp_path,
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--js-runtimes",
            "node",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,en",
            "--sub-format",
            "vtt",
            "--output",
            str(output_template),
            url,
        ]
        try:
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=min(self.timeout_seconds, 90) if self.timeout_seconds else 90,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
