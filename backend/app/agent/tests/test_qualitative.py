"""Offline regressions for the sector-only live-test failure.

Scripted responses exercise the control and evidence boundaries, not real model
quality. No test imports the live harness, reads credentials or contacts an API.
"""

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend.app.agent import AgentConfig, AgentInsightEngine, RecordingTracer
from backend.app.agent.context import GroundedRunContext
from backend.app.agent.models import (
    AgentEvidenceError, BANK_VIEW_UNAVAILABLE, DraftInsight,
)
from backend.app.agent.tests.test_grounded import DynamicClient
from backend.app.agent.verification import verify_draft
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse


RATE_QUOTE = (
    "23 октября 2026 года Банк России решил сохранить ключевую ставку "
    "на уровне 14% годовых."
)


def metallurgy_company(**updates):
    values = {
        "id": 41, "synthetic_u": "OFFLINE_METAL", "name": "Синтетический металлургический завод",
        "mainokved_code": "24.10", "mainokved_name": "Производство чугуна, стали и ферросплавов",
    }
    values.update(updates)
    return CompanyResponse(**values)


def rate_event():
    return NewsArticleResponse(
        id=51, title="Банк России сохранил ключевую ставку", source="Синтетическая тестовая фикстура",
        published_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        category="Экономика", content=RATE_QUOTE,
    )


def qualitative_draft(*, feature="mainokved_code"):
    return {
        "action": "draft", "applicability": "conditional",
        "facts": [{
            "id": "fact:rate", "source_id": "news:51", "quote": RATE_QUOTE,
            "metric": "ключевую ставку", "entity": "Банк России",
            "period": "23 октября 2026 года", "unit": "%", "value": 14,
        }],
        "impacts": [
            {
                "id": "impact:financing", "channel": "financing", "direction": "mixed",
                "mechanism": "Если завод планирует новое заёмное финансирование, сохранение ключевой ставки может оставаться ориентиром при сравнении доступных условий кредитования.",
                "fact_ids": ["fact:rate"], "company_features": [feature],
                "assumptions": ["Компания рассматривает привлечение новых заёмных средств."],
                "data_needs": ["Планы нового финансирования и актуальные предложения кредиторов."],
                "horizon": "medium",
            },
            {
                "id": "impact:investment", "channel": "investment", "direction": "mixed",
                "mechanism": "Если рассматриваются проекты модернизации производства, стоимость нового финансирования может влиять на выбор инвестиционного бюджета и сроков реализации.",
                "fact_ids": ["fact:rate"], "company_features": [feature],
                "assumptions": ["Рассматриваются инвестиционные проекты с привлечением нового финансирования."],
                "data_needs": ["Состав проектов, источники финансирования и требования к окупаемости."],
                "horizon": "medium",
            },
        ],
        "actions": [{
            "title": "Сопоставить предложения по будущему финансированию и проверить инвестиционные сценарии.",
            "impact_ids": ["impact:financing", "impact:investment"],
            "required_data": ["Планы проектов и предложения по новым займам."], "owner_role": "CFO",
        }],
        "missing_data": ["Кредитные договоры и утверждённые планы инвестиций не предоставлены."],
    }


def insufficient_draft(*, event=None):
    draft = qualitative_draft()
    draft.update(
        applicability="insufficient", impacts=[], actions=[],
        missing_data=["Уточнить связь события с деятельностью компании."],
    )
    if event is not None:
        draft["facts"] = [{"id": "fact:event", "source_id": f"news:{event.id}", "quote": event.content}]
    return draft


class QualitativeAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.company = metallurgy_company()
        self.event = rate_event()

    async def run_steps(self, steps, *, company=None, event=None, config=None):
        client = DynamicClient(steps)
        engine = AgentInsightEngine(client, config=config)
        result = await engine.generate_analysis(
            company or self.company, event or self.event, [], RecordingTracer(),
        )
        return result, client

    async def test_sector_only_supports_qualitative_analysis_without_bank_or_payments(self):
        for feature, company in (
            ("mainokved_code", metallurgy_company(mainokved_name=None)),
            ("mainokved_name", metallurgy_company(mainokved_code=None)),
        ):
            with self.subTest(feature=feature):
                self.assertFalse(company.cl_product_loa_mark)
                result, client = await self.run_steps([qualitative_draft(feature=feature)], company=company)
                self.assertTrue(result.verification.passed)
                self.assertEqual(result.analysis.applicability, "conditional")
                self.assertEqual({item.channel for item in result.analysis.impacts}, {"financing", "investment"})
                self.assertTrue(result.analysis.actions)
                self.assertTrue(all(item.assumptions and item.data_needs for item in result.analysis.impacts))
                self.assertTrue(all(item.company_features == [feature] for item in result.analysis.impacts))
                self.assertNotIn("cl_product_loa_mark", result.business_context.known_features)
                self.assertEqual(result.business_context.payment_sample_size, 0)
                self.assertEqual(len(result.sources), 1)
                self.assertEqual(result.insight.bank_view, BANK_VIEW_UNAVAILABLE)
                self.assertIsNone(result.insight.relevant_bank_product)
                self.assertIn("14%", result.insight.what_happened)
                self.assertNotIn("14", result.insight.consequences)
                self.assertIn("Допущения", result.insight.consequences)
                self.assertIn("CFO", result.insight.recommended_action)
                self.assertEqual(len(client.prompts), 1)
                self.assertEqual(result.report["validation_summary"]["tool_calls"], 0)

    async def test_missing_financial_inputs_trigger_qualitative_repair_not_silent_insufficient(self):
        result, client = await self.run_steps([insufficient_draft(), qualitative_draft()])
        self.assertEqual(result.analysis.applicability, "conditional")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("QUALITATIVE_ANALYSIS_MISSING", client.prompts[-1])
        self.assertEqual(result.report["validation_summary"]["repairs"], 1)
        self.assertEqual(result.report["validation_summary"]["tool_calls"], 0)
        self.assertEqual(result.report["validation_attempts"], [
            {"call_number": 1, "phase": "draft", "issue_codes": ["QUALITATIVE_ANALYSIS_MISSING"], "facts": 1, "impacts": 0},
            {"call_number": 2, "phase": "repair", "issue_codes": [], "facts": 1, "impacts": 2},
        ])
        self.assertEqual(
            [call["prompt_chars"] for call in result.report["calls"]],
            [len(prompt) for prompt in client.prompts],
        )
        # The report explains the repair using counts/codes, without storing
        # company text, quotes, prompts or private model reasoning.
        for attempt in result.report["validation_attempts"]:
            self.assertEqual(set(attempt), {"call_number", "phase", "issue_codes", "facts", "impacts"})

    async def test_neutral_title_quote_cannot_hide_a_visible_policy_decision(self):
        event = self.event.model_copy(update={"title": "Решение регулятора"})
        draft = insufficient_draft()
        draft["facts"] = [{
            "id": "fact:title", "source_id": "news:51", "field": "title", "quote": event.title,
        }]
        result, client = await self.run_steps([draft, qualitative_draft()], event=event)
        self.assertEqual(result.analysis.applicability, "conditional")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("QUALITATIVE_ANALYSIS_MISSING", client.prompts[-1])
        self.assertIn(RATE_QUOTE, result.insight.what_happened)

    async def test_repeated_premature_insufficient_exhausts_only_one_repair(self):
        client = DynamicClient([insufficient_draft(), insufficient_draft()])
        with self.assertRaises(AgentEvidenceError) as caught:
            await AgentInsightEngine(client).generate_analysis(self.company, self.event, [], RecordingTracer())
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(len(caught.exception.run_report["calls"]), 2)
        self.assertIn("QUALITATIVE_ANALYSIS_MISSING", client.prompts[-1])

    async def test_disabled_recovery_does_not_certify_premature_insufficient(self):
        client = DynamicClient([insufficient_draft()])
        with self.assertRaises(AgentEvidenceError):
            await AgentInsightEngine(client, config=AgentConfig(max_recovery_attempts=0)).generate_analysis(
                self.company, self.event, [], RecordingTracer(),
            )
        self.assertEqual(len(client.prompts), 1)

    async def test_unrelated_sports_event_remains_insufficient_without_forced_risks(self):
        event = self.event.model_copy(update={
            "id": 52, "title": "Результаты шахматного турнира", "category": "Спорт",
            "content": "В синтетическом шахматном турнире определён победитель.",
        })
        result, client = await self.run_steps([insufficient_draft(event=event)], event=event)
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.analysis.applicability, "insufficient")
        self.assertEqual(result.analysis.impacts, [])
        self.assertEqual(result.analysis.actions, [])
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(result.report["validation_summary"]["repairs"], 0)

    async def test_negated_or_denied_decision_does_not_force_qualitative_conclusions(self):
        for content in (
            "Банк России не решил сохранить ключевую ставку.",
            "Банк России опроверг сообщения, что повысил ключевую ставку.",
            "Банк России может решить сохранить ключевую ставку.",
        ):
            with self.subTest(content=content):
                event = self.event.model_copy(update={
                    "id": 53, "title": "Уточнение сообщения о регуляторе", "content": content,
                })
                result, client = await self.run_steps([insufficient_draft(event=event)], event=event)
                self.assertTrue(result.verification.passed)
                self.assertEqual(result.analysis.applicability, "insufficient")
                self.assertFalse(result.analysis.impacts)
                self.assertEqual(len(client.prompts), 1)

    async def test_incidental_rate_words_in_sports_news_do_not_force_a_business_risk(self):
        event = self.event.model_copy(update={
            "id": 54, "title": "Шахматный турнир", "category": "Спорт",
            "content": "На шахматном турнире участник сохранил ставку в споре о результате партии; зрители обсуждали ключевую ставку Банка России.",
        })
        result, client = await self.run_steps([insufficient_draft(event=event)], event=event)
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.analysis.applicability, "insufficient")
        self.assertEqual(result.analysis.impacts, [])
        self.assertEqual(result.analysis.actions, [])
        self.assertEqual(len(client.prompts), 1)

    async def test_affirmative_headline_cannot_override_a_denial_in_the_body(self):
        event = self.event.model_copy(update={
            "id": 55, "title": "Банк России сохранил ключевую ставку",
            "content": "Банк России опроверг сообщение о решении сохранить ключевую ставку.",
        })
        result, client = await self.run_steps([insufficient_draft(event=event)], event=event)
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.analysis.applicability, "insufficient")
        self.assertFalse(result.analysis.impacts)
        self.assertEqual(len(client.prompts), 1)

    async def test_nested_policy_quote_in_sports_news_does_not_force_a_business_risk(self):
        event = self.event.model_copy(update={
            "id": 56, "title": "Открытие шахматного турнира", "category": "Спорт",
            "content": "На открытии шахматного турнира участник процитировал фразу «Банк России сохранил ключевую ставку».",
        })
        result, client = await self.run_steps([insufficient_draft(event=event)], event=event)
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.analysis.applicability, "insufficient")
        self.assertFalse(result.analysis.impacts)
        self.assertEqual(result.analysis.actions, [])
        self.assertEqual(len(client.prompts), 1)

    async def test_unknown_industry_and_finance_still_allow_insufficient(self):
        company = metallurgy_company(mainokved_code=None, mainokved_name=None, name="Неизвестная компания")
        result, client = await self.run_steps([insufficient_draft()], company=company)
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.analysis.applicability, "insufficient")
        self.assertFalse(result.analysis.impacts)
        self.assertEqual(len(client.prompts), 1)

    async def test_unsupported_specific_financial_effect_is_refused_without_extra_budget(self):
        draft = qualitative_draft()
        draft["impacts"][0]["mechanism"] = "Стоимость финансирования может измениться: процентные расходы завода снизятся на 14 процентов."
        client = DynamicClient([draft])
        with self.assertRaises(AgentEvidenceError):
            await AgentInsightEngine(client, config=AgentConfig(max_recovery_attempts=0)).generate_analysis(
                self.company, self.event, [], RecordingTracer(),
            )
        self.assertEqual(len(client.prompts), 1)
        context = GroundedRunContext(self.company, self.event, [], AgentConfig())
        report = verify_draft(DraftInsight.model_validate(draft), context)
        self.assertFalse(report.passed)
        self.assertIn("UNSUPPORTED_INFERENCE_NUMBER", {issue.code for issue in report.issues})

    async def test_unchanged_rate_cannot_increase_existing_loan_payments(self):
        draft = qualitative_draft()
        draft["impacts"][0]["mechanism"] = "Сохранение ключевой ставки может изменить стоимость финансирования: платежи по кредитам вырастут."
        context = GroundedRunContext(self.company, self.event, [], AgentConfig())
        report = verify_draft(DraftInsight.model_validate(draft), context)
        self.assertFalse(report.passed)
        self.assertIn("UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE", {issue.code for issue in report.issues})

    async def test_product_flag_does_not_establish_contract_terms(self):
        company = metallurgy_company(cl_product_loa_mark=True)
        draft = copy.deepcopy(qualitative_draft())
        draft["impacts"][0]["company_features"].append("cl_product_loa_mark")
        draft["impacts"][0]["mechanism"] = "У завода действуют кредиты с плавающей ставкой, поэтому стоимость финансирования может измениться."
        context = GroundedRunContext(company, self.event, [], AgentConfig())
        report = verify_draft(DraftInsight.model_validate(draft), context)
        self.assertFalse(report.passed)
        self.assertIn("UNSUPPORTED_COMPANY_CONDITION", {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
