"""Regression tests for concrete bypasses found in independent adversarial review."""

import asyncio
import copy
import unittest

import httpx

from backend.app.agent import AgentConfig, AgentInsightEngine, AnalyticsDocument, RecordingTracer
from backend.app.agent.api_client import ChatCompletionsClient, LLMResponseError
from backend.app.agent.config import ChatCompletionsConfig
from backend.app.agent.context import GroundedRunContext
from backend.app.agent.models import DraftInsight, ToolObservation
from backend.app.agent.tests.test_grounded import EmptySearchSource, fixtures, valid_draft
from backend.app.agent.verification import verify_draft, project_insight


class AdversarialVerificationTests(unittest.TestCase):
    def setUp(self):
        self.company, self.event = fixtures()
        self.context = GroundedRunContext(self.company, self.event, [], AgentConfig())

    def add_document(self, text, *, article_id=20, verified=True):
        doc = AnalyticsDocument(
            article=self.event.model_copy(update={"id": article_id, "title": f"Тестовый источник {article_id}",
                                                 "content": text, "is_bank_analytics": True}),
            provenance_status="verified" if verified else "unverified", source_document_id="fixture" if verified else None,
        )
        self.context.add_observation(ToolObservation(tool="get_bank_analytics", arguments={}, status="ok",
                                                     data={"documents": [doc.model_dump(mode="json")]}))
        return list(self.context.sources)[-1]

    def test_bank_quote_cannot_drop_negation_from_a_real_sentence(self):
        source = self.add_document("В синтетическом примере банк не рекомендует повышать кредитную нагрузку.")
        draft = valid_draft()
        draft["facts"].append({"id": "bank", "source_id": source, "kind": "bank_statement",
                                "quote": "рекомендует повышать кредитную нагрузку."})
        report = verify_draft(DraftInsight.model_validate(draft), self.context)
        self.assertIn("QUOTE_CONTEXT_INCOMPLETE", [item.code for item in report.issues])
        draft["facts"][-1]["quote"] = "В синтетическом примере банк не рекомендует повышать кредитную нагрузку."
        model = DraftInsight.model_validate(draft)
        report = verify_draft(model, self.context)
        self.assertTrue(report.passed)
        self.assertIn("не рекомендует", project_insight(model, self.context, report).bank_view)

    def test_conditional_suffix_does_not_hide_an_asserted_floating_rate(self):
        draft = valid_draft()
        draft["impacts"][0]["mechanism"] = "У завода действуют кредиты с плавающей ставкой, поэтому стоимость финансирования может вырасти."
        report = verify_draft(DraftInsight.model_validate(draft), self.context)
        self.assertIn("UNSUPPORTED_COMPANY_CONDITION", [item.code for item in report.issues])

    def test_bank_attribution_variants_are_rejected_outside_source_quotes(self):
        for phrase in ("По оценке Альфа-Банка стоимость финансирования может вырасти.",
                       "Согласно мнению банка стоимость финансирования может вырасти.",
                       "Альфа-Банк: стоимость финансирования может вырасти."):
            with self.subTest(phrase=phrase):
                draft = valid_draft()
                draft["impacts"][0]["mechanism"] = phrase
                report = verify_draft(DraftInsight.model_validate(draft), self.context)
                self.assertIn("UNSUPPORTED_BANK_ATTRIBUTION", [item.code for item in report.issues])

    def test_removing_all_numeric_metadata_cannot_silently_bypass_conflicts(self):
        draft = valid_draft()
        for index, value in enumerate((14, 13)):
            text = f"Тестовый сектор: ставка за период 2026, единица процент, значение {value}."
            source = self.add_document(text, article_id=20 + index)
            draft["facts"].append({"id": f"rate:{index}", "source_id": source, "quote": text})
        report = verify_draft(DraftInsight.model_validate(draft), self.context)
        self.assertFalse(report.passed)
        self.assertIn("UNNORMALIZED_NUMERIC_CLAIM", [item.code for item in report.issues])

    def test_one_unscoped_rate_is_reported_with_warning_and_no_quantitative_conclusion(self):
        event = self.event.model_copy(update={"content": "Ключевая ставка повышена до 14%."})
        context = GroundedRunContext(self.company, event, [], AgentConfig())
        draft = DraftInsight.model_validate(valid_draft(event))
        report = verify_draft(draft, context)
        self.assertTrue(report.passed)
        self.assertIn("NUMERIC_SCOPE_UNRESOLVED", report.warnings)
        insight = project_insight(draft, context, report)
        self.assertIn("14%", insight.what_happened)
        self.assertNotIn("14", insight.consequences)

    def test_conflict_cannot_be_fabricated_with_duplicate_or_unrelated_ids(self):
        for pair in (["event", "event"], ["event", "missing"]):
            with self.subTest(pair=pair):
                draft = valid_draft()
                draft["acknowledged_conflicts"] = [pair]
                report = verify_draft(DraftInsight.model_validate(draft), self.context)
                self.assertIn("INVALID_CONFLICT_REFERENCE", [item.code for item in report.issues])

    def test_decimal_comma_and_dot_are_equivalent_without_substring_matching(self):
        for text_value in ("14,0", "14.0"):
            with self.subTest(value=text_value):
                text = f"Сектор: ставка в период октябрь, единица процент, значение {text_value}."
                source = self.add_document(text)
                draft = valid_draft()
                draft["facts"].append({"id": "numeric", "source_id": source, "quote": text,
                                       "metric": "ставка", "entity": "Сектор", "unit": "процент", "period": "октябрь", "value": 14})
                self.assertTrue(verify_draft(DraftInsight.model_validate(draft), self.context).passed)


class AdversarialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_http_body_is_rejected_before_json_parsing(self):
        config = ChatCompletionsConfig(api_url="https://example.invalid", model="offline", max_http_response_bytes=1024)
        client = ChatCompletionsClient(config, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 2048)))
        with self.assertRaises(LLMResponseError):
            await client.generate("offline")
        self.assertIsNone(client.last_usage)

    async def test_cancelled_run_exposes_safe_attempt_report_without_swallowing_cancel(self):
        class Pending:
            async def generate(self, prompt):
                await asyncio.Future()
        company, event = fixtures()
        task = asyncio.create_task(AgentInsightEngine(Pending()).generate_analysis(company, event, [], RecordingTracer()))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.run_report["calls"][0]["status"], "cancelled")
        self.assertIsNone(caught.exception.run_report["totals"]["total_tokens"])

    async def test_recovery_search_is_followed_by_redraft_without_second_repair(self):
        from backend.app.agent.tests.test_grounded import DynamicClient
        company, event = fixtures()
        bad = valid_draft()
        bad["facts"].append({"id": "unknown", "source_id": "bank:missing", "quote": "Неизвестный источник."})
        client = DynamicClient([bad, {"action": "tool", "tool": "get_bank_analytics", "arguments": {"query": "ставка"}}, valid_draft()])
        source = EmptySearchSource()
        result = await AgentInsightEngine(client, analytics_source=source).generate_analysis(company, event, [], RecordingTracer())
        self.assertEqual(len(client.prompts), 3)
        self.assertEqual(result.report["validation_summary"]["recovery_searches"], 1)
        self.assertEqual(result.report["validation_summary"]["repairs"], 0)
        self.assertEqual(source.queries, ["ставка"])


if __name__ == "__main__":
    unittest.main()
