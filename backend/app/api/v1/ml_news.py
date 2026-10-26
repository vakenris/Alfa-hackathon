from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import (
    UserRole,
    get_current_user_role,
    get_db,
)
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