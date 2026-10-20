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

# Global default engine instance (can be swapped by ML team)
current_ml_engine: MLInsightEngineProtocol = MockMLInsightEngine()


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

            # 5. Call ML Agent via interface
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
