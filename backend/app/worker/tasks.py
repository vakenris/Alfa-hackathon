import asyncio
from datetime import datetime
from backend.app.core.bus import get_event_bus
from backend.app.core.logging import logger
from backend.app.db.session import async_session_factory
from backend.app.db.repositories.company_repo import CompanyRepository
from backend.app.db.repositories.news_repo import NewsRepository
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.worker.tracer import AgentTracer
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse
from backend.app.ml.interface import MockMLInsightEngine, MLInsightEngineProtocol
from backend.app.agent.companion import companion_store
from backend.app.core.config import settings


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


async def run_insight_pipeline(job_id: str, company_id: int, event_id: int):
    """Orchestrates asynchronous insight generation in background worker."""
    bus = get_event_bus()
    logger.info(f"Starting insight pipeline for Job {job_id} (Company: {company_id}, Event: {event_id})")

    async with async_session_factory() as session:
        job_repo = JobRepository(session)
        company_repo = CompanyRepository(session)
        news_repo = NewsRepository(session)
        insight_repo = InsightRepository(session)
        tracer = AgentTracer(job_id, job_repo)

        try:
            await job_repo.update_status(job_id, "PROCESSING")
            await bus.publish(f"job:{job_id}", {
                "event": "status",
                "status": "PROCESSING",
                "job_id": job_id
            })

            # 1. Fetch Company
            company_model = await company_repo.get_by_id(company_id)
            if not company_model:
                raise ValueError(f"Company {company_id} not found")

            # 2. Fetch Event
            event_model = await news_repo.get_by_id(event_id)
            if not event_model:
                raise ValueError(f"News event {event_id} not found")

            # 3. Fetch Counterparties
            counterparties = await company_repo.get_top_counterparties(company_model.synthetic_u, limit=5)

            # 4. Prepare Schemas for ML Engine
            company_schema = CompanyResponse.model_validate(company_model)
            event_schema = NewsArticleResponse.model_validate(event_model)

            # 5. Call ML Agent via interface (supports companion analysis when available)
            if hasattr(current_ml_engine, "generate_analysis"):
                try:
                    analysis_result = await current_ml_engine.generate_analysis(
                        company=company_schema,
                        event=event_schema,
                        counterparties=counterparties,
                        tracer=tracer
                    )
                    ml_result = analysis_result.insight
                    companion_store.save(job_id, company_id, event_id, analysis_result)
                except Exception as ex:
                    logger.warning(f"generate_analysis failed, falling back to generate_insight: {ex}")
                    ml_result = await current_ml_engine.generate_insight(
                        company=company_schema,
                        event=event_schema,
                        counterparties=counterparties,
                        tracer=tracer
                    )
            else:
                ml_result = await current_ml_engine.generate_insight(
                    company=company_schema,
                    event=event_schema,
                    counterparties=counterparties,
                    tracer=tracer
                )

            # 6. Save Insight & Evidence to DB
            saved_insight = await insight_repo.save_insight(
                job_id=job_id,
                company_id=company_id,
                event_id=event_id,
                ml_result=ml_result
            )
            companion_store.link_insight(saved_insight.id, job_id)

            # 7. Mark Job as SUCCESS
            await job_repo.update_status(job_id, "SUCCESS")
            await bus.publish(f"job:{job_id}", {
                "event": "completed",
                "status": "SUCCESS",
                "job_id": job_id,
                "insight_id": saved_insight.id
            })
            logger.info(f"Job {job_id} finished successfully with insight {saved_insight.id}")

        except Exception as e:
            logger.exception(f"Job {job_id} failed: {str(e)}")
            await job_repo.update_status(job_id, "FAILED", error_message=str(e))
            await bus.publish(f"job:{job_id}", {
                "event": "failed",
                "status": "FAILED",
                "job_id": job_id,
                "error": str(e)
            })
