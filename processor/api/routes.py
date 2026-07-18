from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, HttpUrl

from processor.config import Settings
from processor.media import FrameExtractor, MediaProbe, RetrievalBlocked, YtDlpRetriever, classify_url
from processor.media.extract import ExtractionError
from processor.media.probe import ProbeError
from processor.storage import JobStore


class CreateJobRequest(BaseModel):
    url: HttpUrl
    focus: str | None = Field(default=None, max_length=500)


_RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)$")


def create_router(store: JobStore, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/jobs", tags=["jobs"])
    workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="spotted-media")

    def process_url(job_id: str, url: str, focus: str | None) -> None:
        retriever = YtDlpRetriever(
            settings.ytdlp_path,
            settings.download_timeout_seconds,
            settings.max_download_bytes,
        )
        try:
            store.emit(job_id, "retrieving_video", message="Retrieving public video")
            job = store.get(job_id)
            source_path, subtitle_paths = retriever.retrieve(url, job.directory)
            _process_media(job_id, source_path, subtitle_paths, focus)
        except RetrievalBlocked as exc:
            error = {
                "code": "retrieval_blocked",
                "message": str(exc),
                "uploadFallback": True,
            }
            store.update(job_id, error=error)
            store.emit(job_id, "retrieval_blocked", **error)
        except Exception as exc:  # background workers must always terminate the job
            _fail_job(store, job_id, exc)

    def _process_media(
        job_id: str,
        source_path: Path,
        subtitle_paths: list[Path],
        focus: str | None,
    ) -> None:
        try:
            probe = MediaProbe(settings.ffprobe_path, settings.command_timeout_seconds)
            metadata = probe.inspect(source_path)
            media_type = mimetypes.guess_type(source_path.name)[0] or "video/mp4"
            metadata["captions"] = [str(path) for path in subtitle_paths]
            store.update(
                job_id,
                source_path=source_path,
                media_type=media_type,
                metadata=metadata,
                error=None,
            )
            store.emit(
                job_id,
                "extracting_frames",
                message="Extracting scenes and interval frames",
                durationSec=metadata.get("durationSec"),
            )
            extractor = FrameExtractor(
                settings.ffmpeg_path,
                settings.ffprobe_path,
                settings.command_timeout_seconds,
            )
            job = store.get(job_id)
            frames, manifest_path = extractor.extract(
                source_path,
                job.directory,
                job_id=job_id,
                metadata=metadata,
            )
            _add_manifest_context(
                manifest_path,
                source_url=store.get(job_id).source_url,
                subtitle_paths=subtitle_paths,
                search_focus=focus,
            )
            store.update(job_id, frames=frames)
            store.emit(
                job_id,
                "analyzing_frame",
                message="Frames ready for product analysis",
                frameCount=len(frames),
                manifestPath=str(manifest_path),
            )
            # AI/search attaches at this boundary. Until configured, media-only jobs
            # finish with an empty findings array rather than fabricating results.
            if not _try_ai_handoff(store, job_id, manifest_path):
                store.emit(
                    job_id,
                    "completed",
                    message="Media processing completed",
                    frameCount=len(frames),
                    findings=[],
                )
        except (ProbeError, ExtractionError) as exc:
            _fail_job(store, job_id, exc)
        except Exception as exc:
            _fail_job(store, job_id, exc)

    @router.post("")
    async def create_job(payload: CreateJobRequest) -> dict:
        url = str(payload.url)
        try:
            platform = classify_url(url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        job = store.create(source_url=url, platform=platform)
        workers.submit(process_url, job.id, url, _clean_focus(payload.focus))
        return job.snapshot()

    @router.post("/upload")
    async def upload_job(
        file: UploadFile = File(...),
        focus: str | None = Form(default=None, max_length=500),
    ) -> dict:
        if file.content_type and not (
            file.content_type.startswith("video/")
            or file.content_type == "application/octet-stream"
        ):
            raise HTTPException(status_code=415, detail="Upload a video file.")
        suffix = Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
        if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            raise HTTPException(status_code=415, detail="Unsupported video format.")
        job = store.create(platform="upload")
        destination = job.directory / f"source{suffix}"
        try:
            await _save_upload(file, destination, settings.max_upload_bytes)
        except ValueError as exc:
            store.delete(job.id)
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        finally:
            await file.close()
        store.emit(job.id, "retrieving_video", message="Upload received")
        workers.submit(
            _process_media,
            job.id,
            destination.resolve(),
            [],
            _clean_focus(focus),
        )
        return store.get(job.id).snapshot()

    @router.get("/{job_id}")
    async def get_job(job_id: str) -> dict:
        return _get_job_or_404(store, job_id).snapshot()

    @router.get("/{job_id}/events")
    async def job_events(request: Request, job_id: str) -> StreamingResponse:
        _get_job_or_404(store, job_id)
        last_event_id = request.headers.get("last-event-id", "0")
        try:
            after = max(int(last_event_id), 0)
        except ValueError:
            after = 0

        async def stream() -> AsyncIterator[str]:
            sequence = after
            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    events = store.events_after(job_id, sequence)
                    job = store.get(job_id)
                except KeyError:
                    break
                for event in events:
                    sequence = event.sequence
                    payload = json.dumps(event.as_dict(), separators=(",", ":"))
                    yield f"id: {sequence}\nevent: {event.type}\ndata: {payload}\n\n"
                    idle_ticks = 0
                if job.status in {"completed", "failed", "retrieval_blocked"} and not events:
                    break
                idle_ticks += 1
                if idle_ticks >= 60:
                    yield ": keep-alive\n\n"
                    idle_ticks = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/{job_id}/media")
    async def get_media(request: Request, job_id: str) -> Response:
        job = _get_job_or_404(store, job_id)
        if not job.source_path or not job.source_path.exists():
            raise HTTPException(status_code=404, detail="Media is not ready.")
        return _range_response(request, job.source_path, job.media_type)

    @router.get("/{job_id}/frames/{filename}")
    async def get_frame(job_id: str, filename: str) -> FileResponse:
        job = _get_job_or_404(store, job_id)
        if not re.fullmatch(r"frame-\d{4,8}\.jpg", filename):
            raise HTTPException(status_code=404, detail="Frame not found.")
        path = job.directory / "frames" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Frame not found.")
        return FileResponse(path, media_type="image/jpeg")

    @router.delete("/{job_id}", status_code=204)
    async def delete_job(job_id: str) -> Response:
        if not store.delete(job_id):
            raise HTTPException(status_code=404, detail="Job not found.")
        return Response(status_code=204)

    return router


async def _save_upload(file: UploadFile, destination: Path, maximum: int) -> None:
    total = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise ValueError("Video exceeds the configured upload limit.")
            output.write(chunk)


def _get_job_or_404(store: JobStore, job_id: str):
    try:
        return store.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found.") from exc


def _fail_job(store: JobStore, job_id: str, exc: Exception) -> None:
    error = {"code": "processing_failed", "message": str(exc)}
    try:
        store.update(job_id, error=error)
        store.emit(job_id, "failed", **error)
    except KeyError:
        pass


def _try_ai_handoff(store: JobStore, job_id: str, manifest_path: Path) -> bool:
    """Invoke the optional AI pipeline without making media depend on it."""
    try:
        from processor.ai import analyze_and_enrich  # type: ignore[attr-defined]
    except (ImportError, ModuleNotFoundError):
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def emit(event_type: str, payload: dict) -> None:
        store.emit(job_id, event_type, **payload)

    findings = analyze_and_enrich(manifest, event_callback=emit)
    serialized = [finding.to_dict() for finding in findings]
    store.update(job_id, findings=serialized)
    store.emit(
        job_id,
        "completed",
        message="Product analysis completed",
        frameCount=len(manifest.get("frames", [])),
        findingCount=len(serialized),
        findings=serialized,
    )
    return True


def _add_manifest_context(
    manifest_path: Path,
    *,
    source_url: str | None,
    subtitle_paths: list[Path],
    search_focus: str | None,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if source_url:
        manifest["sourceUrl"] = source_url
    if search_focus:
        manifest["searchFocus"] = search_focus
    transcript = _read_vtt_transcript(subtitle_paths)
    if transcript:
        manifest["transcript"] = transcript
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _clean_focus(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _read_vtt_transcript(paths: list[Path], *, limit: int = 50_000) -> str:
    lines: list[str] = []
    previous = ""
    for path in paths:
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw_line in raw_lines:
            line = re.sub(r"<[^>]+>", "", raw_line).strip()
            if (
                not line
                or line == "WEBVTT"
                or "-->" in line
                or line.startswith(("NOTE", "Kind:", "Language:"))
                or line.isdigit()
                or line == previous
            ):
                continue
            lines.append(line)
            previous = line
            if sum(len(item) + 1 for item in lines) >= limit:
                return "\n".join(lines)[:limit]
    return "\n".join(lines)[:limit]


def _range_response(request: Request, path: Path, media_type: str) -> Response:
    size = path.stat().st_size
    header = request.headers.get("range")
    common_headers = {"Accept-Ranges": "bytes"}
    if not header:
        return FileResponse(path, media_type=media_type, headers=common_headers)
    match = _RANGE_PATTERN.fullmatch(header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start = max(size - suffix_length, 0)
        end = size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    length = end - start + 1

    def body():
        remaining = length
        with path.open("rb") as source:
            source.seek(start)
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        **common_headers,
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(length),
    }
    return StreamingResponse(body(), status_code=206, media_type=media_type, headers=headers)
