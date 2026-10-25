"""Allowlisted, read-only tools and an injectable analytics search boundary."""

import asyncio
import math
from typing import Protocol

from pydantic import ValidationError

from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .config import AgentConfig
from .models import (
    AnalyticsArguments, AnalyticsDocument, CounterpartyArguments, ToolAction, ToolObservation,
)


class AnalyticsSourceProtocol(Protocol):
    async def search(
        self, query: str, *, company: CompanyResponse, event: NewsArticleResponse, limit: int,
    ) -> list[AnalyticsDocument]:
        """Return source metadata and articles. Implementations may use search or RAG."""
        ...


class MockAnalyticsSource:
    """Offline fixtures only; no implicit database or network access."""

    def __init__(self, documents: list[AnalyticsDocument] | None = None):
        self._documents = list(documents or [])

    @property
    def search_available(self) -> bool:
        """An empty fixed fixture has nothing to retrieve, independent of query."""
        return bool(self._documents)

    async def search(
        self, query: str, *, company: CompanyResponse, event: NewsArticleResponse, limit: int,
    ) -> list[AnalyticsDocument]:
        return [document.model_copy(deep=True) for document in self._documents[:limit]]


TOOL_DESCRIPTIONS = [
    {
        "name": "get_bank_analytics",
        "description": "Получить банковские материалы по теме; пустой результат допустим.",
        "arguments_schema": AnalyticsArguments.model_json_schema(),
    },
    {
        "name": "summarize_counterparties",
        "description": "Суммы по направлениям только в переданной выборке, не по всем платежам.",
        "arguments_schema": CounterpartyArguments.model_json_schema(),
    },
]


def validate_arguments(action: ToolAction) -> AnalyticsArguments | CounterpartyArguments:
    if action.tool == "get_bank_analytics":
        return AnalyticsArguments.model_validate(action.arguments)
    return CounterpartyArguments.model_validate(action.arguments)


async def execute_tool(
    action: ToolAction, *, source: AnalyticsSourceProtocol, company: CompanyResponse,
    event: NewsArticleResponse, counterparties: list[CounterpartyItem], config: AgentConfig,
) -> ToolObservation:
    def failure(code: str) -> ToolObservation:
        return ToolObservation(
            tool=action.tool, arguments=action.arguments, status="error", error=code,
        )

    try:
        arguments = validate_arguments(action)
    except ValidationError:
        return failure("INVALID_ARGUMENTS")

    try:
        async with asyncio.timeout(config.tool_timeout_seconds):
            if isinstance(arguments, AnalyticsArguments):
                documents = await source.search(
                    arguments.query, company=company, event=event, limit=arguments.limit,
                )
                if not isinstance(documents, list):
                    return failure("INVALID_TOOL_RESULT")
                validated = [
                    AnalyticsDocument.model_validate(
                        item.model_dump(mode="python") if isinstance(item, AnalyticsDocument) else item,
                    )
                    for item in documents[:arguments.limit]
                ]
                if any(not item.article.is_bank_analytics for item in validated):
                    return failure("INVALID_TOOL_RESULT")
                data = {"documents": [item.model_dump(mode="json") for item in validated]}
                coverage = getattr(source, "coverage", None)
                if config.analysis_mode == "grounded" and isinstance(coverage, str):
                    candidate_limit = getattr(source, "candidate_limit", None)
                    data["retrieval"] = {
                        "coverage": coverage[:80],
                        "candidate_limit": candidate_limit if type(candidate_limit) is int and 1 <= candidate_limit <= 50 else None,
                        "exhaustive": False,
                    }
            else:
                directions: dict[str, dict[str, float | int]] = {}
                for item in counterparties:
                    if not math.isfinite(item.total_amount) or item.total_amount < 0 or item.transaction_count < 0:
                        return failure("INVALID_TOOL_RESULT")
                    group = directions.setdefault(
                        item.direction, {"count": 0, "total_amount": 0.0, "transaction_count": 0},
                    )
                    group["count"] += 1
                    group["total_amount"] += item.total_amount
                    group["transaction_count"] += item.transaction_count
                    if not math.isfinite(group["total_amount"]):
                        return failure("INVALID_TOOL_RESULT")
                data = {
                    "directions": directions, "sample_size": len(counterparties),
                    "scope": "Только переданная выборка; полнота сети и период платежей неизвестны.",
                }
    except TimeoutError:
        return failure("TOOL_TIMEOUT")
    except (ValidationError, ValueError, TypeError, OverflowError):
        return failure("INVALID_TOOL_RESULT")
    except Exception:
        return failure("TOOL_FAILED")
    return ToolObservation(tool=action.tool, arguments=action.arguments, status="ok", data=data)
