import asyncio

from sqlalchemy import func, select, update

from backend.app.core.bus import get_event_bus
from backend.app.core.logging import logger
from backend.app.db.models.agent_trace import AgentTraceStep, JobRecord
from backend.app.db.session import async_session_factory
from backend.app.db.repositories.company_repo import CompanyRepository
from backend.app.db.repositories.news_repo import NewsRepository
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.ml.interface import MockMLInsightEngine, MLInsightEngineProtocol
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse
from backend.app.worker.config import worker_settings
from backend.app.worker.events import close_bus, publish_event
from backend.app.worker.queue import ClaimedJob, DatabaseJobQueue, utc_now
from backend.app.worker.tracer import AgentTracer


current_ml_engine: MLInsightEngineProtocol = MockMLInsightEngine()


def set_ml_engine(engine: MLInsightEngineProtocol):
    global current_ml_engine
    current_ml_engine = engine


async def run_insight_pipeline(
    job_id: str,
    company_id: int,
    event_id: int,
):
    # The API has already committed PENDING.
    # The external worker polls the database.
    if worker_settings.mode == "database":
        return

    job = await DatabaseJobQueue(
        async_session_factory
    ).claim(job_id, company_id, event_id)

    if job is not None:
        await execute_job(job)


async def _finish(
    session,
    job: ClaimedJob,
    status: str,
    error=None,
):
    result = await session.execute(
        update(JobRecord)
        .where(
            JobRecord.id == job.id,
            JobRecord.status == "PROCESSING",
        )
        .values(
            status=status,
            error_message=error,
            updated_at=utc_now(),
        )
    )
    await session.commit()

    return result.rowcount == 1


async def execute_job(job: ClaimedJob):
    bus = get_event_bus()

    try:
        async with async_session_factory() as session:
            insight_repo = InsightRepository(session)

            try:
                await publish_event(bus, job.id, {
                    "event": "status",
                    "status": "PROCESSING",
                    "job_id": job.id,
                })

                async with asyncio.timeout(
                    worker_settings.timeout_seconds
                ):
                    saved = await insight_repo.get_by_job_id(
                        job.id
                    )

                    if saved is None:
                        company_repo = CompanyRepository(session)

                        company = await company_repo.get_by_id(
                            job.company_id
                        )
                        event = await NewsRepository(
                            session
                        ).get_by_id(job.event_id)

                        if company is None or event is None:
                            raise ValueError(
                                "Company or news event not found"
                            )

                        counterparties = (
                            await company_repo.get_top_counterparties(
                                company.synthetic_u,
                                limit=5,
                            )
                        )

                        company_schema = (
                            CompanyResponse.model_validate(company)
                        )
                        event_schema = (
                            NewsArticleResponse.model_validate(event)
                        )

                        tracer = AgentTracer(
                            job.id,
                            JobRepository(session),
                            bus=bus,
                        )

                        tracer.current_step = (
                            await session.scalar(
                                select(
                                    func.max(
                                        AgentTraceStep.step_number
                                    )
                                ).where(
                                    AgentTraceStep.job_id == job.id
                                )
                            )
                        ) or 0

                        await session.rollback()

                        result = (
                            await current_ml_engine.generate_insight(
                                company=company_schema,
                                event=event_schema,
                                counterparties=counterparties,
                                tracer=tracer,
                            )
                        )

                        saved = await insight_repo.save_insight(
                            job_id=job.id,
                            company_id=job.company_id,
                            event_id=job.event_id,
                            ml_result=result,
                        )

                    insight_id = saved.id

                    completed = await _finish(
                        session,
                        job,
                        "SUCCESS",
                    )

                if completed:
                    await publish_event(bus, job.id, {
                        "event": "completed",
                        "status": "SUCCESS",
                        "job_id": job.id,
                        "insight_id": insight_id,
                    })

            except asyncio.CancelledError:
                await session.rollback()

                saved = await insight_repo.get_by_job_id(
                    job.id
                )

                await _finish(
                    session,
                    job,
                    "SUCCESS" if saved is not None else "FAILED",
                    (
                        None
                        if saved is not None
                        else "Worker stopped during analysis"
                    ),
                )
                raise

            except Exception as error:
                logger.exception("Job %s failed", job.id)
                await session.rollback()

                # Preserve a result committed before a later failure.
                saved = await insight_repo.get_by_job_id(
                    job.id
                )

                if saved is not None:
                    await _finish(
                        session,
                        job,
                        "SUCCESS",
                    )
                    await publish_event(bus, job.id, {
                        "event": "completed",
                        "status": "SUCCESS",
                        "job_id": job.id,
                        "insight_id": saved.id,
                    })
                else:
                    message = (
                        "Analysis timeout"
                        if isinstance(error, TimeoutError)
                        else str(error)[:500]
                    )

                    await _finish(
                        session,
                        job,
                        "FAILED",
                        message,
                    )
                    await publish_event(bus, job.id, {
                        "event": "failed",
                        "status": "FAILED",
                        "job_id": job.id,
                        "error": message,
                    })

    finally:
        await close_bus(bus)