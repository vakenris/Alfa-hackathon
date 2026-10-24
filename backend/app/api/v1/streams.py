"""Streams API — dedicated router for all Server-Sent Events (SSE) endpoints.

Extracted from jobs.py to keep streaming concerns isolated and make it easy
to add future real-time channels (e.g. feed refresh, system alerts).

Endpoints:
    GET /streams/jobs/{job_id}
        — SSE stream of agent execution trace steps for a running insight job.
          Replays already-stored steps first, then follows the live event bus.

    GET /streams/feed/{company_id}
        — (stub) SSE channel for live news feed updates for a company.
          Currently sends a single confirmation event; full RSS push can be
          wired here without changing the client contract.
"""

import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_db
from backend.app.core.bus import get_event_bus
from backend.app.core.logging import logger
from backend.app.services.job_service import JobService
from backend.app.schemas.job import JobStatusResponse

router = APIRouter(prefix="/streams", tags=["Streams & SSE"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",     # disable Nginx response buffering
}


# ---------------------------------------------------------------------------
# Job trace stream
# ---------------------------------------------------------------------------

async def _job_trace_generator(job_id: str, db: AsyncSession) -> AsyncGenerator[str, None]:
    """Yield SSE events for an insight job: historical steps first, then live."""
    service = JobService(db)
    job = await service.get_job_status(job_id)

    if not job:
        yield f"event: error\ndata: {json.dumps({'error': 'Job not found'})}\n\n"
        return

    # 1. Replay already-stored trace steps
    for step in job.trace_steps:
        payload = {
            "event": "step",
            "job_id": job_id,
            "step_number": step.step_number,
            "step_name": step.step_name,
            "description": step.description,
            "timestamp": step.created_at.isoformat(),
        }
        yield f"event: step\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 2. If already terminal, send final event and close
    if job.status == "SUCCESS":
        payload = {"event": "completed", "status": "SUCCESS", "job_id": job_id,
                   "insight_id": job.insight_id}
        yield f"event: completed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return
    if job.status == "FAILED":
        payload = {"event": "failed", "status": "FAILED", "job_id": job_id,
                   "error": job.error_message}
        yield f"event: failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return

    # 3. Subscribe to event bus for live updates
    bus = get_event_bus()
    async for message in bus.subscribe(f"job:{job_id}"):
        event_name = message.get("event", "message")
        yield f"event: {event_name}\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        if event_name in ("completed", "failed"):
            break


@router.get("/jobs/{job_id}", summary="SSE stream of agent trace steps for a job")
async def stream_job_trace(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Real-time Server-Sent Events stream of agent execution steps.

    The client receives `step`, `completed` or `failed` events.
    Historical steps are replayed immediately on connect.
    """
    return StreamingResponse(
        _job_trace_generator(job_id, db),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/{job_id}/history", response_model=JobStatusResponse, summary="Static trace step history for job")
@router.get("/jobs/{job_id}/history", response_model=JobStatusResponse, summary="Static trace step history for job", include_in_schema=False)
async def get_job_history(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve trace step history as structured JSON (non-streaming)."""
    service = JobService(db)
    job = await service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Feed refresh stream (stub — ready for RSS push integration)
# ---------------------------------------------------------------------------

async def _feed_refresh_generator(company_id: int) -> AsyncGenerator[str, None]:
    """SSE generator for live feed updates.

    Emits a `connected` event immediately, then forwards any messages
    published to the ``feed:{company_id}`` bus channel.  If no message
    arrives within *max_idle_seconds* a ``heartbeat`` event is sent and
    the stream closes — the client is expected to reconnect (standard SSE
    behaviour).  This keeps the generator finite for tests and avoids
    connection leaks.
    """
    import anyio

    connected_payload = json.dumps({
        "event": "connected",
        "company_id": company_id,
        "message": "Feed stream connected. New items will appear here.",
    }, ensure_ascii=False)
    yield f"event: connected\ndata: {connected_payload}\n\n"

    bus = get_event_bus()
    channel = f"feed:{company_id}"
    max_idle_seconds = 1.0  # send heartbeat and close if no events

    with anyio.move_on_after(max_idle_seconds):
        async for message in bus.subscribe(channel):
            event_name = message.get("event", "feed_update")
            yield f"event: {event_name}\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"

    # Heartbeat / close signal so the client knows to reconnect
    heartbeat = json.dumps({"event": "heartbeat", "company_id": company_id}, ensure_ascii=False)
    yield f"event: heartbeat\ndata: {heartbeat}\n\n"


@router.get("/feed/{company_id}", summary="SSE stream for live feed updates (company)")
async def stream_company_feed(company_id: int):
    """Live Server-Sent Events channel for personalized news feed updates.

    Emits `connected` immediately, then `feed_update` events whenever new
    relevant news articles are published and pushed to the company channel.
    """
    return StreamingResponse(
        _feed_refresh_generator(company_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
