"""Internal control messages only. Public backend schemas remain unchanged."""

from typing import Annotated, Any, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, StringConstraints

from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.event import NewsArticleResponse


BANK_VIEW_UNAVAILABLE = "Позиция Альфа-Банка не предоставлена."
DISCLAIMER = "Аналитика не является индивидуальной инвестиционной рекомендацией."


class AgentError(RuntimeError):
    """Safe to expose through the worker's existing FAILED path."""
    run_report: dict[str, Any] | None = None


class AgentConfigurationError(AgentError):
    pass


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
    provenance_status: Literal["unverified", "verified", "synthetic"] = "unverified"
    source_document_id: str | None = Field(default=None, max_length=256)
    source_page: int | None = Field(default=None, ge=1, strict=True)
    source_hash: str | None = Field(default=None, max_length=128)


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


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:.-]+$")]


class BusinessContext(InternalModel):
    known_features: dict[str, Any]
    unknown_features: list[str]
    limitations: list[str]
    payment_sample_size: int
    self_transfer_rows: int
    revenue_year: int | None = None
    revenue_currency: str | None = None


class GroundedSource(InternalModel):
    source_id: str
    source_title: str
    source_url: str | None = None
    published_at: datetime
    is_bank_analytics: bool = False
    provenance_status: Literal["unverified", "verified", "synthetic"] = "unverified"
    source_document_id: str | None = None
    source_page: int | None = None
    source_hash: str | None = None
    text_truncated: bool = False


class AnchoredFact(InternalModel):
    id: Identifier
    source_id: Identifier
    field: Literal["title", "content", "summary"] = "content"
    quote: Annotated[str, Field(min_length=1, max_length=1000, strict=True)]
    kind: Literal["reported_fact", "bank_statement"] = "reported_fact"
    metric: str | None = Field(default=None, max_length=80)
    entity: str | None = Field(default=None, max_length=128)
    period: str | None = Field(default=None, max_length=80)
    unit: str | None = Field(default=None, max_length=32)
    value: float | None = Field(default=None, allow_inf_nan=False)


class ImpactHypothesis(InternalModel):
    id: Identifier
    channel: Literal["financing", "investment", "fx", "logistics", "inputs", "regulation", "demand", "other"]
    direction: Literal["risk", "opportunity", "mixed"]
    mechanism: ShortText
    fact_ids: list[Identifier] = Field(min_length=1, max_length=8)
    company_features: list[str] = Field(min_length=1, max_length=8)
    assumptions: list[ShortText] = Field(min_length=1, max_length=5)
    data_needs: list[ShortText] = Field(default_factory=list, max_length=8)
    horizon: Literal["short", "medium", "long", "unknown"] = "unknown"
    certainty: Literal["conditional"] = "conditional"


class ProposedAction(InternalModel):
    title: ShortText
    impact_ids: list[Identifier] = Field(min_length=1, max_length=8)
    required_data: list[ShortText] = Field(default_factory=list, max_length=8)
    owner_role: Literal["CEO", "CFO", "TREASURY", "ACCOUNTANT", "ANALYST"] | None = None


class DraftInsight(InternalModel):
    action: Literal["draft"] = "draft"
    applicability: Literal["conditional", "insufficient"]
    facts: list[AnchoredFact] = Field(min_length=1, max_length=12)
    impacts: list[ImpactHypothesis] = Field(default_factory=list, max_length=8)
    actions: list[ProposedAction] = Field(default_factory=list, max_length=8)
    missing_data: list[ShortText] = Field(default_factory=list, max_length=10)
    clarification_questions: list[ShortText] = Field(default_factory=list, max_length=5)
    acknowledged_conflicts: list[list[Identifier]] = Field(default_factory=list, max_length=8)


GROUNDED_ACTION_ADAPTER = TypeAdapter(Annotated[ToolAction | DraftInsight, Field(discriminator="action")])


class CheckIssue(InternalModel):
    code: str
    claim_id: str | None = None


class VerificationReport(InternalModel):
    passed: bool
    issues: list[CheckIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_facts: int = 0
    checked_impacts: int = 0


class AgentAnalysisResult(InternalModel):
    company_id: int
    event_id: int
    insight: MLInsightResult
    analysis: DraftInsight
    business_context: BusinessContext
    sources: list[GroundedSource]
    verification: VerificationReport
    report: dict[str, Any]
