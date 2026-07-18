from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from processor.api import create_router
from processor.config import settings
from processor.storage import JobStore


job_store = JobStore(settings.job_root, settings.job_ttl_seconds)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        job_store.cleanup_expired()


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(
    title="Spotted Processor",
    version="0.1.0",
    description="Local media ingestion and timestamped frame extraction for Spotted.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(create_router(job_store, settings))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "spotted-processor"}
