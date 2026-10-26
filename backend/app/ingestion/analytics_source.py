"""PDF-to-agent bridge. No agent imports or database connections at import time."""

from datetime import datetime, time, timedelta, timezone

from backend.app.db.repositories.bank_analytics_repo import (
    BankAnalyticsRepository, BankSearchHit,
)
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse


def event_cutoff_day(value: datetime):
    utc = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    # A date-only PDF is considered available at the END of that day in UTC.
    # This conservative bound is not a claim about its actual publication time.
    return utc.date() if utc.timetz().replace(tzinfo=None) == time.max else utc.date() - timedelta(days=1)


def document_payload(hit: BankSearchHit) -> dict:
    if hit.provenance_status != "verified":
        raise ValueError("Страница не проверена и недоступна агенту")
    article = NewsArticleResponse(
        id=hit.page_id,
        title=f"{hit.title[:480]} (стр. {hit.page_number})",
        source="Альфа-Банк (PDF)",
        published_at=datetime.combine(hit.published_on, time.max, tzinfo=timezone.utc),
        content=hit.text,
        summary=None,
        category=hit.category,
        target_okved_prefix=None,
        is_bank_analytics=True,
        relevance_score=None,
    )
    return {
        "article": article,
        "source_url": None,
        "provenance_status": hit.provenance_status,
        "source_document_id": f"bank-pdf:{hit.document_id}",
        "source_page": hit.page_number,
        "source_hash": hit.document_id,
    }


class BankPdfAnalyticsSource:
    candidate_limit = 300
    coverage = "newest_300_dated_bank_pdf_pages_reviewed_only_first_50000_chars"
    retrieval_method = "lexical_terms"

    def __init__(self, session_factory, *, include_historical: bool = False):
        if not callable(session_factory):
            raise ValueError("Нужна фабрика асинхронных сессий")
        self.session_factory = session_factory
        self.include_historical = include_historical

    async def search(
        self, query: str, *, company: CompanyResponse,
        event: NewsArticleResponse, limit: int,
    ) -> list:
        if not isinstance(query, str):
            raise ValueError("Поисковый запрос должен быть строкой")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("limit должен быть от 1 до 5")
        # This module is supplied by the ML branch and used only after integration.
        from backend.app.agent.models import AnalyticsDocument

        async with self.session_factory() as session:
            hits = await BankAnalyticsRepository(session).search(
                query[:1024] + " " + event.title[:1024],
                as_of=event_cutoff_day(event.published_at),
                limit=limit,
                candidate_limit=self.candidate_limit,
                include_historical=self.include_historical,
                reviewed_only=True,
            )
        return [AnalyticsDocument.model_validate(document_payload(hit)) for hit in hits]
