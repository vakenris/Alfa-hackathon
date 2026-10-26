"""Small internal contract. Source facts are supplied by Python, not generated."""
from datetime import datetime
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.event import NewsArticleResponse

BANK_VIEW_UNAVAILABLE = 'Позиция Альфа-Банка не предоставлена.'
DISCLAIMER = 'Аналитика не является индивидуальной инвестиционной рекомендацией.'


class AgentError(RuntimeError):
    run_report: dict[str, Any] | None = None


class AgentConfigurationError(AgentError): pass
class AgentContextError(AgentError): pass
class AgentResponseError(AgentError): pass
class AgentEvidenceError(AgentError): pass
class AgentLLMError(AgentError): pass
class AgentLLMTimeoutError(AgentLLMError): pass
class AgentTimeoutError(AgentError): pass


class InternalModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_:.-]+$')]
ShortText = Annotated[str, Field(min_length=1, max_length=500, strict=True)]
Channel = Literal['financing', 'investment', 'fx', 'logistics', 'inputs', 'regulation', 'demand', 'other']


class AnalyticsDocument(InternalModel):
    article: NewsArticleResponse
    source_url: HttpUrl | None = None
    provenance_status: Literal['unverified', 'verified', 'synthetic'] = 'unverified'
    source_document_id: str | None = Field(default=None, max_length=256)
    source_page: int | None = Field(default=None, ge=1, strict=True)
    source_hash: str | None = Field(default=None, max_length=128)


class GroundedSource(InternalModel):
    source_id: str
    source_title: str
    source_url: str | None = None
    published_at: datetime | None = None
    is_bank_analytics: bool = False
    provenance_status: Literal['unverified', 'verified', 'synthetic'] = 'unverified'
    source_document_id: str | None = None
    source_page: int | None = None
    source_hash: str | None = None
    text_truncated: bool = False


class Evidence(InternalModel):
    id: Identifier
    source_id: str
    source_title: str
    quote: str
    kind: Literal['news', 'company', 'payment', 'bank', 'calculation']
    company_feature: str | None = None
    source_url: str | None = None
    published_at: datetime | None = None
    provenance_status: Literal['unverified', 'verified', 'synthetic'] = 'unverified'
    source_document_id: str | None = None
    source_page: int | None = None
    source_hash: str | None = None
    text_truncated: bool = False


class BusinessContext(InternalModel):
    known_features: dict[str, Any]
    unknown_features: list[str]
    limitations: list[str]
    payment_sample_size: int
    self_transfer_rows: int
    revenue_year: int | None = None
    revenue_currency: str | None = None


class ImpactHypothesis(InternalModel):
    id: Identifier
    channel: Channel
    direction: Literal['risk', 'opportunity', 'mixed'] = 'mixed'
    condition: ShortText
    mechanism: ShortText
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=6)
    data_needs: list[ShortText] = Field(default_factory=list, max_length=4)
    horizon: Literal['short', 'medium', 'long', 'unknown'] = 'unknown'


class Scenario(InternalModel):
    label: Annotated[str, Field(min_length=1, max_length=100)]
    condition: ShortText
    outcome: ShortText
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=6)


class ProposedAction(InternalModel):
    title: ShortText
    rationale: ShortText
    impact_ids: list[Identifier] = Field(min_length=1, max_length=4)
    required_data: list[ShortText] = Field(default_factory=list, max_length=4)
    owner_role: Literal['CEO', 'CFO', 'TREASURY', 'ACCOUNTANT', 'ANALYST'] = 'CFO'


class ContextRequest(InternalModel):
    """A bounded read-only request for Python to consider, never an LLM tool call."""
    kind: Literal['bank_search'] = 'bank_search'
    query: Annotated[str, Field(min_length=1, max_length=240, strict=True, pattern=r'\S')]
    reason: ShortText


class SourceComparison(InternalModel):
    """Unresolved model interpretation; IDs/dates do not establish semantic truth."""
    evidence_ids: list[Identifier] = Field(min_length=2, max_length=5)
    relationship: Literal['potential_conflict', 'different_vintages', 'scope_requires_check']
    status: Literal['unresolved'] = 'unresolved'
    explanation: ShortText


class DraftInsight(InternalModel):
    applicability: Literal['conditional', 'insufficient']
    impacts: list[ImpactHypothesis] = Field(default_factory=list, max_length=4)
    scenarios: list[Scenario] = Field(default_factory=list, max_length=3)
    actions: list[ProposedAction] = Field(default_factory=list, max_length=4)
    missing_data: list[ShortText] = Field(default_factory=list, max_length=6)
    context_request: ContextRequest | None = None
    source_comparisons: list[SourceComparison] = Field(default_factory=list, max_length=3)


class CheckIssue(InternalModel):
    code: str
    claim_id: str | None = None
    path: str | None = None
    excerpt: str | None = None


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
    evidence: list[Evidence] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
