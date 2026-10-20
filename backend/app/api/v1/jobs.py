import json
import asyncio
from typing import AsyncGenerator
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.services.job_service import JobService
from backend.app.schemas.job import JobStatusResponse
from backend.app.core.bus import get_event_bus
from backend.app.core.logging import logger

router = APIRouter(prefix="/jobs", tags=["Jobs & Agent Streams"])


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get current status of insight job and history of completed trace steps."""
    service = JobService(db)
    job = await service.get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


async def job_sse_generator(job_id: str, db: AsyncSession) -> AsyncGenerator[str, None]:
    """Yields Server-Sent Events (SSE) representing agent execution trace in real time."""
    service = JobService(db)
    job = await service.get_job_status(job_id)

    if not job:
        yield f"event: error\ndata: {json.dumps({'error': 'Job not found'})}\n\n"
        return

    # 1. Send all already existing historical trace steps
    for step in job.trace_steps:
        payload = {
            "event": "step",
            "job_id": job_id,
            "step_number": step.step_number,
            "step_name": step.step_name,
            "description": step.description,
            "timestamp": step.created_at.isoformat()
        }
        yield f"event: step\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # If job already finished
    if job.status == "SUCCESS":
        payload = {"event": "completed", "status": "SUCCESS", "job_id": job_id, "insight_id": job.insight_id}
        yield f"event: completed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return
    elif job.status == "FAILED":
        payload = {"event": "failed", "status": "FAILED", "job_id": job_id, "error": job.error_message}
        yield f"event: failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return

    # 2. Otherwise listen to Event Bus for live incoming events
    bus = get_event_bus()
    async for message in bus.subscribe(f"job:{job_id}"):
        event_name = message.get("event", "message")
        yield f"event: {event_name}\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        
        if event_name in ("completed", "failed"):
            break


@router.get("/{job_id}/stream")
async def stream_job_trace(
    job_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Server-Sent Events (SSE) stream delivering real-time agent steps to frontend."""
    return StreamingResponse(
        job_sse_generator(job_id, db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
