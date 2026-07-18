from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "retrieval_blocked"}


@dataclass(slots=True)
class JobEvent:
    sequence: int
    type: str
    data: dict[str, Any]
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.type,
            "createdAt": self.created_at,
            **self.data,
        }


@dataclass(slots=True)
class JobRecord:
    id: str
    directory: Path
    source_url: str | None = None
    platform: str = "upload"
    status: str = "created"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    source_path: Path | None = None
    media_type: str = "video/mp4"
    metadata: dict[str, Any] = field(default_factory=dict)
    frames: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    events: list[JobEvent] = field(default_factory=list)
    next_sequence: int = 1

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "jobId": self.id,
            "platform": self.platform,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "metadata": self.metadata,
            "frames": self.frames,
            "findings": self.findings,
            "mediaUrl": f"/api/jobs/{self.id}/media" if self.source_path else None,
            "eventsUrl": f"/api/jobs/{self.id}/events",
        }
        if self.error:
            result["error"] = self.error
        return result


class JobStore:
    """Thread-safe in-memory index backed by an isolated directory per job."""

    def __init__(self, root: Path, ttl_seconds: int = 3600) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()

    def create(self, *, source_url: str | None = None, platform: str = "upload") -> JobRecord:
        with self._lock:
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            job = JobRecord(
                id=job_id,
                directory=directory,
                source_url=source_url,
                platform=platform,
            )
            self._jobs[job_id] = job
            self._persist(job)
            return job

    def get(self, job_id: str, *, touch: bool = True) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if touch:
                job.updated_at = time.time()
            return job

    def update(self, job_id: str, **values: Any) -> JobRecord:
        with self._lock:
            job = self.get(job_id, touch=False)
            for key, value in values.items():
                if not hasattr(job, key):
                    raise AttributeError(f"Unknown job field: {key}")
                setattr(job, key, value)
            job.updated_at = time.time()
            self._persist(job)
            return job

    def emit(self, job_id: str, event_type: str, **data: Any) -> JobEvent:
        with self._lock:
            job = self.get(job_id, touch=False)
            event = JobEvent(job.next_sequence, event_type, data)
            job.next_sequence += 1
            job.events.append(event)
            job.updated_at = time.time()
            if event_type in {
                "retrieving_video",
                "extracting_frames",
                "analyzing_frame",
                "merging_duplicates",
                "searching_retailers",
                "completed",
                "failed",
                "retrieval_blocked",
            }:
                job.status = event_type
            self._persist(job)
            return event

    def events_after(self, job_id: str, sequence: int) -> list[JobEvent]:
        with self._lock:
            job = self.get(job_id)
            return [event for event in job.events if event.sequence > sequence]

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        shutil.rmtree(job.directory, ignore_errors=True)
        return True

    def cleanup_expired(self, *, now: float | None = None) -> list[str]:
        current = now if now is not None else time.time()
        with self._lock:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if current - job.updated_at >= self.ttl_seconds
            ]
        for job_id in expired:
            self.delete(job_id)
        return expired

    def _persist(self, job: JobRecord) -> None:
        state_path = job.directory / "job.json"
        temporary = job.directory / ".job.json.tmp"
        temporary.write_text(json.dumps(job.snapshot(), indent=2), encoding="utf-8")
        temporary.replace(state_path)
