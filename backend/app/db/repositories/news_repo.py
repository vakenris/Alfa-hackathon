from typing import List, Optional
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.models.news import NewsArticle


class NewsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, news_id: int) -> Optional[NewsArticle]:
        stmt = select(NewsArticle).where(NewsArticle.id == news_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_feed(self, limit: int = 50, offset: int = 0) -> List[NewsArticle]:
        stmt = select(NewsArticle).order_by(desc(NewsArticle.published_at)).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_bank_analytics(self, limit: int = 20) -> List[NewsArticle]:
        stmt = select(NewsArticle).where(NewsArticle.is_bank_analytics == True).order_by(desc(NewsArticle.published_at)).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
