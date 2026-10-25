"""Shared, idempotent scheduling for feed analysis and explicit insight requests."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from weakref import WeakValueDictionary

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.agent_trace import JobRecord
from backend.app.db.models.news import NewsArticle
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.schemas.job import JobCreateResponse
from backend.app.worker.tasks import run_insight_pipeline

# The deployed backend runs one worker. Share locks across automatic and explicit
# requests; persisted active jobs also prevent duplicates between page refreshes.
_job_locks: WeakValueDictionary = WeakValueDictionary()


async def ensure_insight_job(db: AsyncSession, background_tasks: BackgroundTasks,
                             company_id: int, event_id: int) -> JobCreateResponse:
    key = (company_id, event_id)
    lock = _job_locks.setdefault(key, asyncio.Lock())
    async with lock:
        existing = await InsightRepository(db).get_by_company_and_event(company_id, event_id)
        if existing:
            return JobCreateResponse(job_id=existing.job_id, status="SUCCESS",
                                     message="Insight already generated (retrieved from cache)")
        result = await db.execute(select(JobRecord).where(
            JobRecord.company_id == company_id, JobRecord.event_id == event_id,
            JobRecord.status.in_(["PENDING", "PROCESSING"]),
        ).order_by(JobRecord.created_at.desc()))
        active = result.scalars().first()
        if active:
            updated = active.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated >= datetime.now(timezone.utc) - timedelta(minutes=10):
                return JobCreateResponse(job_id=active.id, status=active.status,
                                         message="Insight generation is already in progress")
            active.status = "FAILED"
            active.error_message = "Analysis interrupted; automatically rescheduled"
            await db.commit()
        job_id = str(uuid.uuid4())
        await JobRepository(db).create_job(job_id, company_id, event_id)
        background_tasks.add_task(run_insight_pipeline, job_id=job_id,
                                  company_id=company_id, event_id=event_id)
        return JobCreateResponse(job_id=job_id, status="PENDING",
                                 message="Insight generation pipeline started")


async def schedule_company_news(db: AsyncSession, background_tasks: BackgroundTasks,
                                company_id: int) -> None:
    # Analyze the entire news corpus, even when the visible feed is paginated.
    event_ids = (await db.execute(select(NewsArticle.id).order_by(NewsArticle.published_at.desc()))).scalars().all()
    for event_id in event_ids:
        await ensure_insight_job(db, background_tasks, company_id, event_id)
