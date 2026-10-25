"""Full engine smoke check: HTTP mock by default, at most two calls with --send.

The document check requires an exact quote from a unique synthetic tool result.
It tests data flow and citation, not semantic fact-checking or real bank research.
No prompts, responses, credentials, headers or raw exceptions enter the report.
"""

import argparse
import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

import httpx
from pydantic import ValidationError

from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .api_client import ChatCompletionsClient, LLMAPIError, LLMHTTPError, LLMTimeoutError
from .config import AgentConfig, ChatCompletionsConfig
from .models import (
    AgentError, AgentEvidenceError, AgentLLMError, AgentLLMTimeoutError,
    AgentResponseError, AgentTimeoutError, AnalyticsDocument, ToolAction,
)
from .orchestrator import CompatibilityInsightEngine as AgentInsightEngine
from .prompts import COMPACT_FINALIZATION_RULES
from .smoke_check import API_URL, MODEL
from .tools import MockAnalyticsSource
from .tracer import RecordingTracer


MAX_LLM_CALLS = 2
DEFAULT_MAX_OUTPUT_TOKENS = 1536
MAX_OUTPUT_TOKENS = 8192
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_LLM_TIMEOUT_SECONDS = 40
DEFAULT_RUN_TIMEOUT_SECONDS = 90
MAX_HTTP_TIMEOUT_SECONDS = 120
MAX_LLM_TIMEOUT_SECONDS = 130
MAX_RUN_TIMEOUT_SECONDS = 300
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
PLANNING_INSTRUCTION = (
    "Проверочный сценарий: на первом шаге выбери get_bank_analytics с limit=1. "
    "Позиция банка пока неизвестна. Верни настоящее действие tool по схеме, "
    "не итог и не summarize_counterparties.\n"
)
FINAL_INSTRUCTION = (
    COMPACT_FINALIZATION_RULES
    + "Проверочный сценарий: relevant_bank_product=null. "
    "В bank_view дословно процитируй полное предложение из content полученного "
    "аналитического документа с короткой пометкой, что он синтетический. "
    "В evidences добавь ровно один элемент — этот документ с точными "
    "source_title и source_url из sources; claim должен дословно совпадать "
    "с процитированным предложением, company_feature=null. "
    "Не копируй вместо цитаты заголовок и не повторяй цитату в других полях, "
    "кроме обязательных bank_view и claim. Собственный вывод помести в ai_view.\n"
)


@dataclass(frozen=True)
class SmokeScenario:
    company: CompanyResponse
    event: NewsArticleResponse
    counterparties: list[CounterpartyItem]
    document: AnalyticsDocument
    quote: str


def make_scenario() -> SmokeScenario:
    # The nonce/fact is not present in the initial company, event or instructions.
    quote = f"Тестовый ориентир {uuid4().hex[:12]}: резерв ликвидности составляет 17 дней."
    published_at = datetime(2026, 10, 21, tzinfo=timezone.utc)
    return SmokeScenario(
        company=CompanyResponse(
            id=1, synthetic_u="ENGINE_SMOKE_COMPANY", name="Синтетический завод",
            mainokved_code="24.10", cl_product_loa_mark=True,
        ),
        event=NewsArticleResponse(
            id=10, title="Условное повышение ставки", source="Синтетический источник",
            published_at=published_at, category="Тестовая экономика",
            content="Тестовая новость: ставка повышена. Условия кредитов завода неизвестны.",
        ),
        counterparties=[
            CounterpartyItem(synthetic_u="SMOKE_SUPPLIER", direction="SUPPLIER", total_amount=1000, transaction_count=2),
            CounterpartyItem(synthetic_u="SMOKE_CUSTOMER", direction="CUSTOMER", total_amount=1500, transaction_count=3),
        ],
        document=AnalyticsDocument(
            article=NewsArticleResponse(
                id=20, title="Синтетический банковский обзор для проверки агента",
                source="Тестовая фикстура, не публикация Альфа-Банка",
                published_at=published_at, category="Тестовая аналитика",
                content=quote, is_bank_analytics=True,
            ),
            source_url="https://example.invalid/engine-smoke/analytics/20",
        ),
        quote=quote,
    )


def validate_output_token_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_OUTPUT_TOKENS:
        raise ValueError("Output token limit must be an integer between 1 and 8192")
    return value


def validate_timeout(value: float, maximum: float) -> float:
    if type(value) not in (int, float) or not 0 < value <= maximum or not math.isfinite(value):
        raise ValueError("Smoke timeout must be finite, positive and within its explicit ceiling")
    return value


def elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def engine_smoke_config(
    *, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> ChatCompletionsConfig:
    """Read credentials only from process environment; never load .env files."""
    return ChatCompletionsConfig(
        api_url=os.environ.get("AGENT_LLM_API_URL", API_URL),
        model=os.environ.get("AGENT_LLM_MODEL", MODEL),
        auth_header=os.environ.get("AGENT_LLM_AUTH_HEADER"),
        auth_value=os.environ.get("AGENT_LLM_AUTH_VALUE"),
        timeout_seconds=validate_timeout(http_timeout_seconds, MAX_HTTP_TIMEOUT_SECONDS),
        max_output_tokens=validate_output_token_limit(max_output_tokens),
        token_limit_field=os.environ.get("AGENT_LLM_TOKEN_LIMIT_FIELD", "max_tokens"),
    )


class ObservedMockAnalyticsSource(MockAnalyticsSource):
    def __init__(self, document: AnalyticsDocument):
        super().__init__([document])
        self.calls = 0
        self.document_returned = False

    async def search(self, query, *, company, event, limit):
        self.calls += 1
        documents = await super().search(query, company=company, event=event, limit=limit)
        self.document_returned = bool(documents)
        return documents


class SmokeCallLimitError(AgentError):
    pass


def safe_usage(raw: dict | None) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    return {key: raw[key] for key in USAGE_FIELDS if type(raw.get(key)) is int and raw[key] >= 0}


def safe_reasoning_tokens(raw: dict | None) -> int | None:
    """Read only the nested counter, without inferring or double counting."""
    details = raw.get("completion_tokens_details") if isinstance(raw, dict) else None
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return value if type(value) is int and value >= 0 else None


def document_in_context(prompt: str, scenario: SmokeScenario) -> bool:
    try:
        context = json.loads(prompt.rsplit("Входные данные (JSON):\n", 1)[1])
        for observation in context["observations"]:
            if observation["tool"] != "get_bank_analytics" or observation["status"] != "ok":
                continue
            for document in observation["data"]["documents"]:
                article = document["article"]
                if (
                    article["id"] == scenario.document.article.id
                    and article["title"] == scenario.document.article.title
                    and article["content"] == scenario.quote
                    and document["source_url"] == str(scenario.document.source_url)
                ):
                    return True
    except (ValueError, IndexError, KeyError, TypeError):
        pass
    return False


class BoundedSmokeClient:
    """A run-local protocol adapter; failed calls consume the same hard budget."""

    def __init__(self, client: ChatCompletionsClient, scenario: SmokeScenario):
        self.client = client
        self.scenario = scenario
        self.calls: list[dict] = []
        self.selected_tool: str | None = None
        self.finalization_seen = False
        self.document_seen = False
        self.limit_exceeded = False

    async def generate(self, prompt: str) -> str:
        # No await between checking and consuming the slot, including concurrent callers.
        if len(self.calls) >= MAX_LLM_CALLS:
            self.limit_exceeded = True
            raise SmokeCallLimitError("Engine smoke check call limit reached")
        started_at = perf_counter()
        call = {
            "number": len(self.calls) + 1, "status": "error", "usage": None,
            "timeout_kind": None, "cancellation_kind": None,
        }
        self.calls.append(call)
        first = call["number"] == 1
        if not first:
            self.finalization_seen = "Режим: финализация." in prompt
            self.document_seen = document_in_context(prompt, self.scenario)
        try:
            response = await self.client.generate(
                (PLANNING_INSTRUCTION if first else FINAL_INSTRUCTION) + prompt,
            )
            call["status"] = "ok"
            if first:
                try:
                    self.selected_tool = ToolAction.model_validate_json(response).tool
                except ValidationError:
                    pass
            return response
        except LLMHTTPError as error:
            call.update(error="LLMHTTPError", http_status=error.status_code)
            raise
        except LLMTimeoutError as error:
            call.update(error="LLMTimeoutError", timeout_kind=error.timeout_kind)
            raise
        except LLMAPIError as error:
            # Only fixed adapter exception names, never exception text or server bodies.
            call["error"] = type(error).__name__
            raise
        except asyncio.CancelledError:
            # The enclosing deadline owner refines this after its context exits.
            call.update(status="cancelled", cancellation_kind="external_or_unknown")
            raise
        except Exception:
            call["error"] = "ClientError"
            raise
        finally:
            call["elapsed_ms"] = elapsed_ms(started_at)
            call["usage"] = safe_usage(self.client.last_usage)
            call["reasoning_tokens"] = safe_reasoning_tokens(self.client.last_usage)
            call["finish_reason"] = self.client.last_finish_reason
            call["content_chars"] = self.client.last_content_chars

    def record_deadline(self, kind: str) -> None:
        if self.calls and self.calls[-1]["status"] == "cancelled":
            self.calls[-1].update(timeout_kind=kind, cancellation_kind=kind)


def mock_final_result(scenario: SmokeScenario) -> dict:
    """Expected fake completion, used only by the HTTP-mock transport."""
    return {
        "what_happened": "В условной новости повышена ставка.",
        "why_important": "У компании отмечены кредитные продукты.",
        "consequences": "Расходы зависят от условий договоров.",
        "bank_view": f"Синтетический материал: {scenario.quote}",
        "ai_view": "Ориентир обзора не описывает кредиты компании.",
        "recommended_action": "Проверить условия кредитных договоров.",
        "relevant_bank_product": None,
        "evidences": [{
            "claim": scenario.quote, "source_title": scenario.document.article.title,
            "source_url": str(scenario.document.source_url), "company_feature": None,
        }],
    }


def mock_transport(scenario: SmokeScenario) -> httpx.MockTransport:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests > MAX_LLM_CALLS:
            raise SmokeCallLimitError("HTTP mock call limit reached")
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        if requests == 1:
            if scenario.quote in prompt:
                raise ValueError("Document must not appear before tool execution")
            content = {"action": "tool", "tool": "get_bank_analytics", "arguments": {"limit": 1}}
        else:
            if not document_in_context(prompt, scenario):
                raise ValueError("Tool result missing from final context")
            content = mock_final_result(scenario)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False, separators=(",", ":"))}}],
        })

    return httpx.MockTransport(handler)


async def run_check(
    config: ChatCompletionsConfig, *, send: bool = False,
    transport: httpx.MockTransport | None = None,
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
) -> dict:
    if send and config.auth_header is None:
        raise ValueError("Authentication must be configured before a live check")
    if transport is not None and not isinstance(transport, httpx.MockTransport):
        raise ValueError("Only HTTP-mock transport overrides are allowed")
    validate_output_token_limit(config.max_output_tokens)
    validate_timeout(config.timeout_seconds, MAX_HTTP_TIMEOUT_SECONDS)
    validate_timeout(llm_timeout_seconds, MAX_LLM_TIMEOUT_SECONDS)
    validate_timeout(run_timeout_seconds, MAX_RUN_TIMEOUT_SECONDS)

    started_at = perf_counter()
    scenario = make_scenario()
    source = ObservedMockAnalyticsSource(scenario.document)
    selected_transport = transport if transport is not None else (None if send else mock_transport(scenario))
    client = BoundedSmokeClient(ChatCompletionsClient(config, transport=selected_transport), scenario)
    tracer = RecordingTracer()
    engine = AgentInsightEngine(client, analytics_source=source, config=AgentConfig(
        max_steps=2, max_llm_calls=2, max_tool_calls=1,
        llm_timeout_seconds=llm_timeout_seconds, run_timeout_seconds=run_timeout_seconds,
        max_context_chars=8000, max_tool_result_chars=2000, max_article_chars=1000,
    ))
    report = {
        "mode": "live" if send else "mock", "success": False,
        "max_llm_calls": MAX_LLM_CALLS, "max_output_tokens_per_call": config.max_output_tokens,
        "automatic_retries": 0, "llm_calls": 0, "calls": client.calls,
        "timeouts_seconds": {
            "http": config.timeout_seconds, "llm": llm_timeout_seconds, "run": run_timeout_seconds,
        },
        "timeout_kind": None,
        "selected_tool": None, "tool_calls": 0,
        "checks": {key: False for key in (
            "tool_selected", "tool_result_received", "forced_finalization", "final_json_valid",
            "document_quote_used", "document_evidence_valid",
        )},
        "usage_complete": False, "total_usage": None, "error": None,
        "document_check": "exact_quote_from_synthetic_document_not_fact_checking",
    }
    result: MLInsightResult | None = None
    try:
        result = await engine.generate_insight(scenario.company, scenario.event, scenario.counterparties, tracer)
    except AgentResponseError:
        report["error"] = "InvalidModelResponse"
    except AgentEvidenceError:
        report["error"] = "InvalidEvidence"
    except AgentLLMTimeoutError:
        report.update(error="LLMCallTimeout", timeout_kind="llm_deadline")
        client.record_deadline("llm_deadline")
    except AgentTimeoutError:
        report.update(error="ScenarioTimeout", timeout_kind="scenario_deadline")
        client.record_deadline("scenario_deadline")
    except AgentLLMError:
        report["error"] = "LLMCallFailed"
    except SmokeCallLimitError:
        report["error"] = "CallLimitExceeded"
    except AgentError:
        report["error"] = "AgentFailed"
    except Exception:
        report["error"] = "CheckFailed"

    report.update(llm_calls=len(client.calls), selected_tool=client.selected_tool, tool_calls=source.calls)
    checks = report["checks"]
    checks["tool_selected"] = client.selected_tool == "get_bank_analytics"
    checks["tool_result_received"] = source.calls == 1 and source.document_returned and client.document_seen
    checks["forced_finalization"] = len(client.calls) == 2 and client.finalization_seen
    if result is not None:
        checks["final_json_valid"] = True  # Already parsed and validated by the actual engine.
        checks["document_quote_used"] = client.document_seen and scenario.quote in result.bank_view
        checks["document_evidence_valid"] = checks["tool_result_received"] and len(result.evidences) == 1 and any(
            item.source_title == scenario.document.article.title
            and item.source_url == str(scenario.document.source_url)
            and item.claim.strip() == scenario.quote
            for item in result.evidences
        )
    if client.limit_exceeded:
        report["error"] = "CallLimitExceeded"
    elif report["timeout_kind"] is None and client.calls and "error" in client.calls[-1]:
        report["error"] = client.calls[-1]["error"]
    if report["timeout_kind"] is None and client.calls:
        report["timeout_kind"] = client.calls[-1]["timeout_kind"]
    report["success"] = report["error"] is None and all(checks.values())
    if not report["success"] and report["error"] is None:
        report["error"] = "ScenarioNotCompleted"

    usages = [call["usage"] for call in client.calls]
    report["usage_complete"] = bool(usages) and all(
        usage is not None and all(key in usage for key in USAGE_FIELDS) for usage in usages
    )
    if usages and any(usage is not None for usage in usages):
        report["total_usage"] = {
            key: sum(usage[key] for usage in usages) if all(usage is not None and key in usage for usage in usages) else None
            for key in USAGE_FIELDS
        }
    report["elapsed_ms"] = elapsed_ms(started_at)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Send at most two paid requests; requires explicit approval")
    parser.add_argument(
        "--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS, metavar="1..8192",
        help="Output token limit for each of the two calls (default: 1536, maximum: 8192); never raised automatically",
    )
    for flag, default, maximum, description in (
        ("--http-timeout-seconds", DEFAULT_HTTP_TIMEOUT_SECONDS, MAX_HTTP_TIMEOUT_SECONDS, "HTTP request"),
        ("--llm-timeout-seconds", DEFAULT_LLM_TIMEOUT_SECONDS, MAX_LLM_TIMEOUT_SECONDS, "Orchestrator LLM call"),
        ("--run-timeout-seconds", DEFAULT_RUN_TIMEOUT_SECONDS, MAX_RUN_TIMEOUT_SECONDS, "Whole scenario"),
    ):
        parser.add_argument(
            flag, type=float, default=default, metavar="SECONDS",
            help=f"{description} timeout (default: {default}, maximum: {maximum}); never raised automatically",
        )
    args = parser.parse_args(argv)
    started_at = perf_counter()
    try:
        report = asyncio.run(run_check(
            engine_smoke_config(
                max_output_tokens=args.max_output_tokens, http_timeout_seconds=args.http_timeout_seconds,
            ), send=args.send, llm_timeout_seconds=args.llm_timeout_seconds,
            run_timeout_seconds=args.run_timeout_seconds,
        ))
    except (ValidationError, ValueError):
        report = {"success": False, "llm_calls": 0, "error": "ConfigurationError"}
        print(json.dumps(report))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({
            "success": False, "error": "Interrupted", "cancellation_kind": "keyboard_interrupt",
            "elapsed_ms": elapsed_ms(started_at),
        }))
        return 130
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
