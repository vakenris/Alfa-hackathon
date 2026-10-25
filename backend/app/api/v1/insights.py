from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.services.insight_service import InsightService
from backend.app.schemas.insight import InsightCreateRequest, InsightResponse
from backend.app.schemas.job import JobCreateResponse
from backend.app.services.automatic_insights import ensure_insight_job

router = APIRouter(prefix="/insights", tags=["Insights"])


@router.post("", response_model=JobCreateResponse)
async def create_insight_job(
    request: InsightCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Reuse an existing insight/job, or schedule one missing analysis."""
    return await ensure_insight_job(db, background_tasks, request.company_id, request.event_id)


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
