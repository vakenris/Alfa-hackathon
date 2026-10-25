from typing import List
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.repositories.news_repo import NewsRepository
from backend.app.db.repositories.company_repo import CompanyRepository
from backend.app.schemas.event import NewsArticleResponse, FeedResponse


class FeedService:
    def __init__(self, session: AsyncSession):
        self.news_repo = NewsRepository(session)
        self.company_repo = CompanyRepository(session)

    async def get_company_feed(self, company_id: int, limit: int = 50) -> FeedResponse:
        company = await self.company_repo.get_by_id(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        articles = await self.news_repo.list_feed(limit=limit)

        results: List[NewsArticleResponse] = []
        for a in articles:
            schema = NewsArticleResponse.model_validate(a)
            # Basic relevance boosting if OKVED prefix matches
            if company and company.mainokved_code and schema.target_okved_prefix:
                if company.mainokved_code.startswith(schema.target_okved_prefix):
                    schema.relevance_score = 1.5
            results.append(schema)

        # Sort by relevance descending, then date
        results.sort(key=lambda x: (x.relevance_score or 1.0, x.published_at), reverse=True)

        return FeedResponse(
            company_id=company_id,
            items=results,
            total=len(results)
        )
