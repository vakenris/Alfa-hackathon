"""Run-local call budgets, safe telemetry, and an explicitly injected runtime facade.

This module never reads environment variables, constructs a network client, or
keeps the latest report on a shared engine. A facade factory must return a fresh
client and source for each job. Calls on one observed client are serialized so
an adapter's ``last_*`` metadata cannot be attributed to another call.
"""

import asyncio
import inspect
from collections.abc import Callable, Collection
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.ml.interface import AgentTracerProtocol, MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse
from backend.app.llm.budget_guard import BudgetedChatCompletionsClient

from .api_client import (
    ChatCompletionsClient, LLMHTTPError, LLMOutputLimitError, LLMResponseError, LLMTimeoutError,
    LLMTransportError,
)
from .config import AgentConfig
from .llm import FakeLLMClient, LLMClientProtocol
from .models import AgentLLMError
from .tools import AnalyticsSourceProtocol, MockAnalyticsSource


UsageField = Literal["prompt_tokens", "completion_tokens", "total_tokens"]
USAGE_FIELDS: tuple[UsageField, ...] = (
    "prompt_tokens", "completion_tokens", "total_tokens",
)
ErrorCode = Literal[
    "CALL_LIMIT_EXCEEDED", "LLM_HTTP_ERROR", "LLM_TIMEOUT", "LLM_OUTPUT_LIMIT",
    "LLM_RESPONSE_ERROR", "LLM_TRANSPORT_ERROR", "LLM_CALL_FAILED", "CANCELLED",
    "RUN_TIMEOUT",
]
TimeoutKind = Literal[
    "http_timeout", "http_connect", "http_read", "http_write", "http_pool",
    "http_deadline", "unknown_timeout", "llm_deadline", "run_deadline",
]
TIMEOUT_KINDS = frozenset((
    "http_timeout", "http_connect", "http_read", "http_write", "http_pool",
    "http_deadline", "unknown_timeout", "llm_deadline", "run_deadline",
))


class SafeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class SafeCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1, strict=True)
    status: Literal["running", "ok", "error", "cancelled"] = "running"
    elapsed_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    usage: SafeUsage | None = None
    reasoning_tokens: int | None = Field(default=None, ge=0, strict=True)
    finish_reason: Literal["stop", "length", "other"] | None = None
    content_chars: int | None = Field(default=None, ge=0, strict=True)
    prompt_chars: int | None = Field(default=None, ge=0, strict=True)
    error_code: ErrorCode | None = None
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    timeout_kind: TimeoutKind | None = None
    cancellation_kind: Literal["external_or_unknown", "llm_deadline", "run_deadline"] | None = None


class ValidationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_number: int = Field(ge=1, strict=True)
    phase: Literal["gather", "draft", "repair", "reanalysis"]
    issue_codes: list[str]
    facts: int = Field(ge=0, strict=True)
    impacts: int = Field(ge=0, strict=True)


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls: int = Field(ge=1, strict=True)
    calls: list[SafeCallRecord] = Field(default_factory=list)
    elapsed_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    totals: SafeUsage = Field(default_factory=SafeUsage)
    known_subtotal: SafeUsage = Field(default_factory=SafeUsage)
    usage_complete: bool = False
    warnings: list[Literal["USAGE_INCOMPLETE", "CALL_LIMIT_EXCEEDED"]] = Field(default_factory=list)
    error_codes: list[ErrorCode] = Field(default_factory=list)
    validation_summary: dict[str, int | bool | None] = Field(default_factory=dict)
    sources_summary: dict[str, int | bool | None] = Field(default_factory=dict)
    validation_attempts: list[ValidationAttempt] = Field(default_factory=list)


class RuntimeCallLimitError(AgentLLMError):
    """No provider call was started because the run's hard budget was spent."""


def _safe_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _safe_usage(raw: object) -> SafeUsage | None:
    if type(raw) is not dict:
        return None
    return SafeUsage(**{key: _safe_int(raw.get(key)) for key in USAGE_FIELDS})


class RunTelemetry:
    """One mutable collector per run; reports are independent snapshots."""

    def __init__(self, *, max_calls: int):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.max_calls = max_calls
        self.calls: list[SafeCallRecord] = []
        self.validation_attempts: list[ValidationAttempt] = []
        self._limit_exceeded = False
        self._started_at = perf_counter()

    def start_call(self) -> SafeCallRecord:
        # Consume the slot synchronously, before any lock acquisition or await.
        if len(self.calls) >= self.max_calls:
            self._limit_exceeded = True
            raise RuntimeCallLimitError("Run LLM call budget exhausted.")
        call = SafeCallRecord(number=len(self.calls) + 1)
        self.calls.append(call)
        return call

    def record_deadline(self, kind: Literal["llm_deadline", "run_deadline"]) -> None:
        """The enclosing timeout owner can refine an observed cancellation."""
        if kind not in ("llm_deadline", "run_deadline"):
            raise ValueError("Unknown deadline owner")
        for call in reversed(self.calls):
            if call.status == "cancelled":
                call.timeout_kind = kind
                call.cancellation_kind = kind
                call.error_code = "LLM_TIMEOUT" if kind == "llm_deadline" else "RUN_TIMEOUT"
                break

    def build_report(self) -> RunReport:
        totals = {}
        subtotal = {}
        for key in USAGE_FIELDS:
            values = [getattr(call.usage, key) if call.usage is not None else None for call in self.calls]
            known = [value for value in values if value is not None]
            totals[key] = sum(known) if values and len(known) == len(values) else None
            subtotal[key] = sum(known) if known else None
        complete = bool(self.calls) and all(value is not None for value in totals.values())
        codes = list(dict.fromkeys(call.error_code for call in self.calls if call.error_code is not None))
        warnings = []
        if self.calls and not complete:
            warnings.append("USAGE_INCOMPLETE")
        if self._limit_exceeded:
            warnings.append("CALL_LIMIT_EXCEEDED")
            codes.append("CALL_LIMIT_EXCEEDED")
        return RunReport(
            max_calls=self.max_calls, calls=[call.model_copy(deep=True) for call in self.calls],
            elapsed_ms=round((perf_counter() - self._started_at) * 1000, 3),
            totals=SafeUsage(**totals), known_subtotal=SafeUsage(**subtotal),
            usage_complete=complete, warnings=warnings, error_codes=codes,
            validation_attempts=[item.model_copy(deep=True) for item in self.validation_attempts],
        )


class ObservedLLMClient:
    """Record allowlisted metadata only when its freshness can be established.

    Writable ``last_*`` fields are cleared before each attempt under the call
    lock. Read-only fields are unknown unless the provider supplies a changing
    ``last_attempt_id`` (an integer or string) for that attempt. The marker is
    used locally and never appears in reports.
    """

    _METADATA_FIELDS = ("last_usage", "last_finish_reason", "last_content_chars")

    def __init__(self, client: LLMClientProtocol, *, telemetry: RunTelemetry):
        self.client = client
        self.telemetry = telemetry
        self._call_lock = asyncio.Lock()

    def _read_metadata(self, name: str):
        try:
            return getattr(self.client, name, None)
        except (Exception, asyncio.CancelledError):
            # Optional adapter properties must not replace a response, failure
            # or cancellation with a second exception from diagnostics.
            return None

    def _reset_metadata(self) -> set[str]:
        cleared = set()
        for name in self._METADATA_FIELDS:
            try:
                setattr(self.client, name, None)
                if self._read_metadata(name) is None:
                    cleared.add(name)
            except (Exception, asyncio.CancelledError):
                pass
        return cleared

    def _capture_metadata(self, call: SafeCallRecord, cleared: set[str], previous_attempt) -> None:
        attempt = self._read_metadata("last_attempt_id")
        fresh_marker = type(attempt) in (int, str) and attempt != previous_attempt
        available = set(self._METADATA_FIELDS) if fresh_marker else cleared
        usage = self._read_metadata("last_usage") if "last_usage" in available else None
        call.usage = _safe_usage(usage)
        details = usage.get("completion_tokens_details") if type(usage) is dict else None
        call.reasoning_tokens = _safe_int(details.get("reasoning_tokens")) if type(details) is dict else None
        finish = self._read_metadata("last_finish_reason") if "last_finish_reason" in available else None
        call.finish_reason = (finish if finish in ("stop", "length") else "other") if type(finish) is str else None
        content_chars = self._read_metadata("last_content_chars") if "last_content_chars" in available else None
        call.content_chars = _safe_int(content_chars)

    async def generate(self, prompt: str) -> str:
        call = self.telemetry.start_call()
        call.prompt_chars = len(prompt)
        started_at = perf_counter()
        try:
            async with self._call_lock:
                previous_attempt = self._read_metadata("last_attempt_id")
                # Only simple scalar markers have safe comparison semantics.
                if type(previous_attempt) not in (int, str):
                    previous_attempt = None
                cleared = self._reset_metadata()
                try:
                    response = await self.client.generate(prompt)
                    call.status = "ok"
                    return response
                finally:
                    self._capture_metadata(call, cleared, previous_attempt)
        except asyncio.CancelledError:
            call.status = "cancelled"
            call.error_code = "CANCELLED"
            call.cancellation_kind = "external_or_unknown"
            raise
        except LLMTimeoutError as error:
            call.status = "error"
            call.error_code = "LLM_TIMEOUT"
            call.timeout_kind = error.timeout_kind if error.timeout_kind in TIMEOUT_KINDS else "unknown_timeout"
            raise
        except TimeoutError:
            call.status = "error"
            call.error_code = "LLM_TIMEOUT"
            call.timeout_kind = "unknown_timeout"
            raise
        except LLMHTTPError as error:
            call.status = "error"
            call.error_code = "LLM_HTTP_ERROR"
            call.http_status = error.status_code if type(error.status_code) is int and 100 <= error.status_code <= 599 else None
            raise
        except LLMOutputLimitError:
            call.status = "error"
            call.error_code = "LLM_OUTPUT_LIMIT"
            raise
        except LLMResponseError:
            call.status = "error"
            call.error_code = "LLM_RESPONSE_ERROR"
            raise
        except LLMTransportError:
            call.status = "error"
            call.error_code = "LLM_TRANSPORT_ERROR"
            raise
        except Exception:
            call.status = "error"
            call.error_code = "LLM_CALL_FAILED"
            raise
        finally:
            call.elapsed_ms = round((perf_counter() - started_at) * 1000, 3)


class RuntimeAgentEngine:
    """Grounded production facade with fresh, explicitly supplied dependencies."""

    def __init__(
        self, *, client_factory: Callable[[], LLMClientProtocol],
        source_factory: Callable[[], AnalyticsSourceProtocol], config: AgentConfig | None = None,
        allow_test_dependencies: bool = False,
        approved_model_ids: Collection[str] | None = None,
    ):
        selected_config = config if config is not None else AgentConfig()
        if any(not callable(factory) or inspect.iscoroutinefunction(factory)
               for factory in (client_factory, source_factory)):
            raise ValueError("Runtime factories must synchronously return explicit dependencies.")
        if approved_model_ids is not None and (
            isinstance(approved_model_ids, (str, bytes))
            or not isinstance(approved_model_ids, Collection)
            or any(type(model) is not str or not model.strip() for model in approved_model_ids)
        ):
            raise ValueError("approved_model_ids must contain explicit nonempty model identifiers.")
        self.config = selected_config
        self._client_factory = client_factory
        self._source_factory = source_factory
        self._allow_test_dependencies = allow_test_dependencies
        self._approved_model_ids = frozenset(approved_model_ids) if approved_model_ids is not None else None

    def _new_engine(self):
        from .orchestrator import AgentInsightEngine

        client = self._client_factory()
        source = self._source_factory()
        for dependency, method_name in ((client, "generate"), (source, "search")):
            try:
                method = getattr(dependency, method_name, None)
            except Exception:
                method = None
            if dependency is None or not inspect.iscoroutinefunction(method):
                raise ValueError("Runtime dependencies must provide async generate and search methods.")
        if not self._allow_test_dependencies and (
            isinstance(client, FakeLLMClient) or isinstance(source, MockAnalyticsSource)
        ):
            raise ValueError("RuntimeAgentEngine requires production dependencies; offline fixtures must be explicitly enabled.")
        if not self._allow_test_dependencies and isinstance(client, ChatCompletionsClient):
            if self._approved_model_ids is None or client.config.model not in self._approved_model_ids:
                raise ValueError("Production ChatCompletionsClient requires an explicitly approved model identifier.")
        if not self._allow_test_dependencies and not isinstance(client, BudgetedChatCompletionsClient):
            raise ValueError("Production runtime requires the shared BudgetedChatCompletionsClient and team budget guard.")
        return AgentInsightEngine(client, analytics_source=source, config=self.config)

    async def generate_insight(
        self, company: CompanyResponse, event: NewsArticleResponse,
        counterparties: list[CounterpartyItem], tracer: AgentTracerProtocol,
    ) -> MLInsightResult:
        return await self._new_engine().generate_insight(company, event, counterparties, tracer)

    async def generate_analysis(
        self, company: CompanyResponse, event: NewsArticleResponse,
        counterparties: list[CounterpartyItem], tracer: AgentTracerProtocol,
        *, financial_inputs=None, loan_sensitivity=None,
    ):
        return await self._new_engine().generate_analysis(
            company, event, counterparties, tracer,
            financial_inputs=financial_inputs, loan_sensitivity=loan_sensitivity,
        )
