from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.schemas.job import JobStatusResponse, TraceStepSchema


class JobService:
    def __init__(self, session: AsyncSession):
        self.job_repo = JobRepository(session)
        self.insight_repo = InsightRepository(session)

    async def get_job_status(self, job_id: str) -> Optional[JobStatusResponse]:
        job = await self.job_repo.get_by_id(job_id)
        if not job:
            return None

        insight = await self.insight_repo.get_by_job_id(job_id)
        insight_id = insight.id if insight else None

        trace_steps = [
            TraceStepSchema.model_validate(step)
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
