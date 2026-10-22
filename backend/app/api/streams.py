import json
from typing import AsyncGenerator, List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.agent_trace import JobRecord
from backend.app.db.models.insight import Insight
from backend.app.schemas.agent import JobStatusResponse, AgentTraceStepSchema
from backend.app.core.bus import get_event_bus

router = APIRouter(prefix="/streams", tags=["Real-time Agent Streams"])


async def agent_trace_sse_generator(job_id: str, db: AsyncSession) -> AsyncGenerator[str, None]:
    """Yields Server-Sent Events (SSE) representing agent execution trace in real time."""
    stmt = (
        select(JobRecord)
        .where(JobRecord.id == job_id)
        .options(selectinload(JobRecord.trace_steps))
    )
    res = await db.execute(stmt)
    job = res.scalar_one_or_none()

    if not job:
        yield f"event: error\ndata: {json.dumps({'error': 'Job not found'})}\n\n"
        return

    # Find associated insight if already finished
    insight_stmt = select(Insight.id).where(Insight.job_id == job_id)
    insight_id = await db.scalar(insight_stmt)

    # 1. Yield already recorded historical trace steps
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

    # If job already completed
    if job.status == "SUCCESS":
        payload = {"event": "completed", "status": "SUCCESS", "job_id": job_id, "insight_id": insight_id}
        yield f"event: completed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return
    elif job.status == "FAILED":
        payload = {"event": "failed", "status": "FAILED", "job_id": job_id, "error": job.error_message}
        yield f"event: failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return

    # 2. Otherwise listen to live Event Bus
    bus = get_event_bus()
    async for message in bus.subscribe(f"job:{job_id}"):
        event_name = message.get("event", "message")
        yield f"event: {event_name}\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        
        if event_name in ("completed", "failed"):
            break


@router.get("/{job_id}")
async def stream_job_trace(
    job_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Server-Sent Events (SSE) stream delivering real-time agent steps to frontend."""
    return StreamingResponse(
        agent_trace_sse_generator(job_id, db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx proxy buffering for SSE!
        }
    )


@router.get("/{job_id}/history", response_model=JobStatusResponse)
async def get_job_history(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get static status and complete trace history for an insight generation job."""
    stmt = (
        select(JobRecord)
        .where(JobRecord.id == job_id)
        .options(selectinload(JobRecord.trace_steps))
    )
    res = await db.execute(stmt)
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    insight_stmt = select(Insight.id).where(Insight.job_id == job_id)
    insight_id = await db.scalar(insight_stmt)

    trace_steps = [
        AgentTraceStepSchema.model_validate(step)
        for step in job.trace_steps
    ]

    return JobStatusResponse(
        job_id=job.id,
        company_id=job.company_id,
        event_id=job.event_id,
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        trace_steps=trace_steps,
        insight_id=insight_id
    )
