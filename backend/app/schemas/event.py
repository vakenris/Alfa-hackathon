from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class NewsArticleResponse(BaseModel):
    id: int
    title: str
    source: str
    published_at: datetime
    content: str
    summary: Optional[str] = None
    category: str
    target_okved_prefix: Optional[str] = None
    is_bank_analytics: bool = False
    relevance_score: Optional[float] = 1.0

    model_config = ConfigDict(from_attributes=True)


class FeedResponse(BaseModel):
    company_id: int
    items: List[NewsArticleResponse]
    total: int
