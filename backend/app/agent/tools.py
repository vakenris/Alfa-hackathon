"""The single injectable, read-only bank search boundary; no agent tool loop."""

from typing import Protocol

from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse

from .models import AnalyticsDocument


class AnalyticsSourceProtocol(Protocol):
    async def search(
        self, query: str, *, company: CompanyResponse, event: NewsArticleResponse, limit: int,
    ) -> list[AnalyticsDocument]:
        """Return documents with their original dates and provenance metadata."""
        ...


class MockAnalyticsSource:
    """Independent fixture copies; never accesses a database or the network."""

    def __init__(self, documents: list[AnalyticsDocument] | None = None):
        self._documents = [item.model_copy(deep=True) for item in documents or []]

    @property
    def search_available(self) -> bool:
        return bool(self._documents)

    async def search(
        self, query: str, *, company: CompanyResponse, event: NewsArticleResponse, limit: int,
    ) -> list[AnalyticsDocument]:
        return [item.model_copy(deep=True) for item in self._documents[:limit]]
