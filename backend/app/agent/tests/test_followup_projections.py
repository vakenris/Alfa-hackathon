"""Saved evidence supports precise follow-ups without another model/tool call."""

import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend.app.agent import AgentInsightEngine, FakeLLMClient, RecordingTracer
from backend.app.agent.action_models import ManagementActionRequest, ManagementActionResult
from backend.app.agent.actions import ManagementActionEngine
from backend.app.agent.models import AgentAnalysisResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


def payment_id(uid, direction="incoming"):
    return "payments:" + direction + ":" + hashlib.sha256(uid.encode()).hexdigest()[:16]


class FollowupProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.company = CompanyResponse(id=1, synthetic_u="SYNTHETIC_LOCAL_A", name="Синтетический завод",
                                       mainokved_code="24.10", cl_product_loa_mark=True)
        self.event = NewsArticleResponse(id=10, title="Ключевая ставка сохранена", source="Синтетический источник",
            published_at=datetime(2026, 10, 23, tzinfo=timezone.utc), category="Экономика",
            content="Ключевая ставка сохранена на уровне 14%.")
        self.rows = [CounterpartyItem(synthetic_u=uid, name=name, direction=direction,
                                     total_amount=amount, transaction_count=1)
                     for uid, name, direction, amount in (
                         ("PAYER_A", "Плательщик А", "PAYER", 900),
                         ("PAYER_B", "Плательщик Б", "PAYER", 100),
                         ("RECIPIENT", "Получатель", "BENEFICIARY", 800),
                         (self.company.synthetic_u, "Собственный перевод", "PAYER", 10000))]

    async def bundle(self, *, payments=(), scenarios=True, global_gap=True, rows=None):
        ids = ["news:event", "company:mainokved_code", *payments]
        response = {"applicability": "conditional", "impacts": [{
            "id": "financing", "channel": "financing", "direction": "mixed",
            "condition": "Если компания рассматривает новое финансирование",
            "mechanism": "Стоимость нового финансирования может влиять на инвестиционные решения.",
            "evidence_ids": ids, "data_needs": ["Планы закупки оборудования и параметры финансирования."],
        }], "actions": [{"title": "Проверить варианты финансирования.",
            "rationale": "Сравнение может уточнить условия.", "impact_ids": ["financing"],
            "required_data": ["Варианты поставки оборудования."]}],
            "missing_data": ["Условия инвестиционной программы."] if global_gap else []}
        if scenarios:
            response["scenarios"] = [
                {"label": "Удорожание нового финансирования", "condition": "Если стоимость новых заимствований повысится",
                 "outcome": "Этапность инвестиционной программы может потребовать пересмотра.", "evidence_ids": ids},
                {"label": "Удешевление нового финансирования", "condition": "Если стоимость новых заимствований снизится",
                 "outcome": "Доступность инвестиционного финансирования может улучшиться.", "evidence_ids": ids},
            ]
        self.client = FakeLLMClient(json.dumps(response, ensure_ascii=False))
        self.core = AgentInsightEngine(self.client)
        original = await self.core.generate_analysis(self.company, self.event, self.rows if rows is None else rows,
                                                      RecordingTracer())
        self.analysis = AgentAnalysisResult.model_validate_json(original.model_dump_json())
        self.helper = ManagementActionEngine(self.core)
        return self.analysis

    async def ask(self, question, *, analysis=None, company=None):
        before = len(self.client.prompts)
        answer = await self.helper.answer_question(question, analysis=analysis or self.analysis,
            company=company or self.company, tracer=RecordingTracer())
        self.assertEqual(len(self.client.prompts), before)
        self.assertEqual(answer.report["llm_calls"], 0)
        return ManagementActionResult.model_validate_json(answer.model_dump_json())

    async def test_scenario_question_returns_conditions_outcomes_and_original_references(self):
        await self.bundle()
        answer = await self.ask("Какие сценарии изменения ставки рассматривает агент?")
        self.assertEqual(answer.status, "complete")
        self.assertEqual(answer.scenarios, self.analysis.analysis.scenarios)
        for scenario in answer.scenarios:
            self.assertIn(scenario.condition, answer.summary)
            self.assertIn(scenario.outcome, answer.summary)
            self.assertTrue(set(scenario.evidence_ids) <= {ref.fact_id for ref in answer.source_refs})
        self.assertIn("не прогноз", answer.summary)

    async def test_absent_scenarios_do_not_return_unrelated_complete_answer(self):
        await self.bundle(scenarios=False)
        answer = await self.ask("Какие сценарии изменения ставки сохранены?")
        self.assertEqual(answer.status, "needs_data")
        self.assertFalse(answer.scenarios)

    async def test_requirements_question_preserves_impact_action_and_global_data_requests(self):
        await self.bundle()
        answer = await self.ask("Какие данные нужны для проверки влияния ставки?")
        self.assertEqual(answer.status, "complete")
        expected = [*self.analysis.analysis.impacts[0].data_needs,
                    *self.analysis.analysis.actions[0].required_data, *self.analysis.analysis.missing_data]
        self.assertEqual(answer.missing_data, expected)
        self.assertTrue(all(text in answer.summary for text in expected))
        self.assertTrue(answer.source_refs)

    async def test_unknown_requirement_topic_remains_needs_data(self):
        await self.bundle(global_gap=False)
        answer = await self.ask("Какие данные нужны для проверки налоговых последствий?")
        self.assertEqual(answer.status, "needs_data")

    async def test_largest_payer_uses_saved_sample_share_and_excludes_self(self):
        await self.bundle(payments=("payments:summary", payment_id("PAYER_A")))
        answer = await self.ask("Кто крупнейший плательщик в переданной выборке и какова его доля?")
        self.assertEqual(answer.status, "complete")
        self.assertIn("Входящие внешние платежи", answer.summary)
        self.assertIn("Плательщик А", answer.summary)
        self.assertIn("90.0%", answer.summary)
        self.assertIn("период, валюта и полнота неизвестны", answer.summary)
        self.assertIn("не установлен как покупатель", answer.summary)
        self.assertNotIn("10000", answer.summary)
        self.assertTrue(all(ref.fact_id in {item.id for item in self.analysis.evidence} for ref in answer.source_refs))

    async def test_largest_recipient_is_outgoing_and_cannot_be_called_supplier(self):
        await self.bundle(payments=("payments:summary", payment_id("RECIPIENT", "outgoing")))
        answer = await self.ask("Кто крупнейший получатель исходящих платежей?")
        self.assertEqual(answer.status, "complete")
        self.assertIn("Исходящие внешние платежи", answer.summary)
        self.assertIn("Получатель", answer.summary)
        self.assertIn("100.0%", answer.summary)
        unknown_role = await self.ask("Кто крупнейший поставщик компании?")
        self.assertEqual(unknown_role.status, "needs_data")

    async def test_direction_must_be_explicit_for_largest_counterparty(self):
        await self.bundle(payments=("payments:summary", payment_id("PAYER_A"), payment_id("RECIPIENT", "outgoing")))
        answer = await self.ask("Кто крупнейший контрагент и какова его доля?")
        self.assertEqual(answer.status, "needs_data")
        self.assertIn("входящие или исходящие", " ".join(answer.missing_data))

    async def test_saved_minor_payer_cannot_be_presented_as_largest_or_restore_omitted_major_payer(self):
        await self.bundle(payments=("payments:summary", payment_id("PAYER_B")))
        answer = await self.ask("Кто крупнейший плательщик?")
        self.assertEqual(answer.status, "needs_data")
        self.assertNotIn("Плательщик А", answer.summary)
        self.assertTrue(all(ref.fact_id != payment_id("PAYER_A") for ref in answer.source_refs))

    async def test_missing_selected_payment_evidence_is_not_rebuilt_from_source_metadata(self):
        await self.bundle()
        self.assertTrue(any(source.source_id == "payments:1" for source in self.analysis.sources))
        answer = await self.ask("Что известно о платежах?")
        self.assertEqual(answer.status, "needs_data")
        self.assertFalse(answer.source_refs)

    async def test_payment_summary_reports_self_separately_without_invented_currency(self):
        await self.bundle(payments=("payments:summary",))
        answer = await self.ask("Что известно о входящих и исходящих платежах?")
        self.assertEqual(answer.status, "complete")
        self.assertIn("Входящие внешние платежи: сумма 1000", answer.summary)
        self.assertIn("Исходящие внешние платежи: сумма 800", answer.summary)
        self.assertIn("Собственные переводы отдельно: сумма 10000", answer.summary)
        self.assertIn("период, валюта и полнота неизвестны", answer.summary)
        self.assertNotIn("RUB", answer.summary)
        self.assertEqual(len(answer.source_refs), 1)
        future_effect = await self.ask("На сколько увеличатся платежи?")
        self.assertEqual(future_effect.status, "needs_data")

    async def test_review_plan_returns_selected_titles_provenance_conditional_links_and_sources(self):
        await self.bundle()
        provided = [
            {"id": "equipment", "title": "Закупка новой линии профилирования", "related_channels": ["financing"],
             "provenance": {"data_mode": "actual", "reference": "Утверждённый синтетический план"}},
            {"id": "contacts", "title": "Обновление контактов", "related_channels": ["other"],
             "provenance": {"data_mode": "actual", "reference": "Тот же синтетический план"}},
        ]
        request = ManagementActionRequest(kind="review_plan", company_id=1, current_plan=provided)
        answer = await self.helper.analyze_action(request, analysis=self.analysis, company=self.company, tracer=RecordingTracer())
        answer = ManagementActionResult.model_validate_json(answer.model_dump_json())
        self.assertEqual(answer.status, "complete")
        self.assertEqual([item.id for item in answer.provided_plan], ["equipment"])
        self.assertEqual(answer.provided_plan[0].provenance.reference, provided[0]["provenance"]["reference"])
        self.assertIn(provided[0]["title"], answer.summary)
        self.assertIn(provided[0]["title"], answer.proposed_actions[0].title)
        self.assertNotIn(provided[1]["title"], answer.summary)
        self.assertEqual(answer.proposed_actions[0].plan_item_ids, ["equipment"])
        self.assertTrue(answer.proposed_actions[0].fact_ids)
        self.assertEqual(set(answer.proposed_actions[0].fact_ids), {ref.fact_id for ref in answer.source_refs})
        self.assertIn(self.analysis.analysis.impacts[0].condition, answer.proposed_actions[0].rationale)
        self.assertTrue(answer.impacts)
        self.assertTrue(answer.proposed_actions[0].requires_human_review)
        self.assertEqual(len(self.client.prompts), 1)

    async def test_hypothetical_plan_remains_explicit_and_unrelated_plan_needs_data(self):
        await self.bundle()
        item = {"id": "hypothetical", "title": "Гипотетическая закупка оборудования", "related_channels": ["financing"],
                "provenance": {"data_mode": "hypothetical", "reference": "Явно синтетическая идея"}}
        request = ManagementActionRequest(kind="review_plan", company_id=1, current_plan=[item])
        answer = await self.helper.analyze_action(request, analysis=self.analysis, company=self.company, tracer=RecordingTracer())
        self.assertIn("гипотетические", answer.summary)
        self.assertEqual(answer.provided_plan[0].provenance.data_mode, "hypothetical")
        item["related_channels"] = ["other"]
        request = ManagementActionRequest(kind="review_plan", company_id=1, current_plan=[item])
        answer = await self.helper.analyze_action(request, analysis=self.analysis, company=self.company, tracer=RecordingTracer())
        self.assertEqual(answer.status, "needs_data")
        self.assertFalse(answer.provided_plan)

    async def test_tampered_scenario_and_foreign_company_cannot_be_projected(self):
        await self.bundle()
        changed = self.analysis.model_copy(deep=True)
        changed.analysis.scenarios[0].outcome = "Расходы могут вырасти на 99%."
        self.assertEqual((await self.ask("Какие сценарии?", analysis=changed)).status, "insufficient_evidence")
        foreign = self.company.model_copy(update={"synthetic_u": "OTHER_COMPANY"})
        self.assertEqual((await self.ask("Какие данные нужны?", company=foreign)).status, "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
