from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import (
    UserRole,
    get_current_user_role,
    get_db,
)
from backend.app.db.models.news import NewsArticle
from backend.app.db.models.news_source import ExternalNewsSource
from backend.app.ingestion.news import payload_hash, sha256
from backend.app.schemas.event import NewsArticleResponse
from backend.app.services.news_selection import (
    CANDIDATE_LIMIT,
    NewsSelectionService,
)


router = APIRouter(tags=["ML News"])


@router.get(
    "/companies/{company_id}/news",
    response_model=list[NewsArticleResponse],
)
async def get_relevant_news(
    company_id: int,
    response: Response,
    limit: int = Query(default=10, ge=1, le=50),
    days: int = Query(default=90, ge=1, le=365),
    as_of: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    """External news selected by rules. Does not schedule analysis."""
    response.headers["X-News-Selection"] = "rules-v1"
    response.headers["X-News-Candidate-Limit"] = str(CANDIDATE_LIMIT)

    return await NewsSelectionService(db).list_for_company(
        company_id,
        limit=limit,
        days=days,
        as_of=as_of,
    )


@router.get("/news/{news_id}/source")
async def get_news_source(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    statement = (
        select(ExternalNewsSource, NewsArticle)
        .join(
            NewsArticle,
            NewsArticle.id == ExternalNewsSource.article_id,
        )
        .where(NewsArticle.id == news_id)
    )

    row = (await db.execute(statement)).first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="News source not found",
        )

    source, article = row

    values = {
        name: getattr(article, name)
        for name in (
            "title",
            "content",
            "published_at",
            "category",
            "source",
            "is_bank_analytics",
        )
    }

    if (
        sha256(article.content) != source.content_sha256
        or payload_hash(values) != source.payload_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="News source integrity check failed",
        )

    return {
        "news_id": news_id,
        "source_url": source.source_url,
        "source_item_id": source.source_item_id,
        "feed_url": source.feed_url,
        "feed_sha256": source.feed_sha256,
        "content_sha256": source.content_sha256,
        "content_scope": source.content_scope,
    }