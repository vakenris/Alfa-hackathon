"""Offline behavioral checks: bounded execution, tool recovery and provenance."""

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from backend.app.agent import (
    AgentConfig, CompatibilityInsightEngine as AgentInsightEngine, AnalyticsDocument, FakeLLMClient,
    MockAnalyticsSource, RecordingTracer,
)
from backend.app.agent.context import json_text
from backend.app.agent.models import (
    BANK_VIEW_UNAVAILABLE, AgentContextError, AgentEvidenceError, AgentLLMError,
    AgentResponseError, AgentTimeoutError,
)
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


def tool(name="get_bank_analytics", **arguments):
    return json_text({"action": "tool", "tool": name, "arguments": arguments})


def context_of(prompt):
    return json.loads(prompt.split("Входные данные (JSON):\n", 1)[1])


def evidence(title, url=None, feature=None):
    return {"claim": "Тестовое утверждение", "source_title": title,
            "source_url": url, "company_feature": feature}


class MultiStepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Any unexpected real HTTP connection must fail the test.
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.company = CompanyResponse(
            id=1, synthetic_u="TEST", name="Завод", cl_product_loa_mark=True,
        )
        self.event = NewsArticleResponse(
            id=10, title="Повышение ставки", source="Тестовый источник",
            published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
            content="Ставка повышена. Условия кредитов завода неизвестны.", category="Экономика",
        )
        self.document = AnalyticsDocument(
            article=self.event.model_copy(update={
                "id": 20, "title": "Банковский обзор", "is_bank_analytics": True,
                "content": "В тестовом обзоре отмечается рост стоимости новых кредитов.",
            }),
            source_url="https://example.test/bank/review",
        )
        self.result = {
            "what_happened": "Ставка повышена.", "why_important": "Есть признак кредитных продуктов.",
            "consequences": "Расходы зависят от условий договоров.",
            "bank_view": BANK_VIEW_UNAVAILABLE, "ai_view": "Нужно уточнить условия.",
            "recommended_action": "Проверить договоры.", "relevant_bank_product": None,
            "evidences": [evidence("Профиль компании", feature="cl_product_loa_mark=true")],
        }
        self.tracer = RecordingTracer()

    async def run_script(self, responses, *, config=None, source=None, event=None, counterparties=None):
        self.client = FakeLLMClient(responses=responses)
        self.engine = AgentInsightEngine(self.client, config=config, analytics_source=source)
        return await self.engine.generate_insight(
            self.company, event or self.event, counterparties or [], self.tracer,
        )

    async def test_multiple_tools_then_enveloped_early_final(self):
        self.result["bank_view"] = "В обзоре отмечен рост стоимости новых кредитов."
        self.result["evidences"].append(evidence(
            "Банковский обзор", "https://example.test/bank/review",
        ))
        source = AsyncMock()
        source.search.return_value = [self.document]
        result = await self.run_script([
            tool(query="ставка", limit=1), tool("summarize_counterparties"),
            json_text({"action": "final", "result": self.result}),
        ], source=source, counterparties=[CounterpartyItem(
            synthetic_u="SUPPLIER", direction="SUPPLIER", total_amount=1000, transaction_count=2,
        )])
        self.assertEqual(result.bank_view, self.result["bank_view"])
        self.assertEqual(len(self.client.prompts), 3)
        source.search.assert_awaited_once_with("ставка", company=self.company, event=self.event, limit=1)
        observations = context_of(self.client.prompts[-1])["observations"]
        self.assertEqual([item["status"] for item in observations], ["ok", "ok"])
        self.assertEqual(observations[1]["data"]["directions"]["SUPPLIER"]["total_amount"], 1000)
        self.assertEqual([step["step_name"] for step in self.tracer.steps], [
            "Подготовка промпта", "Вызов клиента", "Выбор инструмента", "Результат инструмента",
            "Вызов клиента", "Выбор инструмента", "Результат инструмента",
            "Вызов клиента", "Проверка формата",
        ])

    async def test_tool_failure_is_observed_and_analysis_continues(self):
        source = AsyncMock()
        source.search.side_effect = RuntimeError("private source error")
        result = await self.run_script([tool(), json_text(self.result)], source=source)
        self.assertEqual(result.bank_view, BANK_VIEW_UNAVAILABLE)
        observation = context_of(self.client.prompts[1])["observations"][0]
        self.assertEqual(observation["error"], "TOOL_FAILED")
        self.assertNotIn("private source error", self.client.prompts[1] + str(self.tracer.steps))

    async def test_tool_timeout_is_recoverable(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        source = AsyncMock()
        source.search.side_effect = slow
        await self.run_script([tool(), json_text(self.result)], source=source,
                              config=AgentConfig(tool_timeout_seconds=0.01))
        self.assertEqual(context_of(self.client.prompts[1])["observations"][0]["error"], "TOOL_TIMEOUT")

    async def test_call_and_step_limits_reserve_separate_finalization(self):
        for config in (AgentConfig(max_steps=2), AgentConfig(max_llm_calls=2), AgentConfig(max_tool_calls=1)):
            with self.subTest(config=config):
                await self.run_script([tool(), json_text(self.result)], config=config)
                self.assertEqual(len(self.client.prompts), 2)
                self.assertIn("Режим: планирование", self.client.prompts[0])
                self.assertIn("Режим: финализация", self.client.prompts[1])
                self.assertNotIn("Схема действия", self.client.prompts[1])

    async def test_single_call_or_zero_tools_uses_finalization_immediately(self):
        for config in (AgentConfig(max_llm_calls=1), AgentConfig(max_steps=1), AgentConfig(max_tool_calls=0)):
            with self.subTest(config=config):
                await self.run_script([json_text(self.result)], config=config)
                self.assertEqual(len(self.client.prompts), 1)
                self.assertIn("Режим: финализация", self.client.prompts[0])

    async def test_last_call_rejects_more_tools_or_invalid_result(self):
        for response in (tool(), "not JSON", "{}", json_text({"action": "final", "result": self.result})):
            with self.subTest(response=response):
                source = AsyncMock()
                with self.assertRaisesRegex(AgentResponseError, "Финализация"):
                    await self.run_script([response], config=AgentConfig(max_llm_calls=1), source=source)
                source.search.assert_not_awaited()
                self.assertEqual(len(self.client.prompts), 1)

    async def test_invalid_json_and_unknown_actions_stop_without_retry(self):
        for response in ('{"action":', "[]", "null", tool("run_sql"),
                         json_text({"action": "wait"}), "```json\n{}\n```", "x" * 25_000):
            with self.subTest(response=response[:50]):
                with self.assertRaises(AgentResponseError):
                    await self.run_script([response])
                self.assertEqual(len(self.client.prompts), 1)
                self.assertEqual(self.tracer.steps[-1]["step_name"], "Ошибка валидации")

    async def test_invalid_tool_arguments_do_not_reach_source(self):
        source = AsyncMock()
        await self.run_script([tool(limit=999, company_id=99), json_text(self.result)], source=source)
        source.search.assert_not_awaited()
        self.assertEqual(context_of(self.client.prompts[1])["observations"][0]["error"], "INVALID_ARGUMENTS")

    async def test_duplicate_request_is_not_executed_and_forces_finalization(self):
        source = AsyncMock()
        source.search.return_value = []
        await self.run_script([
            tool(query="ставка", limit=1), tool(limit=1, query="ставка"), json_text(self.result),
        ], source=source)
        source.search.assert_awaited_once()
        self.assertIn("Режим: финализация", self.client.prompts[2])
        self.assertEqual(context_of(self.client.prompts[2])["observations"][-1]["error"], "DUPLICATE_REQUEST")

    async def test_default_arguments_and_explicit_defaults_are_the_same_request(self):
        source = AsyncMock()
        source.search.return_value = []
        await self.run_script([
            tool(), tool(query="", limit=3), json_text(self.result),
        ], source=source)
        source.search.assert_awaited_once()
        self.assertEqual(context_of(self.client.prompts[2])["observations"][-1]["error"], "DUPLICATE_REQUEST")

    async def test_no_bank_materials_cannot_produce_a_bank_position(self):
        self.result["bank_view"] = "Банк рекомендует немедленно взять кредит."
        self.result["relevant_bank_product"] = "Выдуманный продукт"
        result = await self.run_script([json_text(self.result)])
        self.assertEqual(result.bank_view, BANK_VIEW_UNAVAILABLE)
        self.assertIsNone(result.relevant_bank_product)
        self.assertIn("Ограничение источников", [step["step_name"] for step in self.tracer.steps])

    async def test_unknown_title_url_and_company_feature_are_rejected(self):
        for item in (
            evidence("Неизвестный источник"),
            evidence(self.event.title, "https://invented.test/article"),
            evidence("Профиль компании", "https://invented.test/company"),
            evidence("Профиль компании", feature="revenue_company=1000000"),
            evidence("Профиль компании", feature="cl_product_loa_mark=false"),
        ):
            with self.subTest(evidence=item):
                payload = {**self.result, "evidences": [item]}
                with self.assertRaises(AgentEvidenceError):
                    await self.run_script([json_text(payload)])
                self.assertEqual(self.tracer.steps[-1]["step_name"], "Ошибка evidence")

    async def test_fixture_source_is_not_citable_before_tool_was_called(self):
        self.result["evidences"] = [evidence("Банковский обзор")]
        with self.assertRaises(AgentEvidenceError):
            await self.run_script([json_text(self.result)], source=MockAnalyticsSource([self.document]))

    async def test_source_url_must_match_the_cited_document(self):
        second = AnalyticsDocument(
            article=self.document.article.model_copy(update={"id": 21, "title": "Второй обзор"}),
            source_url="https://example.test/bank/second",
        )
        self.result["evidences"] = [evidence("Банковский обзор", "https://example.test/bank/second")]
        with self.assertRaises(AgentEvidenceError):
            await self.run_script([tool(), json_text(self.result)],
                                  source=MockAnalyticsSource([self.document, second]))

    async def test_bank_position_requires_evidence_on_bank_material(self):
        self.result["bank_view"] = "Банковский вывод без источника."
        with self.assertRaisesRegex(AgentEvidenceError, "bank_view"):
            await self.run_script([tool(), json_text(self.result)], source=MockAnalyticsSource([self.document]))

    async def test_initial_bank_event_can_support_bank_view_without_tool(self):
        self.result["bank_view"] = "В банковском материале отмечено повышение ставки."
        self.result["evidences"] = [evidence(self.document.article.title)]
        result = await self.run_script([json_text(self.result)], event=self.document.article)
        self.assertEqual(result.bank_view, self.result["bank_view"])

    async def test_ambiguous_titles_are_rejected(self):
        second = self.document.model_copy(update={
            "article": self.document.article.model_copy(update={"id": 21}),
        })
        self.result["evidences"] = [evidence("Банковский обзор")]
        with self.assertRaisesRegex(AgentEvidenceError, "неоднозначный"):
            await self.run_script([tool(), json_text(self.result)],
                                  source=MockAnalyticsSource([self.document, second]))

    async def test_oversized_tool_result_is_not_added_or_citable(self):
        large = self.document.model_copy(update={
            "article": self.document.article.model_copy(update={"content": "А" * 30_000}),
        })
        await self.run_script([tool(), json_text(self.result)], source=MockAnalyticsSource([large]),
                              config=AgentConfig(max_tool_result_chars=512))
        final_context = context_of(self.client.prompts[-1])
        self.assertEqual(final_context["observations"][0]["error"], "CONTEXT_LIMIT")
        self.assertFalse(any(item["is_bank_analytics"] for item in final_context["sources"]))
        self.assertIn("Режим: финализация", self.client.prompts[-1])
        self.result["evidences"] = [evidence("Банковский обзор")]
        with self.assertRaises(AgentEvidenceError):
            await self.run_script([tool(), json_text(self.result)], source=MockAnalyticsSource([large]),
                                  config=AgentConfig(max_tool_result_chars=512))

    async def test_accumulated_context_and_input_are_bounded_valid_json(self):
        large_event = self.event.model_copy(update={"content": "Факт. " * 20_000})
        config = AgentConfig(max_context_chars=3000, max_article_chars=100_000)
        counterparties = [CounterpartyItem(
            synthetic_u=f"C{i}", direction="CUSTOMER", total_amount=100, transaction_count=1,
        ) for i in range(100)]
        await self.run_script([tool(), tool("summarize_counterparties"), json_text(self.result)],
                              config=config, event=large_event, counterparties=counterparties)
        for prompt in self.client.prompts:
            context = context_of(prompt)
            self.assertLessEqual(len(json_text(context)), config.max_context_chars)
            self.assertTrue(context["limits"]["event_text_truncated"])
            self.assertGreater(context["limits"]["counterparties_omitted"], 0)
        self.assertEqual(large_event.content, "Факт. " * 20_000)

    async def test_cumulative_limit_rejects_only_new_result_and_retains_old_sources(self):
        first = AnalyticsDocument(article=self.document.article.model_copy(update={"content": "А" * 1800}))
        second = AnalyticsDocument(article=first.article.model_copy(update={"id": 21, "title": "Новый обзор"}))
        source = AsyncMock()
        source.search.side_effect = [[first], [second]]
        self.result["evidences"].append(evidence("Банковский обзор"))
        config = AgentConfig(max_context_chars=5000, max_tool_result_chars=4000)
        await self.run_script([tool(query="первый"), tool(query="второй"), json_text(self.result)],
                              source=source, config=config)
        context = context_of(self.client.prompts[-1])
        self.assertEqual(context["observations"][0]["status"], "ok")
        self.assertEqual(context["observations"][1]["error"], "CONTEXT_LIMIT")
        self.assertIn("Банковский обзор", [item["source_title"] for item in context["sources"]])
        self.assertNotIn("Новый обзор", [item["source_title"] for item in context["sources"]])
        self.assertIn("Режим: финализация", self.client.prompts[-1])
        self.assertLessEqual(len(json_text(context)), config.max_context_chars)

    async def test_unshrinkable_input_fails_before_llm(self):
        self.company.name = "X" * 10_000
        with self.assertRaises(AgentContextError):
            await self.run_script([json_text(self.result)], config=AgentConfig(max_context_chars=2048))
        self.assertEqual(self.client.prompts, [])

    async def test_llm_failure_is_sanitized_and_not_retried(self):
        with self.assertRaises(AgentLLMError) as caught:
            await self.run_script([RuntimeError("private prompt and secret")])
        self.assertNotIn("private", str(caught.exception) + str(self.tracer.steps))
        self.assertEqual(len(self.client.prompts), 1)

    async def test_llm_and_total_timeouts(self):
        for config, error in (
            (AgentConfig(llm_timeout_seconds=0.01), AgentLLMError),
            (AgentConfig(run_timeout_seconds=0.01), AgentTimeoutError),
        ):
            with self.subTest(error=error):
                client = AsyncMock()
                async def slow(prompt):
                    await asyncio.sleep(1)
                client.generate.side_effect = slow
                engine = AgentInsightEngine(client, config=config)
                with self.assertRaises(error):
                    await engine.generate_insight(self.company, self.event, [], self.tracer)
                self.assertEqual(client.generate.await_count, 1)

    async def test_cancellation_and_tracer_failure_are_not_swallowed(self):
        client = AsyncMock()
        client.generate.side_effect = asyncio.CancelledError()
        engine = AgentInsightEngine(client)
        with self.assertRaises(asyncio.CancelledError):
            await engine.generate_insight(self.company, self.event, [], self.tracer)
        tracer = AsyncMock()
        tracer.emit_step.side_effect = RuntimeError("tracer unavailable")
        client.generate.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "tracer unavailable"):
            await engine.generate_insight(self.company, self.event, [], tracer)
        client.generate.assert_not_awaited()

    async def test_runs_on_same_engine_do_not_share_observations_or_sources(self):
        self.result["evidences"] = [evidence("Банковский обзор")]
        client = FakeLLMClient(responses=[tool(), json_text(self.result), json_text(self.result)])
        engine = AgentInsightEngine(client, analytics_source=MockAnalyticsSource([self.document]))
        await engine.generate_insight(self.company, self.event, [], self.tracer)
        with self.assertRaises(AgentEvidenceError):
            await engine.generate_insight(self.company, self.event, [], RecordingTracer())
        self.assertEqual(context_of(client.prompts[2])["observations"], [])


class AgentConfigTests(unittest.TestCase):
    def test_invalid_limits_are_rejected(self):
        for kwargs in ({"max_steps": 0}, {"max_llm_calls": 0}, {"max_tool_calls": -1},
                       {"max_context_chars": 1}, {"max_response_chars": 1},
                       {"tool_timeout_seconds": float("inf")}, {"max_steps": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                AgentConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
