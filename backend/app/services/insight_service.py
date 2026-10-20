from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.schemas.insight import InsightResponse, EvidenceItem


class InsightService:
    def __init__(self, session: AsyncSession):
        self.repo = InsightRepository(session)

    async def get_insight(self, insight_id: int) -> Optional[InsightResponse]:
        insight = await self.repo.get_by_id(insight_id)
        if not insight:
            return None
        return InsightResponse.model_validate(insight)

    async def get_by_company_and_event(self, company_id: int, event_id: int) -> Optional[InsightResponse]:
        insight = await self.repo.get_by_company_and_event(company_id, event_id)
        if not insight:
            return None
        return InsightResponse.model_validate(insight)
