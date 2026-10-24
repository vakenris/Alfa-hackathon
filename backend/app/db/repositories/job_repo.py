from typing import Optional, List
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.models.agent_trace import JobRecord, AgentTraceStep


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_job(self, job_id: str, company_id: int, event_id: int) -> JobRecord:
        job = JobRecord(
            id=job_id,
            company_id=company_id,
            event_id=event_id,
            status="PENDING"
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def get_by_id(self, job_id: str) -> Optional[JobRecord]:
        stmt = (
            select(JobRecord)
            .where(JobRecord.id == job_id)
            .options(selectinload(JobRecord.trace_steps))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(self, job_id: str, status: str, error_message: Optional[str] = None):
        job = await self.get_by_id(job_id)
        if job:
            job.status = status
            if error_message:
                job.error_message = error_message
            await self.session.commit()

    async def add_trace_step(self, job_id: str, step_number: int, step_name: str, description: str) -> AgentTraceStep:
        step = AgentTraceStep(
            job_id=job_id,
            step_number=step_number,
            step_name=step_name,
            description=description
        )
        self.session.add(step)
        await self.session.commit()
        await self.session.refresh(step)
        return step
