from typing import List, Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.news import NewsArticle
from backend.app.schemas.event import NewsArticleResponse

router = APIRouter(prefix="/analytics", tags=["Bank Research Analytics"])


@router.get("", response_model=List[NewsArticleResponse])
async def list_bank_analytics(
    category: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Retrieve research and macroeconomic publications from Alfa-Bank."""
    stmt = select(NewsArticle).where(NewsArticle.is_bank_analytics == True)
    if category:
        stmt = stmt.where(NewsArticle.category == category)
    stmt = stmt.order_by(desc(NewsArticle.published_at)).limit(limit)
    
    result = await db.execute(stmt)
    articles = result.scalars().all()
    return [NewsArticleResponse.model_validate(a) for a in articles]


@router.get("/{analytics_id}", response_model=NewsArticleResponse)
async def get_bank_analytics_by_id(
    analytics_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get single Alfa-Bank macroeconomic report by ID."""
    stmt = select(NewsArticle).where(NewsArticle.id == analytics_id, NewsArticle.is_bank_analytics == True)
    result = await db.execute(stmt)
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Bank analytical report not found")
    return NewsArticleResponse.model_validate(article)
