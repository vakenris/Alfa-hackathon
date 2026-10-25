"""Production-path tests: legacy JSON cannot bypass grounding, and recovery is bounded."""

import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from backend.app.agent import AgentConfig, AgentInsightEngine, AnalyticsDocument, FakeLLMClient, MockAnalyticsSource, RecordingTracer
from backend.app.agent.api_client import ChatCompletionsClient
from backend.app.agent.config import ChatCompletionsConfig
from backend.app.agent.context import GroundedRunContext, json_text
from backend.app.agent.models import (
    AgentConfigurationError, AgentEvidenceError, AgentResponseError, AgentTimeoutError,
    AgentLLMTimeoutError, BANK_VIEW_UNAVAILABLE, DISCLAIMER,
)
from backend.app.agent.runtime import RuntimeAgentEngine
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


def fixtures():
    company = CompanyResponse(id=1, synthetic_u="U_TEST", name="Синтетический завод",
                              cl_product_loa_mark=True, revenue_company=1000)
    event = NewsArticleResponse(id=10, title="Повышение ключевой ставки", source="Тестовая фикстура",
                               published_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                               category="Экономика", content="Ключевая ставка повышена. Условия договоров неизвестны.")
    return company, event


def valid_draft(event=None):
    if event is None:
        _, event = fixtures()
    return {
        "action": "draft", "applicability": "conditional",
        "facts": [{"id": "event", "source_id": f"news:{event.id}", "quote": event.content}],
        "impacts": [{"id": "financing", "channel": "financing", "direction": "risk",
                     "mechanism": "Стоимость нового финансирования может ограничивать возможности привлечения заёмных средств.",
                     "fact_ids": ["event"], "company_features": ["cl_product_loa_mark"],
                     "assumptions": ["Компания рассматривает новое заёмное финансирование."],
                     "data_needs": ["Условия кредитных договоров и планы финансирования."], "horizon": "short"}],
        "actions": [{"title": "Запросить условия действующих договоров и варианты нового финансирования.",
                     "impact_ids": ["financing"], "required_data": ["Формулы ставок и даты пересмотра."], "owner_role": "TREASURY"}],
        "missing_data": ["Остатки обязательств и формулы договорных ставок."],
    }


class DynamicClient:
    def __init__(self, steps):
        self.steps = list(steps)
        self.prompts = []
        self.last_usage = None

    async def generate(self, prompt):
        index = len(self.prompts)
        self.prompts.append(prompt)
        step = self.steps[index]
        if isinstance(step, Exception):
            raise step
        payload = step(prompt) if callable(step) else step
        self.last_usage = {"prompt_tokens": 20 + index, "completion_tokens": 10, "total_tokens": 30 + index}
        return payload if isinstance(payload, str) else json_text(payload)


class EmptySearchSource:
    """A real search boundary may return empty for multiple different queries."""
    def __init__(self):
        self.queries = []

    async def search(self, query, *, company, event, limit):
        self.queries.append(query)
        return []


class GroundedEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.company, self.event = fixtures()

    async def run_steps(self, steps, *, source=None, config=None, company=None, counterparties=None):
        self.client = DynamicClient(steps)
        self.engine = AgentInsightEngine(self.client, analytics_source=source, config=config)
        return await self.engine.generate_analysis(company or self.company, self.event, counterparties or [], RecordingTracer())

    async def test_default_path_projects_grounded_draft_and_public_contract(self):
        result = await self.run_steps([valid_draft()])
        self.assertTrue(result.verification.passed)
        self.assertIsInstance(result.insight, MLInsightResult)
        self.assertIn("Гипотеза AI", result.insight.consequences)
        self.assertIn(DISCLAIMER, result.insight.recommended_action)
        self.assertEqual(result.insight.bank_view, BANK_VIEW_UNAVAILABLE)
        self.assertIsNone(result.insight.relevant_bank_product)
        self.assertIn("revenue_company", result.business_context.unknown_features)
        self.assertNotIn("revenue_company", result.business_context.known_features)
        self.assertEqual(result.report["totals"]["total_tokens"], 30)
        self.assertEqual(set(result.insight.model_dump()), set(MLInsightResult.model_fields))

    async def test_generate_insight_uses_the_same_strict_path(self):
        engine = AgentInsightEngine(FakeLLMClient(json_text(valid_draft())))
        result = await engine.generate_insight(self.company, self.event, [], RecordingTracer())
        self.assertIsInstance(result, MLInsightResult)
        self.assertIn(DISCLAIMER, result.recommended_action)

    async def test_implicit_fake_is_not_a_production_fallback(self):
        with self.assertRaises(AgentConfigurationError):
            await AgentInsightEngine().generate_insight(self.company, self.event, [], RecordingTracer())

    async def test_legacy_direct_and_final_wrapper_cannot_bypass_default_grounding(self):
        legacy = {key: "Unverified prose" for key in ("what_happened", "why_important", "consequences", "bank_view", "ai_view", "recommended_action")}
        for payload in (legacy, {"action": "final", "result": legacy}):
            with self.subTest(payload=payload), self.assertRaises(AgentResponseError):
                await self.run_steps([payload], config=AgentConfig(max_llm_calls=1))
            self.assertEqual(len(self.client.prompts), 1)

    async def test_forced_finalization_cannot_request_more_tools(self):
        with self.assertRaises(AgentResponseError):
            await self.run_steps([{"action": "tool", "tool": "get_bank_analytics", "arguments": {}}],
                                 config=AgentConfig(max_llm_calls=1))

    async def test_bad_json_has_only_one_bounded_repair(self):
        result = await self.run_steps(["not json", valid_draft()])
        self.assertEqual(result.report["validation_summary"]["repairs"], 1)
        self.assertEqual(len(self.client.prompts), 2)
        self.assertIn("INVALID_GROUNDED_JSON", self.client.prompts[-1])
        with self.assertRaises(AgentResponseError) as caught:
            await self.run_steps(["bad", "bad"])
        self.assertEqual(len(caught.exception.run_report["calls"]), 2)

    async def test_unsupported_causal_statement_is_repaired_and_rechecked(self):
        bad = valid_draft()
        bad["impacts"][0]["mechanism"] = "Платежи компании обязательно вырастут."
        result = await self.run_steps([bad, valid_draft()])
        self.assertTrue(result.verification.passed)
        self.assertIn("FALSE_CERTAINTY", self.client.prompts[-1])
        self.assertNotIn("обязательно", result.insight.consequences)

    async def test_valid_quote_does_not_allow_false_bank_attribution(self):
        bad = valid_draft()
        bad["impacts"][0]["mechanism"] = "Банк рекомендует увеличить кредиты, что может улучшить финансирование."
        with self.assertRaises(AgentEvidenceError) as caught:
            await self.run_steps([bad, bad])
        self.assertEqual(len(caught.exception.run_report["calls"]), 2)
        self.assertIn("UNSUPPORTED_BANK_ATTRIBUTION", self.client.prompts[-1])

    async def test_empty_tool_result_is_visible_and_a_different_query_is_possible(self):
        tool = {"action": "tool", "tool": "get_bank_analytics", "arguments": {"query": "ставка"}}
        second = copy.deepcopy(tool)
        second["arguments"]["query"] = "кредитные условия"
        source = EmptySearchSource()
        result = await self.run_steps([tool, second, valid_draft()], source=source)
        self.assertEqual(result.report["validation_summary"]["tool_calls"], 2)
        self.assertIn('"documents":[]', self.client.prompts[1])
        self.assertEqual(len(self.client.prompts), 3)
        self.assertEqual(source.queries, ["ставка", "кредитные условия"])

    async def test_verified_bank_quote_flows_from_tool_to_deterministic_bank_view(self):
        document = AnalyticsDocument(
            article=self.event.model_copy(update={"id": 20, "title": "Синтетический тестовый документ",
                                                 "content": "Новые кредитные условия зависят от стоимости фондирования.",
                                                 "is_bank_analytics": True}),
            source_url="https://example.invalid/fixture", provenance_status="verified", source_document_id="fixture-doc",
            source_page=2,
        )
        def final(prompt):
            context = json.loads(prompt.split("Входные данные (JSON):\n", 1)[1])
            item = next(source for source in context["sources"] if source["source_id"].startswith("bank:"))
            draft = valid_draft()
            draft["facts"].append({"id": "bank", "source_id": item["source_id"], "kind": "bank_statement", "quote": document.article.content})
            return draft
        result = await self.run_steps([
            {"action": "tool", "tool": "get_bank_analytics", "arguments": {"query": "ставка"}}, final,
        ], source=MockAnalyticsSource([document]), config=AgentConfig(max_llm_calls=2))
        self.assertIn(document.article.content, result.insight.bank_view)
        self.assertEqual(result.sources[-1].source_page, 2)
        self.assertEqual(result.insight.evidences[-2].source_url, str(document.source_url))

    async def test_three_long_documents_fit_as_visible_excerpts_with_honest_coverage(self):
        from backend.app.agent.backend_source import BackendAnalyticsSource
        documents = [AnalyticsDocument(article=self.event.model_copy(update={
            "id": 20 + index, "title": f"Кредитные ставки: тестовый материал {index}", "is_bank_analytics": True,
            "content": "Кредитные условия зависят от стоимости фондирования. " * 200,
            "summary": "Аналитика кредитных ставок. " * 200,
        })) for index in range(3)]
        async def load(limit):
            return documents
        result = await self.run_steps([
            {"action": "tool", "tool": "get_bank_analytics", "arguments": {"query": "кредиты", "limit": 3}},
            valid_draft(),
        ], source=BackendAnalyticsSource(load))
        payload = json.loads(self.client.prompts[-1].split("Входные данные (JSON):\n", 1)[1])
        observation = payload["observations"][0]
        self.assertEqual(observation["status"], "ok")
        self.assertEqual(len(observation["data"]["documents"]), 3)
        self.assertFalse(observation["data"]["retrieval"]["exhaustive"])
        self.assertEqual(observation["data"]["retrieval"]["candidate_limit"], 50)
        self.assertTrue(all(source.text_truncated for source in result.sources if source.source_id.startswith("bank:")))
        self.assertLessEqual(len(json_text(payload)), AgentConfig().max_context_chars)

    async def test_unverified_seed_cannot_become_bank_position(self):
        bad = valid_draft()
        bad["facts"][0]["kind"] = "bank_statement"
        with self.assertRaises(AgentEvidenceError):
            await self.run_steps([bad, bad])
        self.assertIn("BANK_SOURCE_NOT_VERIFIED", self.client.prompts[-1])

    async def test_insufficient_company_data_has_no_invented_impacts(self):
        minimal = CompanyResponse(id=2, synthetic_u="M", name="Неизвестная компания")
        draft = valid_draft()
        draft.update(applicability="insufficient", impacts=[], actions=[], missing_data=["Отрасль и условия финансирования."])
        result = await self.run_steps([draft], company=minimal)
        self.assertIn("не установлена", result.insight.why_important)
        self.assertFalse(result.analysis.impacts)

    async def test_counterparty_directions_and_self_transfers_are_neutral(self):
        rows = [CounterpartyItem(synthetic_u=uid, direction=direction, total_amount=amount, transaction_count=1)
                for uid, direction, amount in (("U_TEST", "SUPPLIER", 100), ("X", "CUSTOMER", 50))]
        result = await self.run_steps([
            {"action": "tool", "tool": "summarize_counterparties", "arguments": {}}, valid_draft(),
        ], counterparties=rows)
        payload = json.loads(self.client.prompts[-1].split("Входные данные (JSON):\n", 1)[1])
        self.assertEqual(set(payload["observations"][0]["data"]["directions"]), {"self_transfer", "incoming"})
        self.assertEqual(result.business_context.self_transfer_rows, 1)
        self.assertEqual(rows[0].direction, "SUPPLIER")

    async def test_response_duplicates_and_nonfinite_values_are_rejected(self):
        for response in ('{"action":"draft","action":"tool"}', '{"value":NaN}',
                         '{"action":"tool","tool":"get_bank_analytics","arguments":{"limit":1e999}}', "x" * 25000):
            with self.subTest(response=response[:50]), self.assertRaises(AgentResponseError):
                await self.run_steps([response], config=AgentConfig(max_llm_calls=1))

    async def test_run_and_call_timeouts_and_cancellation(self):
        class Pending:
            async def generate(self, prompt):
                await asyncio.Future()
        for config, error in ((AgentConfig(llm_timeout_seconds=.01), AgentLLMTimeoutError),
                              (AgentConfig(run_timeout_seconds=.01), AgentTimeoutError)):
            with self.subTest(error=error), self.assertRaises(error) as caught:
                await AgentInsightEngine(Pending(), config=config).generate_analysis(self.company, self.event, [], RecordingTracer())
            self.assertEqual(len(caught.exception.run_report["calls"]), 1)
        task = asyncio.create_task(AgentInsightEngine(Pending()).generate_insight(self.company, self.event, [], RecordingTracer()))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_http_mock_exercises_actual_adapter_and_grounded_engine(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json_text(valid_draft())}}],
                                            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
        config = ChatCompletionsConfig(api_url="https://example.invalid/completions", model="offline-fixture")
        result = await AgentInsightEngine(ChatCompletionsClient(config, transport=httpx.MockTransport(handler))).generate_analysis(
            self.company, self.event, [], RecordingTracer())
        self.assertEqual(len(requests), 1)
        self.assertTrue(result.report["usage_complete"])
        self.assertIn("JSON Schema", requests[0]["messages"][0]["content"])

    async def test_reports_and_sources_do_not_leak_between_concurrent_runs(self):
        def client_factory():
            return DynamicClient([valid_draft()])
        engine = RuntimeAgentEngine(client_factory=client_factory, source_factory=MockAnalyticsSource, allow_test_dependencies=True)
        first, second = await asyncio.gather(*(
            engine.generate_analysis(self.company, self.event, [], RecordingTracer()) for _ in range(2)))
        self.assertEqual([len(first.report["calls"]), len(second.report["calls"])], [1, 1])
        first.report["calls"].clear()
        self.assertEqual(len(second.report["calls"]), 1)


if __name__ == "__main__":
    unittest.main()
