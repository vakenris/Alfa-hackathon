from typing import Optional, List
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.models.insight import Insight, Evidence
from backend.app.ml.interface import MLInsightResult


class InsightRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_insight(
        self,
        job_id: str,
        company_id: int,
        event_id: int,
        ml_result: MLInsightResult
    ) -> Insight:
        insight = Insight(
            job_id=job_id,
            company_id=company_id,
            event_id=event_id,
            what_happened=ml_result.what_happened,
            why_important=ml_result.why_important,
            consequences=ml_result.consequences,
            bank_view=ml_result.bank_view,
            ai_view=ml_result.ai_view,
            recommended_action=ml_result.recommended_action,
            relevant_bank_product=ml_result.relevant_bank_product
        )
        self.session.add(insight)
        await self.session.flush()

        for ev in ml_result.evidences:
            evidence_model = Evidence(
                insight_id=insight.id,
                claim=ev.claim,
                source_title=ev.source_title,
                source_url=ev.source_url,
                company_feature=ev.company_feature
            )
            self.session.add(evidence_model)

        await self.session.commit()
        await self.session.refresh(insight)
        return insight

    async def get_by_id(self, insight_id: int) -> Optional[Insight]:
        stmt = (
            select(Insight)
            .where(Insight.id == insight_id)
            .options(selectinload(Insight.evidences))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_job_id(self, job_id: str) -> Optional[Insight]:
        stmt = (
            select(Insight)
            .where(Insight.job_id == job_id)
            .options(selectinload(Insight.evidences))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_company_and_event(self, company_id: int, event_id: int) -> Optional[Insight]:
        stmt = (
            select(Insight)
            .where(Insight.company_id == company_id, Insight.event_id == event_id)
            .order_by(Insight.id.desc())
            .limit(1)
            .options(selectinload(Insight.evidences))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
