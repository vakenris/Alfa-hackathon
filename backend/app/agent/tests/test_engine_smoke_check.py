"""Full engine HTTP-mock checks; every credential and document is synthetic."""

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from backend.app.agent.api_client import ChatCompletionsClient, LLMHTTPError
from backend.app.agent import AgentConfig, FakeLLMClient, RecordingTracer
from backend.app.agent.engine_smoke_check import (
    BoundedSmokeClient, MAX_LLM_CALLS, MAX_OUTPUT_TOKENS, SmokeCallLimitError,
    engine_smoke_config, main, make_scenario, mock_final_result, run_check,
)
from backend.app.agent.models import BANK_VIEW_UNAVAILABLE
from backend.app.agent.orchestrator import CompatibilityInsightEngine as AgentInsightEngine
from backend.app.agent.prompts import COMPACT_FINALIZATION_RULES
from backend.app.ml.interface import MLInsightResult


MODULE = "backend.app.agent.engine_smoke_check"
TOOL = {"action": "tool", "tool": "get_bank_analytics", "arguments": {"limit": 1}}
MISSING = object()


def completion(payload, *, usage=MISSING, reason="stop"):
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    body = {"choices": [{"finish_reason": reason, "message": {"content": content}}]}
    if usage is not MISSING:
        body["usage"] = usage
    return httpx.Response(200, json=body)


class EngineSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.config = engine_smoke_config()
        self.scenario = make_scenario()
        self.final = mock_final_result(self.scenario)
        self.requests = []
        scenario_patch = patch(MODULE + ".make_scenario", return_value=self.scenario)
        scenario_patch.start()
        self.addCleanup(scenario_patch.stop)
        network_patch = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def scripted_transport(self, responses):
        def handler(request):
            index = len(self.requests)
            self.requests.append(request)
            if index >= len(responses):
                raise AssertionError("Unexpected extra HTTP request")
            response = responses[index]
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.MockTransport(handler)

    async def script(self, responses, **kwargs):
        return await run_check(self.config, transport=self.scripted_transport(responses), **kwargs)

    async def test_default_http_mock_runs_full_engine_without_usage_or_network(self):
        report = await run_check(self.config)
        self.assertTrue(report["success"])
        self.assertEqual(report["mode"], "mock")
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["tool_calls"], 1)
        self.assertEqual(report["selected_tool"], "get_bank_analytics")
        self.assertTrue(all(report["checks"].values()))
        self.assertFalse(report["usage_complete"])
        self.assertIsNone(report["total_usage"])
        self.assertTrue(all(call["usage"] is None for call in report["calls"]))

    async def test_document_is_unknown_before_tool_and_visible_to_forced_finalization(self):
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertTrue(report["success"])
        payloads = [json.loads(request.content) for request in self.requests]
        prompts = [body["messages"][0]["content"] for body in payloads]
        self.assertNotIn(self.scenario.quote, prompts[0])
        self.assertIn(self.scenario.quote, prompts[1])
        self.assertIn("Режим: планирование.", prompts[0])
        self.assertIn("Режим: финализация.", prompts[1])
        self.assertNotIn("Схема действия", prompts[1])
        initial = json.loads(prompts[0].split("Входные данные (JSON):\n", 1)[1])
        self.assertEqual(len(initial["counterparties"]), 2)
        self.assertEqual([body["max_tokens"] for body in payloads], [1536, 1536])

    async def test_compact_instructions_are_only_added_to_smoke_finalization(self):
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertTrue(report["success"])
        prompts = [json.loads(request.content)["messages"][0]["content"] for request in self.requests]
        self.assertNotIn(COMPACT_FINALIZATION_RULES, prompts[0])
        self.assertIn(COMPACT_FINALIZATION_RULES, prompts[1])
        self.assertIn("ровно один элемент", prompts[1])
        self.assertIn("цитату сохрани целиком", prompts[1])
        self.assertIn("в одну строку", prompts[1])

    async def test_normal_engine_keeps_detailed_results_and_has_no_smoke_length_policy(self):
        detailed = {**self.final, "why_important": "Подробный вывод с оговорками. " * 40,
                    "bank_view": BANK_VIEW_UNAVAILABLE, "evidences": []}
        client = FakeLLMClient(json.dumps(detailed, ensure_ascii=False))
        engine = AgentInsightEngine(client, config=AgentConfig(max_llm_calls=1))
        result = await engine.generate_insight(
            self.scenario.company, self.scenario.event, self.scenario.counterparties, RecordingTracer(),
        )
        self.assertEqual(result.why_important, detailed["why_important"])
        self.assertGreater(len(result.why_important), 120)
        self.assertNotIn(COMPACT_FINALIZATION_RULES, client.prompts[0])
        self.assertNotIn("ровно один элемент", client.prompts[0])
        self.assertNotIn("до 120 символов", client.prompts[0])

    async def test_compact_response_preserves_all_fields_and_exact_source_quote(self):
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertTrue(report["success"])
        result = MLInsightResult.model_validate(self.final)
        self.assertEqual(set(result.model_dump()), set(MLInsightResult.model_fields))
        self.assertEqual(len(result.evidences), 1)
        self.assertIn(self.scenario.quote, result.bank_view)
        self.assertEqual(result.evidences[0].claim, self.scenario.quote)
        self.assertEqual(result.evidences[0].source_title, self.scenario.document.article.title)
        self.assertEqual(result.evidences[0].source_url, str(self.scenario.document.source_url))
        self.assertIsNone(result.relevant_bank_product)

    async def test_compact_reply_missing_any_required_field_is_rejected(self):
        required = [name for name, field in MLInsightResult.model_fields.items() if field.is_required()]
        for name in required:
            with self.subTest(field=name):
                self.requests.clear()
                payload = dict(self.final)
                del payload[name]
                report = await self.script([completion(TOOL), completion(payload)])
                self.assertFalse(report["success"])
                self.assertFalse(report["checks"]["final_json_valid"])
                self.assertEqual(report["error"], "InvalidModelResponse")
                self.assertEqual(report["llm_calls"], 2)

    async def test_only_smoke_check_requires_exactly_one_evidence(self):
        self.final["evidences"].append({
            "claim": "В профиле отмечены кредитные продукты.", "source_title": "Профиль компании",
            "source_url": None, "company_feature": "cl_product_loa_mark=true",
        })
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertFalse(report["success"])
        self.assertTrue(report["checks"]["final_json_valid"])
        self.assertTrue(report["checks"]["document_quote_used"])
        self.assertFalse(report["checks"]["document_evidence_valid"])
        self.assertEqual(report["error"], "ScenarioNotCompleted")

    async def test_second_tool_request_is_rejected_without_third_http_call(self):
        report = await self.script([completion(TOOL), completion(TOOL)])
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "InvalidModelResponse")
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["tool_calls"], 1)
        self.assertEqual(len(self.requests), 2)

    async def test_hard_guard_blocks_extra_calls_even_if_engine_ignores_its_budget(self):
        class OverCallingEngine(AgentInsightEngine):
            async def generate_insight(self, *args):
                for _ in range(3):
                    await self.llm_client.generate("Test prompt")
        with patch(MODULE + ".AgentInsightEngine", OverCallingEngine):
            report = await self.script([completion(TOOL), completion(self.final)])
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "CallLimitExceeded")
        self.assertEqual(report["llm_calls"], MAX_LLM_CALLS)
        self.assertEqual(len(self.requests), MAX_LLM_CALLS)

    async def test_failed_calls_also_consume_hard_budget(self):
        adapter = ChatCompletionsClient(self.config, transport=self.scripted_transport([
            httpx.Response(503), httpx.Response(503),
        ]))
        client = BoundedSmokeClient(adapter, self.scenario)
        for _ in range(2):
            with self.assertRaises(LLMHTTPError):
                await client.generate("Test prompt")
        with self.assertRaises(SmokeCallLimitError):
            await client.generate("Must not send")
        self.assertEqual(len(self.requests), 2)

    async def test_concurrent_calls_cannot_bypass_hard_budget(self):
        adapter = ChatCompletionsClient(self.config, transport=self.scripted_transport([
            completion(TOOL), completion(self.final),
        ]))
        client = BoundedSmokeClient(adapter, self.scenario)
        outcomes = await asyncio.gather(*(client.generate("Test prompt") for _ in range(5)), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, SmokeCallLimitError) for item in outcomes), 3)
        self.assertEqual(len(self.requests), 2)

    async def test_citation_alone_does_not_prove_use_of_document(self):
        self.final["bank_view"] = "Общие слова о ликвидности без тестового факта."
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertFalse(report["success"])
        self.assertTrue(report["checks"]["document_evidence_valid"])
        self.assertFalse(report["checks"]["document_quote_used"])
        self.assertEqual(report["error"], "ScenarioNotCompleted")

    async def test_document_quote_in_other_field_does_not_validate_bank_view(self):
        self.final["bank_view"] = BANK_VIEW_UNAVAILABLE
        self.final["ai_view"] = self.scenario.quote
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["document_quote_used"])

    async def test_evidence_must_quote_document_not_only_name_it(self):
        self.final["evidences"][0]["claim"] = "Неподтверждённый вывод"
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertFalse(report["success"])
        self.assertTrue(report["checks"]["document_quote_used"])
        self.assertFalse(report["checks"]["document_evidence_valid"])

    async def test_wrong_evidence_url_title_or_absence_fails(self):
        for field, value in (("source_url", "https://example.invalid/wrong"), ("source_title", "Не получен")):
            with self.subTest(field=field):
                self.requests.clear()
                final = mock_final_result(self.scenario)
                final["evidences"][0][field] = value
                report = await self.script([completion(TOOL), completion(final)])
                self.assertFalse(report["success"])
                self.assertEqual(report["error"], "InvalidEvidence")
        self.requests.clear()
        self.final["evidences"] = []
        report = await self.script([completion(TOOL), completion(self.final)])
        self.assertEqual(report["error"], "InvalidEvidence")

    async def test_early_valid_final_is_not_a_successful_tool_cycle(self):
        self.final.update(bank_view=BANK_VIEW_UNAVAILABLE, evidences=[])
        report = await self.script([completion(self.final)])
        self.assertFalse(report["success"])
        self.assertTrue(report["checks"]["final_json_valid"])
        self.assertFalse(report["checks"]["tool_selected"])
        self.assertFalse(report["checks"]["document_quote_used"])
        self.assertEqual(report["llm_calls"], 1)
        self.assertEqual(report["tool_calls"], 0)

    async def test_wrong_tool_does_not_complete_scenario(self):
        action = {"action": "tool", "tool": "summarize_counterparties", "arguments": {}}
        self.final.update(bank_view=BANK_VIEW_UNAVAILABLE, evidences=[])
        report = await self.script([completion(action), completion(self.final)])
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["tool_selected"])
        self.assertEqual(report["tool_calls"], 0)

    async def test_invalid_json_first_or_second_call_stops_without_retry(self):
        for responses, count in (([completion('{"action":')], 1),
                                 ([completion(TOOL), completion("```json\n{}\n```")], 2)):
            with self.subTest(count=count):
                self.requests.clear()
                report = await self.script(responses)
                self.assertFalse(report["success"])
                self.assertEqual(report["error"], "InvalidModelResponse")
                self.assertEqual(report["llm_calls"], count)
                self.assertFalse(report["checks"]["final_json_valid"])

    async def test_api_errors_are_sanitized_and_not_retried(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.requests.clear()
                report = await self.script([httpx.Response(status, text="PRIVATE_RESPONSE AUTH_SECRET")])
                self.assertFalse(report["success"])
                self.assertEqual(report["error"], "LLMHTTPError")
                self.assertEqual(report["calls"][0]["http_status"], status)
                self.assertEqual(report["llm_calls"], 1)
                self.assertNotIn("PRIVATE_RESPONSE", json.dumps(report))

    async def test_second_api_failure_preserves_first_usage_and_unknown_total(self):
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        report = await self.script([completion(TOOL, usage=usage), httpx.Response(500)])
        self.assertEqual(report["error"], "LLMHTTPError")
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["calls"][0]["usage"], usage)
        self.assertIsNone(report["calls"][1]["usage"])
        self.assertFalse(report["usage_complete"])
        self.assertTrue(all(value is None for value in report["total_usage"].values()))

    async def test_timeout_and_unexpected_client_failure_do_not_leak_details(self):
        for failure, expected in ((httpx.ReadTimeout("AUTH_SECRET"), "LLMTimeoutError"),
                                  (RuntimeError("PRIVATE_PROMPT AUTH_SECRET"), "ClientError")):
            with self.subTest(error=expected):
                self.requests.clear()
                report = await self.script([failure])
                self.assertEqual(report["error"], expected)
                self.assertEqual(report["llm_calls"], 1)
                self.assertNotIn("AUTH_SECRET", json.dumps(report))

    async def test_truncation_retains_usage_without_increasing_limit(self):
        report = await self.script([completion("incomplete", reason="length", usage={"completion_tokens": 1536})])
        self.assertEqual(report["error"], "LLMOutputLimitError")
        self.assertEqual(report["llm_calls"], 1)
        self.assertEqual(report["calls"][0]["usage"], {"completion_tokens": 1536})

    async def test_final_truncation_reports_content_and_reasoning_without_double_counting(self):
        first_usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        last_usage = {"prompt_tokens": 200, "completion_tokens": 1536, "total_tokens": 1736,
                      "completion_tokens_details": {"reasoning_tokens": 1500, "private": "REASONING_SECRET"}}
        report = await self.script([
            completion(TOOL, usage=first_usage),
            completion("", reason="length", usage=last_usage),
        ])
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], "LLMOutputLimitError")
        self.assertEqual(report["llm_calls"], 2)
        self.assertTrue(report["checks"]["forced_finalization"])
        self.assertFalse(report["checks"]["final_json_valid"])
        self.assertEqual(report["calls"][1]["finish_reason"], "length")
        self.assertEqual(report["calls"][1]["content_chars"], 0)
        self.assertEqual(report["calls"][1]["reasoning_tokens"], 1500)
        self.assertIsNone(report["calls"][0]["reasoning_tokens"])
        self.assertEqual(report["total_usage"], {"prompt_tokens": 300, "completion_tokens": 1556, "total_tokens": 1856})
        self.assertNotIn("REASONING_SECRET", json.dumps(report))
        self.assertEqual([json.loads(request.content)["max_tokens"] for request in self.requests], [1536, 1536])

    async def test_truncated_complete_json_is_still_rejected(self):
        report = await self.script([completion(TOOL), completion(self.final, reason="length")])
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["final_json_valid"])
        self.assertEqual(report["error"], "LLMOutputLimitError")
        self.assertEqual(report["llm_calls"], 2)
        self.assertGreater(report["calls"][1]["content_chars"], 0)

    async def test_missing_invalid_or_zero_reasoning_stats_do_not_invent_tokens(self):
        for details, expected in ((None, None), ([], None), ({}, None),
                                  ({"reasoning_tokens": -1}, None), ({"reasoning_tokens": True}, None),
                                  ({"reasoning_tokens": "SECRET"}, None), ({"reasoning_tokens": 1.5}, None),
                                  ({"reasoning_tokens": 0}, 0)):
            with self.subTest(details=details):
                self.requests.clear()
                report = await self.script([
                    completion(TOOL, usage={"completion_tokens_details": details}), completion(self.final),
                ])
                self.assertTrue(report["success"])
                self.assertEqual(report["calls"][0]["reasoning_tokens"], expected)
                self.assertIsNone(report["calls"][1]["reasoning_tokens"])
                self.assertNotIn("SECRET", json.dumps(report))

    async def test_error_after_success_does_not_reuse_diagnostics(self):
        report = await self.script([completion(TOOL), httpx.Response(503)])
        self.assertEqual(report["calls"][0]["finish_reason"], "stop")
        self.assertGreater(report["calls"][0]["content_chars"], 0)
        self.assertIsNone(report["calls"][1]["finish_reason"])
        self.assertIsNone(report["calls"][1]["content_chars"])
        self.assertIsNone(report["calls"][1]["reasoning_tokens"])

    async def test_diagnostics_never_print_content_reasoning_or_arbitrary_finish_reason(self):
        response = httpx.Response(200, json={
            "choices": [{"finish_reason": "PRIVATE_FINISH_REASON", "message": {
                "content": "PRIVATE_CONTENT", "reasoning_content": "PRIVATE_REASONING",
            }}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 12, "private": "PRIVATE_USAGE"}},
        })
        report = await self.script([response])
        self.assertFalse(report["success"])
        self.assertEqual(report["calls"][0]["finish_reason"], "other")
        self.assertEqual(report["calls"][0]["content_chars"], len("PRIVATE_CONTENT"))
        self.assertEqual(report["calls"][0]["reasoning_tokens"], 12)
        self.assertNotIn("PRIVATE_", json.dumps(report))

    async def test_invalid_api_envelope_and_usage_fail_safely(self):
        for response in (httpx.Response(200, text="PRIVATE non JSON"),
                         httpx.Response(200, json={}), completion(TOOL, usage=[])):
            with self.subTest(response=response.status_code):
                self.requests.clear()
                report = await self.script([response])
                self.assertEqual(report["error"], "LLMResponseError")
                self.assertNotIn("PRIVATE", json.dumps(report))

    async def test_usage_is_captured_for_each_call_and_only_safe_numeric_fields_reported(self):
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "private": "SECRET"}
        report = await self.script([completion(TOOL, usage=usage), completion(self.final, usage=usage)])
        self.assertTrue(report["success"])
        self.assertTrue(report["usage_complete"])
        self.assertEqual(report["total_usage"], {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60})
        self.assertNotIn("SECRET", json.dumps(report))

    async def test_null_empty_and_partial_usage_are_not_estimated(self):
        for usage in (None, {}, {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 3.5}, {"total_tokens": 9}):
            with self.subTest(usage=usage):
                self.requests.clear()
                report = await self.script([completion(TOOL, usage=usage), completion(self.final)])
                self.assertTrue(report["success"])
                self.assertFalse(report["usage_complete"])
                self.assertIsNone(report["calls"][1]["usage"])
                if report["total_usage"] is not None:
                    self.assertTrue(all(value is None for value in report["total_usage"].values()))

    async def test_usage_survives_invalid_final_json(self):
        report = await self.script([completion(TOOL), completion("bad", usage={"total_tokens": 7})])
        self.assertFalse(report["success"])
        self.assertEqual(report["calls"][1]["usage"], {"total_tokens": 7})

    async def test_live_without_auth_is_blocked_before_client_creation(self):
        with patch(MODULE + ".ChatCompletionsClient") as factory:
            with self.assertRaises(ValueError):
                await run_check(self.config, send=True)
        factory.assert_not_called()

    async def test_send_flag_uses_real_adapter_with_test_http_mock(self):
        with patch.dict(os.environ, {
            "AGENT_LLM_AUTH_HEADER": "X-Private-Smoke-Header", "AGENT_LLM_AUTH_VALUE": "engine-test-secret",
        }, clear=True):
            config = engine_smoke_config()
        report = await run_check(config, send=True, transport=self.scripted_transport([
            completion(TOOL), completion(self.final),
        ]))
        self.assertTrue(report["success"])
        self.assertEqual(report["mode"], "live")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0].headers["X-Private-Smoke-Header"], "engine-test-secret")
        serialized = json.dumps(report)
        for private in ("X-Private-Smoke-Header", "engine-test-secret", self.scenario.quote, self.scenario.company.name):
            self.assertNotIn(private, serialized)

    async def test_mock_mode_rejects_a_real_transport_override(self):
        transport = httpx.AsyncHTTPTransport()
        try:
            with self.assertRaises(ValueError):
                await run_check(self.config, transport=transport)
        finally:
            await transport.aclose()

    async def test_profile_cannot_exceed_output_or_timeout_ceiling(self):
        for config in (self.config.model_copy(update={"max_output_tokens": MAX_OUTPUT_TOKENS + 1}),
                       self.config.model_copy(update={"timeout_seconds": 121})):
            with self.subTest(config=config), self.assertRaises(ValueError):
                await run_check(config)

    async def test_custom_limit_is_sent_on_both_calls_including_live_mode_with_http_mock(self):
        for limit in (1, 1536, 4096, 8192):
            with self.subTest(limit=limit):
                self.requests.clear()
                config = self.config.model_copy(update={
                    "max_output_tokens": limit, "auth_header": "X-Smoke-Test",
                    "auth_value": SecretStr("engine-test-secret"),
                })
                report = await run_check(config, send=True, transport=self.scripted_transport([
                    completion(TOOL), completion(self.final),
                ]))
                self.assertTrue(report["success"])
                self.assertEqual(report["max_output_tokens_per_call"], limit)
                self.assertEqual(report["llm_calls"], 2)
                self.assertEqual(report["automatic_retries"], 0)
                self.assertEqual([json.loads(request.content)["max_tokens"] for request in self.requests], [limit, limit])
                self.assertTrue(report["checks"]["forced_finalization"])

    async def test_custom_limit_works_with_alternative_token_limit_field(self):
        config = self.config.model_copy(update={"max_output_tokens": 8192, "token_limit_field": "max_completion_tokens"})
        report = await run_check(config, transport=self.scripted_transport([completion(TOOL), completion(self.final)]))
        self.assertTrue(report["success"])
        for request in self.requests:
            body = json.loads(request.content)
            self.assertEqual(body["max_completion_tokens"], 8192)
            self.assertNotIn("max_tokens", body)

    async def test_invalid_programmatic_limits_are_blocked_before_client_creation(self):
        for limit in (0, -1, 8193, True, 1.5, "4096"):
            with self.subTest(limit=limit), patch(MODULE + ".ChatCompletionsClient") as factory:
                config = self.config.model_copy(update={"max_output_tokens": limit})
                with self.assertRaises(ValueError):
                    await run_check(config)
                factory.assert_not_called()

    async def test_larger_limit_does_not_allow_third_request_or_retry_on_truncation(self):
        class OverCallingEngine(AgentInsightEngine):
            async def generate_insight(self, *args):
                for _ in range(3):
                    await self.llm_client.generate("Test prompt")
        config = self.config.model_copy(update={"max_output_tokens": 8192})
        with patch(MODULE + ".AgentInsightEngine", OverCallingEngine):
            report = await run_check(config, transport=self.scripted_transport([completion(TOOL), completion(self.final)]))
        self.assertEqual(report["error"], "CallLimitExceeded")
        self.assertEqual(len(self.requests), 2)
        self.requests.clear()
        report = await run_check(config, transport=self.scripted_transport([
            completion(TOOL), completion("", reason="length", usage={"completion_tokens": 8192}),
        ]))
        self.assertEqual(report["error"], "LLMOutputLimitError")
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["automatic_retries"], 0)
        self.assertEqual([json.loads(request.content)["max_tokens"] for request in self.requests], [8192, 8192])


class EngineSmokeCommandTests(unittest.TestCase):
    def test_cli_accepts_explicit_limit_and_keeps_mock_as_default(self):
        for limit in (1, 4096, 8192):
            with self.subTest(limit=limit):
                output = io.StringIO()
                with patch.dict(os.environ, {}, clear=True), patch(
                    "httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("Network forbidden"),
                ), redirect_stdout(output):
                    code = main(["--max-output-tokens", str(limit)])
                report = json.loads(output.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(report["mode"], "mock")
                self.assertEqual(report["max_output_tokens_per_call"], limit)
                self.assertEqual(report["llm_calls"], 2)

    def test_invalid_cli_limits_fail_before_client_creation(self):
        for value in ("0", "-1", "8193", "1.5", "not-an-integer"):
            with self.subTest(value=value):
                output = io.StringIO()
                with patch.dict(os.environ, {}, clear=True), patch(MODULE + ".ChatCompletionsClient") as factory:
                    with redirect_stdout(output), redirect_stderr(io.StringIO()):
                        try:
                            code = main(["--send", "--max-output-tokens", value])
                        except SystemExit as error:
                            code = error.code
                self.assertEqual(code, 2)
                factory.assert_not_called()

    def test_config_validates_explicit_limit_independently_of_environment(self):
        with patch.dict(os.environ, {"AGENT_LLM_MAX_OUTPUT_TOKENS": "99999"}, clear=True):
            self.assertEqual(engine_smoke_config().max_output_tokens, 1536)
            self.assertEqual(engine_smoke_config(max_output_tokens=8192).max_output_tokens, 8192)
            for value in (0, -1, 8193, True, 1.5, "4096"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    engine_smoke_config(max_output_tokens=value)

    def test_cli_defaults_to_mock_and_prints_no_credentials_or_payloads(self):
        output = io.StringIO()
        with patch.dict(os.environ, {
            "AGENT_LLM_AUTH_HEADER": "X-Private-Smoke-Header", "AGENT_LLM_AUTH_VALUE": "engine-test-secret",
        }, clear=True), patch("httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("Network forbidden")):
            with redirect_stdout(output):
                code = main([])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "mock")
        self.assertEqual(report["max_output_tokens_per_call"], 1536)
        for private in ("engine-test-secret", "X-Private-Smoke-Header", "headers", "messages", "source_url"):
            self.assertNotIn(private, output.getvalue())

    def test_cli_send_without_auth_and_invalid_configuration_do_not_send(self):
        for arguments, env in ((["--send"], {}), ([], {"AGENT_LLM_AUTH_VALUE": "engine-test-secret"})):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with patch.dict(os.environ, env, clear=True), patch(MODULE + ".ChatCompletionsClient") as factory:
                    with redirect_stdout(output):
                        code = main(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue())["error"], "ConfigurationError")
                self.assertNotIn("engine-test-secret", output.getvalue())
                factory.assert_not_called()

    def test_cli_failure_returns_nonzero_without_exception_body(self):
        output = io.StringIO()
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="PRIVATE AUTH_SECRET"))
        with patch.dict(os.environ, {}, clear=True), patch(MODULE + ".mock_transport", return_value=transport):
            with redirect_stdout(output):
                code = main([])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "LLMHTTPError")
        self.assertNotIn("AUTH_SECRET", output.getvalue())

    def test_config_reads_credentials_only_from_environment_and_keeps_profile_bounded(self):
        with patch.dict(os.environ, {
            "LLM_API_KEY": "backend-placeholder", "AGENT_LLM_AUTH_HEADER": "X-Smoke-Test",
            "AGENT_LLM_AUTH_VALUE": "engine-test-secret", "AGENT_LLM_MAX_OUTPUT_TOKENS": "99999",
            "AGENT_LLM_TIMEOUT_SECONDS": "9999", "AGENT_LLM_TOKEN_LIMIT_FIELD": "max_completion_tokens",
        }, clear=True):
            config = engine_smoke_config()
        self.assertEqual(config.auth_value.get_secret_value(), "engine-test-secret")
        self.assertNotIn("engine-test-secret", repr(config))
        self.assertEqual(config.max_output_tokens, 1536)
        self.assertEqual(config.timeout_seconds, 30)
        self.assertEqual(config.token_limit_field, "max_completion_tokens")


if __name__ == "__main__":
    unittest.main()
