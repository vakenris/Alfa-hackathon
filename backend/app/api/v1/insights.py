import uuid
import asyncio
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.services.insight_service import InsightService
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.schemas.insight import InsightCreateRequest, InsightResponse
from backend.app.schemas.job import JobCreateResponse
from backend.app.worker.tasks import run_insight_pipeline

router = APIRouter(prefix="/insights", tags=["Insights"])


@router.post("", response_model=JobCreateResponse)
async def create_insight_job(
    request: InsightCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Trigger personalized AI agent insight generation for a company and news event."""
    insight_service = InsightService(db)
    
    # 1. Idempotency / Cache check: if already generated, return existing
    existing = await insight_service.get_by_company_and_event(
        company_id=request.company_id,
        event_id=request.event_id
    )
    if existing:
        return JobCreateResponse(
            job_id=existing.job_id,
            status="SUCCESS",
            message="Insight already generated (retrieved from cache)"
        )

    # 2. Create new Job record
    job_id = str(uuid.uuid4())
    job_repo = JobRepository(db)
    await job_repo.create_job(job_id=job_id, company_id=request.company_id, event_id=request.event_id)

    # 3. Dispatch to worker pipeline (async background task)
    background_tasks.add_task(
        run_insight_pipeline,
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
    service = InsightService(db)
    insight = await service.get_insight(insight_id)
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")
    return insight
