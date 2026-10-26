"""Persisted job queue using existing job_records, on SQLite or PostgreSQL."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select, update

from backend.app.db.models.agent_trace import JobRecord
from backend.app.db.models.insight import Insight


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    company_id: int
    event_id: int


class DatabaseJobQueue:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def claim(
        self,
        job_id=None,
        company_id=None,
        event_id=None,
    ):
        async with self.session_factory() as session:
            statement = select(JobRecord.id).where(
                JobRecord.status == "PENDING"
            )

            if job_id is not None:
                statement = statement.where(
                    JobRecord.id == job_id
                )

            if company_id is not None:
                statement = statement.where(
                    JobRecord.company_id == company_id
                )

            if event_id is not None:
                statement = statement.where(
                    JobRecord.event_id == event_id
                )

            candidate = await session.scalar(
                statement
                .order_by(JobRecord.created_at, JobRecord.id)
                .limit(1)
            )

            if candidate is None:
                return None

            result = await session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == candidate,
                    JobRecord.status == "PENDING",
                )
                .values(
                    status="PROCESSING",
                    error_message=None,
                    updated_at=utc_now(),
                )
                .returning(
                    JobRecord.id,
                    JobRecord.company_id,
                    JobRecord.event_id,
                )
            )

            row = result.first()
            await session.commit()

            return ClaimedJob(*row) if row else None

    async def expire_stale(self):
        # Runtime ограничен 9 минутами.
        # Прерванные задания устаревают через 10 минут.
        cutoff = utc_now() - timedelta(minutes=10)

        async with self.session_factory() as session:
            stale = (
                JobRecord.status == "PROCESSING",
                JobRecord.updated_at < cutoff,
            )

            saved = exists(
                select(Insight.id).where(
                    Insight.job_id == JobRecord.id
                )
            )

            await session.execute(
                update(JobRecord)
                .where(*stale, saved)
                .values(
                    status="SUCCESS",
                    error_message=None,
                    updated_at=utc_now(),
                )
                .execution_options(synchronize_session=False)
            )

            await session.execute(
                update(JobRecord)
                .where(*stale)
                .values(
                    status="FAILED",
                    updated_at=utc_now(),
                    error_message=(
                        "Worker interrupted; request analysis again"
                    ),
                )
                .execution_options(synchronize_session=False)
            )

            await session.commit()