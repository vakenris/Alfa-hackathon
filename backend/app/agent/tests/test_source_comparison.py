"""Bounded context requests and unresolved source comparisons; no provider or DB."""

import copy
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from backend.app.agent.config import AgentConfig
from backend.app.agent.context import prepare_context
from backend.app.agent.models import ContextRequest, DraftInsight, SourceComparison
from backend.app.agent.prompts import build_prompt
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.agent.tools import MockAnalyticsSource
from backend.app.agent.models import AnalyticsDocument
from backend.app.agent.verification import project_insight, selected_evidence, verify_draft


def comparison(*ids, relationship="scope_requires_check", explanation=None):
    return {
        "evidence_ids": list(ids), "relationship": relationship, "status": "unresolved",
        "explanation": explanation or "Условия сценариев и целевые периоды требуют сверки; общую оценку пока делать нельзя.",
    }


class SourceComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.company, self.event = fixtures()
        docs = []
        for index, days in enumerate((-1, -2, -3)):
            article = self.event.model_copy(update={
                "id": 100+index, "title": "Учебный банковский прогноз",
                "content": f"Базовый прогноз ключевой ставки на конец 2027 года: {8+index}% годовых.",
                "published_at": self.event.published_at+timedelta(days=days), "is_bank_analytics": True,
            })
            docs.append(AnalyticsDocument(article=article, source_document_id=f"fixture:{index}",
                                          provenance_status="verified"))
        self.context = await prepare_context(self.company, self.event, [], MockAnalyticsSource(docs), AgentConfig())

    def verify(self, payload):
        return verify_draft(DraftInsight.model_validate(payload), self.context)

    def assert_issue(self, payload, code, path):
        result = self.verify(payload)
        self.assertFalse(result.passed)
        self.assertIn((code, path), [(issue.code, issue.path) for issue in result.issues])

    async def test_context_request_is_bounded_and_cannot_be_published_as_a_final_answer(self):
        request = {"query": "ключевая ставка условия финансирования", "reason": "Нужен обзор по теме вместо календаря публикаций."}
        draft = valid_draft()
        draft["context_request"] = request
        self.assert_issue(draft, "PENDING_CONTEXT_REQUEST", "context_request")
        self.assertEqual(ContextRequest.model_validate(request).kind, "bank_search")
        for changed in ({"query": ""}, {"query": " "}, {"query": "x"*241}, {"query": 7}, {"kind": "execute"}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                ContextRequest.model_validate({**request, **changed})
        prompt = build_prompt({"agent_context": {"can_request_bank_search": False}})
        self.assertIn("agent_context.can_request_bank_search=true", prompt)
        self.assertIn('"can_request_bank_search":false', prompt)

    async def test_multiple_used_bank_sources_require_complete_comparison_coverage(self):
        draft = valid_draft()
        draft["impacts"][0]["evidence_ids"] += ["bank:100:excerpt", "bank:101:excerpt"]
        self.assert_issue(draft, "MISSING_SOURCE_COMPARISON", "source_comparisons")
        draft["source_comparisons"] = [comparison("bank:100:excerpt", "bank:101:excerpt")]
        self.assertTrue(self.verify(draft).passed)
        draft["scenarios"] = [{"label": "Проверка другого сценария", "condition": "Если требуется сопоставить условия финансирования",
                               "outcome": "Стоимость капитала может влиять на выбор этапности проектов.",
                               "evidence_ids": ["news:event", "company:mainokved_code", "bank:102:excerpt"]}]
        self.assert_issue(draft, "MISSING_SOURCE_COMPARISON", "source_comparisons")
        draft["source_comparisons"] = [comparison("bank:100:excerpt", "bank:101:excerpt", "bank:102:excerpt")]
        self.assertTrue(self.verify(draft).passed)

    async def test_comparison_only_sources_are_preserved_and_shown_with_unresolved_scope(self):
        payload = {"applicability": "insufficient", "missing_data": ["Связь сценариев с инвестиционными планами не установлена."],
                   "source_comparisons": [comparison("bank:100:excerpt", "bank:101:excerpt", relationship="potential_conflict")]}
        draft = DraftInsight.model_validate(payload)
        report = verify_draft(draft, self.context)
        self.assertTrue(report.passed)
        self.assertIn("UNRESOLVED_SOURCE_COMPARISON", report.warnings)
        self.assertEqual({item.id for item in selected_evidence(draft, self.context)},
                         {"news:event", "bank:100:excerpt", "bank:101:excerpt"})
        insight = project_insight(draft, self.context, report)
        for key in ("bank:100:excerpt", "bank:101:excerpt"):
            entry = self.context.evidence[key]
            self.assertIn(entry.quote, insight.bank_view)
            self.assertIn(key, insight.ai_view)
            self.assertIn(entry.published_at.isoformat(), insight.ai_view)
        self.assertIn("Неразрешённое сопоставление", insight.ai_view)
        self.assertIn("Семантика сопоставления экспертно не проверена", insight.ai_view)

    async def test_unknown_nonbank_and_duplicate_references_are_rejected(self):
        cases = [
            ([comparison("bank:100:excerpt", "bank:404:excerpt")], "UNKNOWN_EVIDENCE", "source_comparisons[0].evidence_ids"),
            ([comparison("bank:100:excerpt", "company:mainokved_code")], "NON_BANK_COMPARISON_EVIDENCE", "source_comparisons[0].evidence_ids"),
            ([comparison("bank:100:excerpt", "bank:100:excerpt")], "DUPLICATE_COMPARISON_EVIDENCE", "source_comparisons[0].evidence_ids"),
            ([comparison("bank:100:excerpt", "bank:101:excerpt"), comparison("bank:101:excerpt", "bank:100:excerpt")],
             "DUPLICATE_SOURCE_COMPARISON", "source_comparisons[1].evidence_ids"),
        ]
        for comparisons, code, path in cases:
            with self.subTest(code=code):
                draft = valid_draft()
                draft["source_comparisons"] = comparisons
                self.assert_issue(draft, code, path)

    async def test_different_vintages_uses_actual_publication_instants_without_inferring_conflict(self):
        draft = valid_draft()
        draft["source_comparisons"] = [comparison("bank:100:excerpt", "bank:101:excerpt", relationship="different_vintages")]
        self.assertTrue(self.verify(draft).passed)
        original = copy.deepcopy(self.context)
        first = self.context.evidence["bank:100:excerpt"].published_at
        self.context.evidence["bank:101:excerpt"].published_at = first.astimezone(timezone(timedelta(hours=3)))
        self.assert_issue(draft, "INVALID_COMPARISON_VINTAGES", "source_comparisons[0].relationship")
        self.context.evidence["bank:101:excerpt"].published_at = first.replace(tzinfo=None)
        self.assert_issue(draft, "INVALID_COMPARISON_VINTAGES", "source_comparisons[0].relationship")
        self.context.evidence["bank:101:excerpt"].published_at = None
        self.assert_issue(draft, "INVALID_COMPARISON_VINTAGES", "source_comparisons[0].relationship")
        self.context = original
        draft["source_comparisons"][0]["relationship"] = "potential_conflict"
        self.assertTrue(self.verify(draft).passed)
        self.assertEqual(DraftInsight.model_validate(draft).source_comparisons[0].status, "unresolved")

    async def test_comparison_explanations_cannot_add_numbers_certainty_or_company_facts(self):
        for text, code in (("Разница прогнозов составляет 9 процентов.", "UNSUPPORTED_INFERENCE_NUMBER"),
                           ("Прогнозы гарантированно реализуются.", "FALSE_CERTAINTY"),
                           ("Компания получила кредит.", "UNSUPPORTED_FACTUAL_PREMISE")):
            with self.subTest(code=code):
                draft = valid_draft()
                draft["source_comparisons"] = [comparison("bank:100:excerpt", "bank:101:excerpt", explanation=text)]
                self.assert_issue(draft, code, "source_comparisons[0].explanation")
        base = comparison("bank:100:excerpt", "bank:101:excerpt")
        for changed in ({"status": "resolved"}, {"relationship": "consistent"}, {"evidence_ids": ["bank:100:excerpt"]},
                        {"evidence_ids": [f"bank:{index}:excerpt" for index in range(6)]}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                SourceComparison.model_validate({**base, **changed})


if __name__ == "__main__":
    unittest.main()
