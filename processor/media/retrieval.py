from __future__ import annotations

import subprocess
from pathlib import Path


class RetrievalBlocked(RuntimeError):
    """The platform did not expose downloadable public media."""


class YtDlpRetriever:
    def __init__(
        self,
        ytdlp_path: str = "yt-dlp",
        timeout_seconds: int = 300,
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
            "--max-filesize",
            max_size,
            "--format",
            "bv*[height<=720]+ba/b[height<=720]/b",
            "--merge-output-format",
            "mp4",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,en",
            "--sub-format",
            "vtt",
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
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RetrievalBlocked("yt-dlp is not installed on the processor.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RetrievalBlocked("The platform took too long to return this video.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip().splitlines()
            summary = detail[-1] if detail else "The platform blocked media retrieval."
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
        subtitles = sorted(directory.glob("source*.vtt"))
        return media_path.resolve(), subtitles
