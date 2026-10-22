import uuid
import asyncio
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.insight import Insight
from backend.app.db.models.agent_trace import JobRecord
from backend.app.schemas.insight import InsightCreateRequest, InsightResponse
from backend.app.schemas.agent import JobCreateResponse

router = APIRouter(prefix="/insights", tags=["Insights"])


# Fallback runner when worker is in-process
async def execute_in_process_job(job_id: str, company_id: int, event_id: int):
    # Delegate to worker if available
    try:
        from backend.app.worker.tasks import run_insight_pipeline
        await run_insight_pipeline(job_id=job_id, company_id=company_id, event_id=event_id)
    except ImportError:
        pass


@router.post("", response_model=JobCreateResponse)
async def create_insight_job(
    request: InsightCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Trigger personalized AI agent insight generation for a company and news event."""
    # 1. Idempotency / Cache check: if already generated, return existing
    existing_stmt = (
        select(Insight)
        .where(Insight.company_id == request.company_id, Insight.event_id == request.event_id)
        .order_by(Insight.id.desc())
    )
    res = await db.execute(existing_stmt)
    existing = res.scalar_one_or_none()
    if existing:
        return JobCreateResponse(
            job_id=existing.job_id,
            status="SUCCESS",
            message="Insight already generated (retrieved from cache)"
        )

    # 2. Create new Job record
    job_id = str(uuid.uuid4())
    job = JobRecord(
        id=job_id,
        company_id=request.company_id,
        event_id=request.event_id,
        status="PENDING"
    )
    db.add(job)
    await db.commit()

    # 3. Dispatch to worker pipeline (async background task)
    background_tasks.add_task(
        execute_in_process_job,
        job_id=job_id,
        company_id=request.company_id,
        event_id=request.event_id
    )

    return JobCreateResponse(
        job_id=job_id,
        status="PENDING",
        message="Insight generation pipeline started"
    )


@router.get("/{insight_id}", response_model=InsightResponse)
async def get_insight_by_id(
    insight_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Retrieve full insight by ID, including facts, consequences, bank view, AI view and evidences."""
    stmt = (
        select(Insight)
        .where(Insight.id == insight_id)
        .options(selectinload(Insight.evidences))
    )
    res = await db.execute(stmt)
    insight = res.scalar_one_or_none()
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")
    return InsightResponse.model_validate(insight)


@router.get("/company/{company_id}", response_model=List[InsightResponse])
async def list_insights_for_company(
    company_id: int,
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """List all generated insights for a specific company."""
    stmt = (
        select(Insight)
        .where(Insight.company_id == company_id)
        .order_by(Insight.id.desc())
        .limit(limit)
        .options(selectinload(Insight.evidences))
    )
    res = await db.execute(stmt)
    insights = res.scalars().all()
    return [InsightResponse.model_validate(i) for i in insights]
