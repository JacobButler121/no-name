from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _binary(name: str, env_name: str) -> str:
    configured = os.environ.get(env_name)
    if configured:
        return configured
    return shutil.which(name) or name


def _environment_binary(name: str, env_name: str) -> str:
    """Prefer tools installed beside the active Python interpreter."""
    configured = os.environ.get(env_name)
    if configured:
        return configured
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name) or name


def _origins() -> tuple[str, ...]:
    raw = os.environ.get(
        "SPOTTED_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return tuple(value.strip() for value in raw.split(",") if value.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    job_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "SPOTTED_JOB_ROOT",
                str(Path(tempfile.gettempdir()) / "spotted-jobs"),
            )
        )
    )
    job_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("SPOTTED_JOB_TTL_SECONDS", "3600"))
    )
    cleanup_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SPOTTED_CLEANUP_INTERVAL_SECONDS", "60")
        )
    )
    max_upload_bytes: int = field(
        default_factory=lambda: int(
            os.environ.get("SPOTTED_MAX_UPLOAD_BYTES", str(2 * 1024**3))
        )
    )
    max_download_bytes: int = field(
        default_factory=lambda: int(
            os.environ.get("SPOTTED_MAX_DOWNLOAD_BYTES", str(2 * 1024**3))
        )
    )
    download_timeout_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SPOTTED_DOWNLOAD_TIMEOUT_SECONDS", "300")
        )
    )
    command_timeout_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SPOTTED_COMMAND_TIMEOUT_SECONDS", "300")
        )
    )
    ffmpeg_path: str = field(default_factory=lambda: _binary("ffmpeg", "FFMPEG_PATH"))
    ffprobe_path: str = field(
        default_factory=lambda: _binary("ffprobe", "FFPROBE_PATH")
    )
    ytdlp_path: str = field(
        default_factory=lambda: _environment_binary("yt-dlp", "YTDLP_PATH")
    )
    cors_origins: tuple[str, ...] = field(default_factory=_origins)


settings = Settings()
