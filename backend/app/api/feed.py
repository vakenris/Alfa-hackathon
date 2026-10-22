from typing import List
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.company import Company
from backend.app.db.models.news import NewsArticle
from backend.app.schemas.event import FeedResponse, NewsArticleResponse

router = APIRouter(prefix="/feed", tags=["News Feed"])


@router.get("/{company_id}", response_model=FeedResponse)
async def get_company_feed(
    company_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Retrieve personalized news feed tailored to the company's OKVED and context."""
    # Check company
    comp_stmt = select(Company).where(Company.id == company_id)
    comp_res = await db.execute(comp_stmt)
    company = comp_res.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Fetch news
    news_stmt = select(NewsArticle).order_by(desc(NewsArticle.published_at)).limit(limit)
    news_res = await db.execute(news_stmt)
    articles = news_res.scalars().all()

    items: List[NewsArticleResponse] = []
    for art in articles:
        schema = NewsArticleResponse.model_validate(art)
        # Relevance boosting: match OKVED prefix
        if company.mainokved_code and schema.target_okved_prefix:
            if company.mainokved_code.startswith(schema.target_okved_prefix):
                schema.relevance_score = 1.6
        # Macroeconomic news affects all large business
        elif schema.category == "Макроэкономика":
            schema.relevance_score = 1.3
        items.append(schema)

    # Sort by relevance descending, then date
    items.sort(key=lambda x: (x.relevance_score or 1.0, x.published_at), reverse=True)

    return FeedResponse(
        company_id=company_id,
        items=items,
        total=len(items)
    )
