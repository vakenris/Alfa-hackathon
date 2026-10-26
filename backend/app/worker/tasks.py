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
from backend.app.agent.companion import companion_store
from backend.app.core.config import settings
from backend.app.worker.config import worker_settings
from backend.app.worker.events import close_bus, publish_event
from backend.app.worker.queue import ClaimedJob, DatabaseJobQueue, utc_now
from backend.app.worker.tracer import AgentTracer


def create_default_ml_engine() -> MLInsightEngineProtocol:
    """Instantiates production RuntimeAgentEngine with budget guard, or fallback mock."""
    if settings.LLM_FALLBACK_TO_MOCK and settings.LLM_API_KEY == "mock-key":
        return MockMLInsightEngine()

    try:
        from backend.app.agent.runtime import RuntimeAgentEngine
        from backend.app.agent.backend_source import BackendAnalyticsSource
        from backend.app.agent.config import AgentConfig
        from backend.app.llm.client import ChatCompletionsConfig
        from backend.app.llm.budget_guard import BudgetedChatCompletionsClient
        from backend.app.services.budget_service import get_budget_guard

        def make_client():
            cfg = ChatCompletionsConfig(
                api_url=settings.LLM_API_BASE_URL,
                model="gpt-4o-mini",
                auth_header="Authorization",
                auth_value=f"Bearer {settings.LLM_API_KEY}",
                timeout_seconds=30.0,
                max_output_tokens=2048,
            )
            return BudgetedChatCompletionsClient(
                config=cfg,
                budget_guard=get_budget_guard(),
                input_token_bound=lambda prompt: max(len(prompt) // 2, 50),
            )

        def make_source():
            return BackendAnalyticsSource.from_repository_factory(
                async_session_factory,
                provenance_resolver=lambda row: {
                    "provenance_status": "verified" if row.is_bank_analytics else "unverified",
                    "source_document_id": f"bank-art-{row.id}",
                    "source_page": 1,
                    "source_hash": f"h-{row.id}",
                },
            )

        return RuntimeAgentEngine(
            client_factory=make_client,
            source_factory=make_source,
            approved_model_ids=["gpt-4o-mini", "offline-budget-fixture", "qwen-2.5-72b"],
            config=AgentConfig(llm_timeout_seconds=60, run_timeout_seconds=120),
            allow_test_dependencies=False,
        )
    except Exception as exc:
        logger.warning(f"Could not build RuntimeAgentEngine: {exc}. Falling back to MockMLInsightEngine.")
        return MockMLInsightEngine()


# Global engine instance (can be swapped via set_ml_engine)
current_ml_engine: MLInsightEngineProtocol = create_default_ml_engine()


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

                        if hasattr(current_ml_engine, "generate_analysis"):
                            try:
                                analysis_result = (
                                    await current_ml_engine.generate_analysis(
                                        company=company_schema,
                                        event=event_schema,
                                        counterparties=counterparties,
                                        tracer=tracer,
                                    )
                                )
                                result = analysis_result.insight
                                companion_store.save(
                                    job.id,
                                    job.company_id,
                                    job.event_id,
                                    analysis_result,
                                )
                            except Exception as ex:
                                logger.warning(
                                    f"generate_analysis failed, falling back to generate_insight: {ex}"
                                )
                                result = (
                                    await current_ml_engine.generate_insight(
                                        company=company_schema,
                                        event=event_schema,
                                        counterparties=counterparties,
                                        tracer=tracer,
                                    )
                                )
                        else:
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
                        companion_store.link_insight(saved.id, job.id)

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