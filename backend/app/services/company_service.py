from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.repositories.company_repo import CompanyRepository
from backend.app.schemas.company import CompanyResponse, CounterpartyGraphResponse


class CompanyService:
    def __init__(self, session: AsyncSession):
        self.repo = CompanyRepository(session)

    async def list_companies(self, limit: int = 50, offset: int = 0) -> List[CompanyResponse]:
        companies = await self.repo.list_all(limit=limit, offset=offset)
        return [CompanyResponse.model_validate(c) for c in companies]

    async def get_company(self, company_id: int) -> Optional[CompanyResponse]:
        c = await self.repo.get_by_id(company_id)
        if not c:
            return None
        return CompanyResponse.model_validate(c)

    async def get_counterparties_graph(self, company_id: int) -> Optional[CounterpartyGraphResponse]:
        c = await self.repo.get_by_id(company_id)
        if not c:
            return None
        items = await self.repo.get_top_counterparties(c.synthetic_u)
        return CounterpartyGraphResponse(
            company_synthetic_u=c.synthetic_u,
            top_counterparties=items
        )
