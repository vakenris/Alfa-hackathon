"""Internal control messages only. Public backend schemas remain unchanged."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter

from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.event import NewsArticleResponse


BANK_VIEW_UNAVAILABLE = "Позиция Альфа-Банка не предоставлена."


class AgentError(RuntimeError):
    """Safe to expose through the worker's existing FAILED path."""


class AgentResponseError(AgentError):
    pass


class AgentEvidenceError(AgentError):
    pass


class AgentContextError(AgentError):
    pass


class AgentLLMError(AgentError):
    pass


class AgentLLMTimeoutError(AgentLLMError):
    """The orchestrator's own per-call deadline expired, not an API failure."""


class AgentTimeoutError(AgentError):
    pass


class InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class AnalyticsDocument(InternalModel):
    """Source metadata supplied by an adapter, never invented by the LLM."""

    article: NewsArticleResponse
    source_url: HttpUrl | None = None


class AnalyticsArguments(InternalModel):
    query: str = Field(default="", max_length=256, strict=True)
    limit: int = Field(default=3, ge=1, le=5, strict=True)


class CounterpartyArguments(InternalModel):
    pass


class ToolAction(InternalModel):
    action: Literal["tool"]
    tool: Literal["get_bank_analytics", "summarize_counterparties"]
    arguments: dict[str, Any]
    purpose: str = Field(default="", max_length=240, strict=True)


class FinalAction(InternalModel):
    action: Literal["final"]
    result: MLInsightResult


ACTION_ADAPTER = TypeAdapter(Annotated[ToolAction | FinalAction, Field(discriminator="action")])


class SourceRecord(InternalModel):
    source_id: str
    source_title: str
    source_url: str | None = None
    is_bank_analytics: bool = False


class ToolObservation(InternalModel):
    tool: str
    arguments: dict[str, Any]
    status: Literal["ok", "error"]
    data: dict[str, Any] | None = None
    error: Literal[
        "INVALID_ARGUMENTS", "TOOL_TIMEOUT", "TOOL_FAILED", "INVALID_TOOL_RESULT",
        "DUPLICATE_REQUEST", "CONTEXT_LIMIT",
    ] | None = None
