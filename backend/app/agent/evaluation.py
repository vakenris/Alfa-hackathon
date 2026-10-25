"""Offline technical evaluation with explicitly scripted, synthetic drafts.

These cases exercise verification and projection, not LLM intelligence. No model
is asked to invent an answer: each FakeLLMClient returns a hand-authored draft.
The module reads no files, environment variables, databases or URLs. The CLI
prints only safe aggregate counts and verification limitations.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse

from .models import AgentAnalysisResult, BANK_VIEW_UNAVAILABLE, DISCLAIMER, DraftInsight


EVALUATION_VERSION = "offline-technical-v1"
VERIFICATION_LIMITS = (
    "SCRIPTED_FIXTURES_DO_NOT_MEASURE_LLM_INTELLIGENCE",
    "RESULT_METADATA_CANNOT_RECHECK_QUOTES_AGAINST_SOURCE_TEXT",
    "LEXICAL_RULES_DO_NOT_PROVE_CAUSAL_ENTAILMENT",
    "SOURCE_PROVENANCE_METADATA_REQUIRES_TRUSTED_INGESTION",
    "HUMAN_EXPERT_REVIEW_REQUIRED",
)
HUMAN_RUBRIC = (
    "event_relevance_to_company",
    "causal_mechanism_and_assumptions",
    "economic_materiality_and_missing_data",
    "faithful_source_interpretation_and_temporal_scope",
    "management_action_feasibility",
)


@dataclass(frozen=True)
class FixtureExpectation:
    """Fixture labels and expert questions, not machine-assigned quality grades."""

    applicability: Literal["conditional", "insufficient"]
    channels: tuple[str, ...]
    human_focus: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    company: CompanyResponse
    event: NewsArticleResponse
    draft: DraftInsight
    expectation: FixtureExpectation


class AnalysisEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = EVALUATION_VERSION
    evaluation_kind: Literal["technical_structure_only"] = "technical_structure_only"
    llm_intelligence_measured: Literal[False] = False
    technical_checks: dict[str, bool | None]
    counts: dict[str, int | None]
    fixture_expectations: dict[str, bool]
    human_review: dict[str, Literal["required"]]
    limitations: list[str]


def evaluate_analysis(
    result: AgentAnalysisResult, *, expected: FixtureExpectation | None = None,
) -> AnalysisEvaluation:
    """Inspect an already-produced result without rerunning a model or verifier.

    A passing verification flag is reported as such, not treated as semantic
    proof. The companion DTO has source metadata but not all visible source
    text, so this function cannot independently recheck quotation substrings.
    Missing telemetry is unknown (None), never a measured zero or a pass.
    No combined quality score or automatic human-rubric grade is produced.
    """
    if not isinstance(result, AgentAnalysisResult):
        raise TypeError("result must be AgentAnalysisResult")
    facts = result.analysis.facts
    impacts = result.analysis.impacts
    actions = result.analysis.actions
    source_ids = {source.source_id for source in result.sources}
    fact_ids = {fact.id for fact in facts}
    impact_ids = {impact.id for impact in impacts}
    known_features = set(result.business_context.known_features)
    by_source = {source.source_id: source for source in result.sources}
    bank_facts = [fact for fact in facts if fact.kind == "bank_statement"]
    calls = result.report.get("calls")
    max_calls = result.report.get("max_calls")
    valid_calls = isinstance(calls, list)
    known_budget = type(max_calls) is int and max_calls >= 1 and valid_calls
    budget_respected = len(calls) <= max_calls if known_budget else None
    has_no_bank_position = result.insight.bank_view == BANK_VIEW_UNAVAILABLE
    checks = {
        "reported_verification_passed": result.verification.passed,
        "reported_verification_has_no_issues": not result.verification.issues,
        "source_ids_are_unique": len(source_ids) == len(result.sources),
        "fact_ids_are_unique": len(fact_ids) == len(facts),
        "impact_ids_are_unique": len(impact_ids) == len(impacts),
        "fact_sources_exist": all(fact.source_id in source_ids for fact in facts),
        "event_anchor_present": any(fact.source_id == f"news:{result.event_id}" for fact in facts),
        "impact_fact_references_exist": all(set(impact.fact_ids) <= fact_ids for impact in impacts),
        "impact_features_are_known": all(set(impact.company_features) <= known_features for impact in impacts),
        "action_impact_references_exist": all(set(action.impact_ids) <= impact_ids for action in actions),
        "bank_fact_provenance_metadata_present": all(
            fact.source_id in by_source and by_source[fact.source_id].is_bank_analytics
            and by_source[fact.source_id].provenance_status == "verified"
            and bool(by_source[fact.source_id].source_document_id) and fact.field == "content"
            for fact in bank_facts
        ),
        "bank_position_projection_consistent": not has_no_bank_position if bank_facts else has_no_bank_position,
        "insufficient_analysis_has_no_impacts_or_actions": (
            not impacts and not actions and bool(result.analysis.missing_data)
            if result.analysis.applicability == "insufficient" else True
        ),
        "disclaimer_present": DISCLAIMER in result.insight.recommended_action,
        "product_not_invented": result.insight.relevant_bank_product is None,
        "per_run_call_limit_respected": budget_respected,
    }
    fixture_checks = {}
    if expected is not None:
        fixture_checks = {
            "applicability_matches_fixture": result.analysis.applicability == expected.applicability,
            "channels_match_fixture": set(impact.channel for impact in impacts) == set(expected.channels),
        }
    return AnalysisEvaluation(
        technical_checks=checks,
        counts={
            "facts": len(facts), "impacts": len(impacts), "actions": len(actions),
            "sources": len(result.sources), "bank_statement_facts": len(bank_facts),
            "verification_issues": len(result.verification.issues),
            "verification_warnings": len(result.verification.warnings),
            "recorded_calls": len(calls) if valid_calls else None,
            "known_company_features": len(known_features),
            "unknown_company_features": len(result.business_context.unknown_features),
        },
        fixture_expectations=fixture_checks,
        human_review={label: "required" for label in HUMAN_RUBRIC},
        limitations=list(VERIFICATION_LIMITS),
    )


def _case(
    number: int, name: str, title: str, company_fields: dict, *, channel: str | None,
    mechanism: str = "", assumption: str = "", need: str, action: str = "",
    feature: str = "", human_focus: tuple[str, ...],
) -> EvaluationCase:
    """Serialize supplied fixture text; no event-to-impact inference is performed."""
    company = CompanyResponse(id=number, synthetic_u=f"SYNTHETIC_{number}",
                              name="Синтетическая компания", **company_fields)
    event = NewsArticleResponse(
        id=number + 100, title=title, content=title, source="Синтетическая тестовая фикстура",
        category="Тест", published_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )
    applicability = "conditional" if channel else "insufficient"
    draft = DraftInsight.model_validate({
        "action": "draft", "applicability": applicability,
        "facts": [{"id": "event", "source_id": f"news:{event.id}", "quote": event.content}],
        "impacts": [] if channel is None else [{
            "id": "impact", "channel": channel, "direction": "mixed", "mechanism": mechanism,
            "fact_ids": ["event"], "company_features": [feature], "assumptions": [assumption],
            "data_needs": [need], "horizon": "unknown",
        }],
        "actions": [] if channel is None else [{
            "title": action, "impact_ids": ["impact"], "required_data": [need], "owner_role": "ANALYST",
        }],
        "missing_data": [need],
    })
    return EvaluationCase(name, company, event, draft, FixtureExpectation(
        applicability=applicability, channels=() if channel is None else (channel,),
        human_focus=human_focus,
    ))


def build_evaluation_cases() -> list[EvaluationCase]:
    """Six independent, hand-authored scenarios; all content is synthetic."""
    return [
        _case(1, "rate", "Регулятор сохранил ключевую ставку без изменений.",
              {"cl_product_loa_mark": True}, channel="financing", feature="cl_product_loa_mark",
              mechanism="Уровень ключевой ставки может влиять на стоимость нового кредитного финансирования.",
              assumption="Компания рассматривает новое заёмное финансирование.",
              need="Условия новых кредитов и потребность в финансировании.",
              action="Сопоставить предложения по новому финансированию с потребностью компании.",
              human_focus=("Не выводить изменение существующих платежей из неизменной ставки.",
                           "Флаг кредита не раскрывает договорные условия.")),
        _case(2, "logistics", "На выбранном направлении выросли ставки фрахта.",
              {"mainokved_code": "52.29"}, channel="logistics", feature="mainokved_code",
              mechanism="Удорожание перевозки может повышать затраты на логистику.",
              assumption="Компания использует затронутое направление перевозок.",
              need="Используемые маршруты и условия транспортных договоров.",
              action="Проверить затронутые маршруты и альтернативные варианты доставки.",
              human_focus=("Отрасль сама по себе не подтверждает использование маршрута.",)),
        _case(3, "equipment", "Объявлена программа поддержки закупки промышленного оборудования.",
              {"cl_product_lea_mark": True}, channel="investment", feature="cl_product_lea_mark",
              mechanism="Поддержка может изменить бюджет инвестиционного проекта по модернизации.",
              assumption="Планируемый проект соответствует требованиям программы.",
              need="Правила программы и план капитальных затрат.",
              action="Проверить применимость программы к планируемому проекту модернизации.",
              human_focus=("Участие в программе и экономия не считаются подтвержденными.",)),
        _case(4, "fx", "На рынке выросла волатильность курса юаня.",
              {"fts_flag": True, "fts_country_name": "Китай"}, channel="fx", feature="fts_country_name",
              mechanism="Изменение валютного курса может влиять на расходы по международным расчётам.",
              assumption="Часть расчетов ведется в затронутой валюте.",
              need="Валюты договоров и график международных расчетов.",
              action="Уточнить валютную структуру обязательств и сроки расчетов.",
              human_focus=("Страна ФТС не определяет валюту, направление или объем торговли.",)),
        _case(5, "regulation", "Введены новые требования к обязательной отчетности.",
              {"mainokved_code": "24.10"}, channel="regulation", feature="mainokved_code",
              mechanism="Изменение требований может потребовать доработки документов для соответствия регулированию.",
              assumption="Новые требования распространяются на деятельность компании.",
              need="Область применения требований и действующие процедуры отчетности.",
              action="Проверить применимость требований и перечень необходимых документов.",
              human_focus=("Проверить юридическую применимость и дату вступления требований.",)),
        _case(6, "irrelevant", "Открылась выставка декоративного искусства.",
              {"mainokved_code": "24.10"}, channel=None,
              need="Связь культурного события с деятельностью предприятия.",
              human_focus=("Сохранить недостаточность данных без выдуманного финансового эффекта.",)),
    ]


async def run_offline_evaluation() -> dict:
    """Run the real grounded verifier/projector with fixed fake responses only."""
    from .config import AgentConfig
    from .llm import FakeLLMClient
    from .runtime import RuntimeAgentEngine
    from .tools import MockAnalyticsSource
    from .tracer import RecordingTracer

    evaluations = []
    for case in build_evaluation_cases():
        response = case.draft.model_dump_json()
        engine = RuntimeAgentEngine(
            client_factory=lambda response=response: FakeLLMClient(response),
            source_factory=MockAnalyticsSource,
            config=AgentConfig(max_steps=1, max_llm_calls=1, max_tool_calls=0),
            allow_test_dependencies=True,
        )
        result = await engine.generate_analysis(case.company, case.event, [], RecordingTracer())
        evaluations.append(evaluate_analysis(result, expected=case.expectation))
    technical_passes = sum(all(value is True for value in item.technical_checks.values()) for item in evaluations)
    fixture_passes = sum(all(item.fixture_expectations.values()) for item in evaluations)
    return {
        "version": EVALUATION_VERSION, "mode": "offline_scripted_fixtures",
        "provider_calls": 0, "llm_intelligence_measured": False,
        "cases": len(evaluations), "technical_passed_cases": technical_passes,
        "fixture_expectations_matched_cases": fixture_passes,
        "scripted_client_calls": (
            sum(item.counts["recorded_calls"] for item in evaluations)
            if all(item.counts["recorded_calls"] is not None for item in evaluations) else None
        ),
        "human_review_required_cases": len(evaluations),
        "verification_limits": list(VERIFICATION_LIMITS),
    }


def main() -> int:
    try:
        summary = asyncio.run(run_offline_evaluation())
    except Exception:
        # Exception text may contain source content; the CLI never prints it.
        print(json.dumps({"version": EVALUATION_VERSION, "mode": "offline_scripted_fixtures",
                          "error_code": "OFFLINE_EVALUATION_FAILED"}, sort_keys=True))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0 if (summary["technical_passed_cases"] == summary["cases"]
                 and summary["fixture_expectations_matched_cases"] == summary["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
