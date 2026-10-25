from typing import List
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.services.feed_service import FeedService
from backend.app.services.automatic_insights import schedule_company_news
from backend.app.schemas.event import FeedResponse, NewsArticleResponse
from backend.app.db.repositories.news_repo import NewsRepository

router = APIRouter(tags=["News & Feed"])


@router.get("/companies/{company_id}/feed", response_model=FeedResponse)
async def get_company_feed(
    company_id: int,
    background_tasks: BackgroundTasks,
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Retrieve personalized news feed tailored to the company context and industry."""
    service = FeedService(db)
    feed = await service.get_company_feed(company_id=company_id, limit=limit)
    await schedule_company_news(db, background_tasks, company_id)
    return feed


@router.get("/feed/{company_id}", response_model=FeedResponse, include_in_schema=False)
async def get_feed_alias(
    company_id: int,
    background_tasks: BackgroundTasks,
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Alias for /companies/{company_id}/feed."""
    service = FeedService(db)
    feed = await service.get_company_feed(company_id=company_id, limit=limit)
    await schedule_company_news(db, background_tasks, company_id)
    return feed


@router.get("/analytics/bank", response_model=List[NewsArticleResponse])
async def get_bank_analytics(
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get research and macroeconomic publications from Alfa-Bank."""
    repo = NewsRepository(db)
    articles = await repo.list_bank_analytics(limit=limit)
    return [NewsArticleResponse.model_validate(a) for a in articles]


@router.get("/analytics", response_model=List[NewsArticleResponse], include_in_schema=False)
async def get_analytics_alias(
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Alias for /analytics/bank."""
    repo = NewsRepository(db)
    articles = await repo.list_bank_analytics(limit=limit)
    return [NewsArticleResponse.model_validate(a) for a in articles]
