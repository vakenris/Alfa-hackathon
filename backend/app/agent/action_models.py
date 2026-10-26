"""Typed companion DTOs for proposals, questions, and explicit loan sensitivity.

All monetary inputs are supplied by the caller with provenance; ``actual`` is
the caller's declaration, not a claim of independent document verification.
``delta_contract_rate`` is an annual rate fraction (0.01 means one percentage
point). It applies for every supplied day, after any explicitly accounted-for
contract reset. ``days`` is not an arbitrary pre-reset forecast horizon. Neither
the reset date nor a contractual delta may be inferred from a news/key-rate move.
"""

from decimal import Decimal
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import Identifier, InternalModel, Scenario, ShortText


Money = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=6, allow_inf_nan=False)]
Currency = Literal["RUB", "USD", "EUR", "CNY"]


class InputProvenance(InternalModel):
    data_mode: Literal["actual", "hypothetical"]
    reference: Annotated[str, Field(min_length=1, max_length=240, strict=True)]

    @field_validator('reference')
    @classmethod
    def nonempty_reference(cls,value):
        if not value.strip(): raise ValueError('A meaningful source reference is required')
        return value


class LoanSensitivityInput(InternalModel):
    company_id: int = Field(gt=0, strict=True)
    principal: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=6, allow_inf_nan=False)]
    currency: Currency
    rate_type: Literal["fixed", "floating"]
    delta_contract_rate: Annotated[Decimal, Field(ge=-1, le=1, max_digits=12, decimal_places=10, allow_inf_nan=False)]
    days: int = Field(ge=1, le=3660, strict=True,
                      description="Number of days during which the explicit contractual delta applies in full; any reset is already accounted for by the caller.")
    day_count_basis: Literal[360, 365]
    reset_within_horizon: bool = Field(strict=True)
    provenance: InputProvenance

    @field_validator("principal", "delta_contract_rate", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean is not a monetary or rate input")
        return value

    @field_validator("day_count_basis", mode="before")
    @classmethod
    def require_integer_basis(cls, value):
        if type(value) is not int:
            raise ValueError("Day-count basis must be an integer")
        return value

    @model_validator(mode="after")
    def validate_reset(self):
        if not self.reset_within_horizon and self.delta_contract_rate != 0:
            raise ValueError("A nonzero contractual delta requires an explicit reset within the horizon")
        return self


class FinancialInputs(InternalModel):
    company_id: int = Field(gt=0, strict=True)
    available_cash: Money
    planned_capex: Money
    currency: Currency
    provenance: InputProvenance

    @field_validator("available_cash", "planned_capex", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean is not a monetary input")
        return value


class PlanItem(InternalModel):
    id: Identifier
    title: ShortText
    provenance: InputProvenance
    related_channels: list[Literal[
        "financing", "investment", "fx", "logistics", "inputs", "regulation", "demand", "other",
    ]] = Field(default_factory=list, max_length=8)


class ManagementActionRequest(InternalModel):
    kind: Literal["rate_scenario", "investment_budget", "review_plan", "question"]
    company_id: int = Field(gt=0, strict=True)
    rate_direction: Literal["up", "down", "unchanged"] | None = None
    question: Annotated[str, Field(min_length=1, max_length=1200, strict=True)] | None = None
    loan_sensitivity: LoanSensitivityInput | None = None
    financial_inputs: FinancialInputs | None = None
    current_plan: list[PlanItem] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def require_kind_fields(self):
        if self.kind == "rate_scenario" and self.rate_direction is None:
            raise ValueError("Rate direction is required for a rate scenario")
        if self.kind == "question" and (self.question is None or not self.question.strip()):
            raise ValueError("A nonempty question is required")
        if self.kind != "rate_scenario" and self.loan_sensitivity is not None:
            raise ValueError("Loan sensitivity is available only for an explicit rate scenario")
        for inputs in (self.loan_sensitivity, self.financial_inputs):
            if inputs is not None and inputs.company_id != self.company_id:
                raise ValueError("Financial input belongs to another company")
        if len({item.id for item in self.current_plan}) != len(self.current_plan):
            raise ValueError("Current plan item IDs must be unique")
        return self


class ActionSourceRef(InternalModel):
    fact_id: Identifier
    source_id: Identifier
    source_title: str
    source_url: str | None = None
    quote: str
    published_at: datetime | None = None
    source_document_id: str | None = None
    source_page: int | None = None
    source_hash: str | None = None
    text_truncated: bool = False
    provenance_status: Literal["unverified", "verified", "synthetic"]
    attribution: Literal["reported_source", "verified_bank_statement"]


class KnownCompanyFeature(InternalModel):
    key: str
    value: str | int | float | bool


class ActionImpact(InternalModel):
    description: str
    fact_ids: list[Identifier]
    company_features: list[KnownCompanyFeature] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    certainty: Literal["conditional"] = "conditional"


class ActionProposal(InternalModel):
    title: str
    rationale: str
    fact_ids: list[Identifier] = Field(default_factory=list)
    plan_item_ids: list[Identifier] = Field(default_factory=list)
    owner_role: Literal["CEO", "CFO", "TREASURY", "ACCOUNTANT", "ANALYST"] | None = None
    requires_human_review: Literal[True] = True


class SensitivityCalculation(InternalModel):
    kind: Literal["simple_interest_cost_delta"] = "simple_interest_cost_delta"
    formula: Literal["principal * delta_contract_rate * days / day_count_basis"] = "principal * delta_contract_rate * days / day_count_basis"
    principal: Decimal
    delta_contract_rate: Decimal
    days: int
    day_count_basis: int
    currency: Currency
    amount: Decimal
    rate_type: Literal["fixed", "floating"]
    reset_within_horizon: bool
    provenance: InputProvenance
    is_financial_forecast: Literal[False] = False


class ManagementActionResult(InternalModel):
    company_id: int
    event_id: int
    action_kind: Literal["rate_scenario", "investment_budget", "review_plan", "question"]
    status: Literal["complete", "needs_data", "insufficient_evidence"]
    analysis_kind: Literal["qualitative", "quantitative_sensitivity"] = "qualitative"
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    impacts: list[ActionImpact] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    proposed_actions: list[ActionProposal] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[ActionSourceRef] = Field(default_factory=list)
    company_features: list[KnownCompanyFeature] = Field(default_factory=list)
    provided_financial_inputs: FinancialInputs | None = None
    provided_plan: list[PlanItem] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    disclaimer: str
    report: dict[str, Any] = Field(default_factory=dict)
