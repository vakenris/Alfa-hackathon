
"""Local data and news test stand. No network unless --live AND --allow-live.

Place in backend/app/agent and run from repository root:
  python -m backend.app.agent.local_test_harness list
  python -m backend.app.agent.local_test_harness inspect
  python -m backend.app.agent.local_test_harness validate --case rate_hold_2026_10_23 --company examples/demo_company.json

See ALFA_TEST_README.md for the live flow. This module never reads .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .api_client import ChatCompletionsClient
from .backend_source import BackendAnalyticsSource
from .config import AgentConfig, ChatCompletionsConfig
from .models import AnalyticsDocument
from .orchestrator import AgentInsightEngine
from .tracer import RecordingTracer

DEFAULT_CASES = Path(__file__).parent / "evaluation" / "real_news_cases.json"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_case(path: Path, case_id: str) -> dict:
    cases = load_json(path)
    matches = [case for case in cases if case["case_id"] == case_id]
    if len(matches) != 1:
        raise ValueError(f"Не найден уникальный case_id: {case_id}")
    return matches[0]


def schemas():
    for name, model in (("CompanyResponse", CompanyResponse),
                        ("CounterpartyItem", CounterpartyItem),
                        ("NewsArticleResponse", NewsArticleResponse),
                        ("AnalyticsDocument", AnalyticsDocument)):
        print(f"\n{name}:")
        for field_name, definition in model.model_fields.items():
            status = "обязательно" if definition.is_required() else "необязательно"
            print(f"  {field_name}: {status}")


def load_inputs(args):
    case = get_case(args.cases, args.case)
    event = NewsArticleResponse.model_validate(case["event"])
    company = CompanyResponse.model_validate(load_json(args.company))
    counterparties = (
        [CounterpartyItem.model_validate(item) for item in load_json(args.counterparties)]
        if args.counterparties else []
    )
    bank_documents = (
        [AnalyticsDocument.model_validate(item) for item in load_json(args.bank_documents)]
        if args.bank_documents else []
    )
    # Restrict the test corpus to material already public by the event timestamp.
    # Older materials may still be historical; do not claim that their forecasts are current.
    for document in bank_documents:
        if document.article.published_at > event.published_at:
            raise ValueError("Банковский документ опубликован ПОЗЖЕ выбранной новости")
        if not document.article.is_bank_analytics:
            raise ValueError("Банковский документ не отмечен как is_bank_analytics")
    return case, company, event, counterparties, bank_documents


def show_overview(case, company, event, counterparties, bank_documents):
    print("\nСЦЕНАРИЙ", case["case_id"])
    print("Новость:", event.title)
    print("Дата:", event.published_at.isoformat())
    print("Первоисточник для ручной проверки:", case["reference_url"])
    print("Компания (id):", company.id)
    print("Платёжных связей в выборке:", len(counterparties))
    print("Банковских документов:", len(bank_documents))
    print("Критерий ручной проверки:", case["evaluation_question"])
    if bank_documents:
        verified = sum(doc.provenance_status == "verified" and bool(doc.source_document_id)
                       for doc in bank_documents)
        print("Документов с заявленной verified provenance и ID:", verified)
        print("ВНИМАНИЕ: статус verified должен подтверждаться доверенным реестром, не этим скриптом.")
    print("ВНИМАНИЕ: reference_url для новости хранится только в тестовом каталоге. "
          "Публичный DTO news может не передавать его в EvidenceItem.")


def live_config() -> ChatCompletionsConfig:
    # Explicit env, not .env, and no silent fallbacks to other models or gateways.
    names = ("AGENT_LLM_API_URL", "AGENT_LLM_MODEL", "AGENT_LLM_AUTH_HEADER", "AGENT_LLM_AUTH_VALUE")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ValueError("Для live-теста явно задай переменные: " + ", ".join(missing))
    return ChatCompletionsConfig.from_env()


async def run_agent(args, company, event, counterparties, bank_documents):
    llm_config = live_config()
    # The runner can use either mock bank search (only data-flow) or the real
    # bounded lexical source against preloaded documents (tests local retrieval).
    if args.retrieval == "lexical":
        async def loader(limit: int):
            return [doc.model_copy(deep=True) for doc in bank_documents[:limit]]
        source = BackendAnalyticsSource(document_loader=loader)
    else:
        from .tools import MockAnalyticsSource
        source = MockAnalyticsSource(bank_documents)
    client = ChatCompletionsClient(llm_config)
    engine = AgentInsightEngine(
        llm_client=client,
        analytics_source=source,
        config=AgentConfig(    analysis_mode="grounded",max_steps=4,max_llm_calls=4,max_tool_calls=3,max_recovery_attempts=1,llm_timeout_seconds=180,run_timeout_seconds=400,max_response_chars=80000,),
    )
    tracer = RecordingTracer()
    result = await engine.generate_analysis(company, event, counterparties, tracer)
    return result, tracer


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальный стенд агента. По умолчанию без платных вызовов.")
    parser.add_argument("command", choices=("list", "inspect", "validate", "run"))
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case", default="rate_hold_2026_10_23")
    parser.add_argument("--company", type=Path)
    parser.add_argument("--counterparties", type=Path)
    parser.add_argument("--bank-documents", type=Path)
    parser.add_argument("--retrieval", choices=("lexical", "mock"), default="lexical")
    parser.add_argument("--live", action="store_true", help="Запрос к внешней LLM")
    parser.add_argument("--allow-live", action="store_true", help="Подтверждение расхода разрешённого API")
    parser.add_argument("--out", type=Path, help="JSON-результат; может содержать бизнес-данные")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for case in load_json(args.cases):
                print(case["case_id"], "—", case["event"]["title"])
            return 0
        if args.command == "inspect":
            schemas()
            return 0
        if args.company is None:
            parser.error("--company обязателен для validate/run. Используй демонстрационный или реальный профиль в JSON")
        case, company, event, counterparties, bank_documents = load_inputs(args)
        show_overview(case, company, event, counterparties, bank_documents)
        if args.command == "validate":
            print("\nOK: JSON соответствует backend DTO, сеть и LLM не использованы.")
            return 0
        if not (args.live and args.allow_live):
            parser.error("Для запуска LLM нужны ОБА флага: --live --allow-live. Команда validate всегда офлайн.")
        result, tracer = asyncio.run(run_agent(args, company, event, counterparties, bank_documents))
        output = result.model_dump(mode="json")
        for field in ("what_happened", "why_important", "consequences", "bank_view", "ai_view", "recommended_action"):
            print(f"\n===== {field} =====\n{getattr(result.insight, field)}")
        print("\nПроверка:", output["verification"])
        print("Вызовы:", output["report"].get("calls"))
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps({"analysis": output, "trace": tracer.steps, "case_id": case["case_id"]},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
            print("JSON сохранён:", args.out)
            print("Не публикуй файл: в нём могут быть данные клиентов и гипотезы.")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"INPUT ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"RUN ERROR: {type(error).__name__}", file=sys.stderr)

        report = getattr(error, "run_report", None)

        if isinstance(report, dict):
            print("Коды ошибок:", report.get("error_codes"))

            for call in report.get("calls", []):
                print({
                    "call": call.get("number"),
                    "error_code": call.get("error_code"),
                    "http_status": call.get("http_status"),
                    "timeout_kind": call.get("timeout_kind"),
                    "finish_reason": call.get("finish_reason"),
                    "usage": call.get("usage"),
                    "elapsed_ms": call.get("elapsed_ms"),
                })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
