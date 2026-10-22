"""Offline checks of timeout ownership, cancellation propagation and safe timing."""

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from pydantic import SecretStr

from backend.app.agent import AgentConfig, RecordingTracer
from backend.app.agent.engine_smoke_check import (
    BoundedSmokeClient, engine_smoke_config, main, make_scenario, mock_final_result, run_check,
)
from backend.app.agent.models import AgentLLMError, AgentLLMTimeoutError
from backend.app.agent.orchestrator import AgentInsightEngine


MODULE = "backend.app.agent.engine_smoke_check"
TOOL = {"action": "tool", "tool": "get_bank_analytics", "arguments": {"limit": 1}}
USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def completion(payload):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}],
        "usage": USAGE,
    })


class NestedTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.config = engine_smoke_config(max_output_tokens=4096)
        self.scenario = make_scenario()
        self.requests = []
        self.second_started = asyncio.Event()
        self.second_cancelled = asyncio.Event()

        def observe_client(adapter, scenario):
            self.client = BoundedSmokeClient(adapter, scenario)
            return self.client

        for patcher in (
            patch(MODULE + ".make_scenario", return_value=self.scenario),
            patch(MODULE + ".BoundedSmokeClient", side_effect=observe_client),
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def slow_finalization(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return completion(TOOL)
        self.second_started.set()
        try:
            await asyncio.Future()  # Deliberately pending until a deadline/caller cancels it.
        finally:
            self.second_cancelled.set()

    async def run_slow(self, *, http, llm, run):
        return await run_check(
            self.config.model_copy(update={"timeout_seconds": http}),
            llm_timeout_seconds=llm, run_timeout_seconds=run,
            transport=httpx.MockTransport(self.slow_finalization),
        )

    def assert_failure(self, report, error, kind, status):
        self.assertFalse(report["success"])
        self.assertEqual(report["error"], error)
        self.assertEqual(report["timeout_kind"], kind)
        self.assertEqual(report["calls"][1]["timeout_kind"], kind)
        self.assertEqual(report["calls"][1]["status"], status)
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(report["automatic_retries"], 0)
        self.assertEqual(report["max_output_tokens_per_call"], 4096)
        self.assertEqual(report["calls"][0]["usage"], USAGE)
        self.assertIsNone(report["calls"][1]["usage"])
        self.assertFalse(report["usage_complete"])
        self.assertTrue(all(value is None for value in report["total_usage"].values()))
        self.assertTrue(report["checks"]["forced_finalization"])
        self.assertFalse(report["checks"]["final_json_valid"])
        self.assertTrue(self.second_cancelled.is_set())
        for call in report["calls"]:
            self.assertGreaterEqual(call["elapsed_ms"], 0)
            self.assertGreaterEqual(report["elapsed_ms"], call["elapsed_ms"])
        self.assertEqual([json.loads(request.content)["max_tokens"] for request in self.requests], [4096, 4096])

    async def test_http_deadline_before_llm_deadline_is_an_api_timeout(self):
        report = await self.run_slow(http=0.05, llm=0.5, run=1)
        self.assert_failure(report, "LLMTimeoutError", "http_deadline", "error")
        self.assertIsNone(report["calls"][1]["cancellation_kind"])

    async def test_llm_deadline_before_http_deadline_identifies_cancel_owner(self):
        report = await self.run_slow(http=0.5, llm=0.05, run=1)
        self.assert_failure(report, "LLMCallTimeout", "llm_deadline", "cancelled")
        self.assertEqual(report["calls"][1]["cancellation_kind"], "llm_deadline")

    async def test_equal_deadlines_reproduce_original_case_without_generic_error(self):
        report = await self.run_slow(http=0.05, llm=0.05, run=1)
        self.assert_failure(report, "LLMCallTimeout", "llm_deadline", "cancelled")

    async def test_scenario_deadline_cancels_second_call_with_its_own_code(self):
        report = await self.run_slow(http=0.5, llm=0.4, run=0.1)
        self.assert_failure(report, "ScenarioTimeout", "scenario_deadline", "cancelled")
        self.assertEqual(report["calls"][1]["cancellation_kind"], "scenario_deadline")

    async def test_scenario_timeout_during_tool_does_not_relabel_completed_call(self):
        async def slow_tool(*args, **kwargs):
            await asyncio.Future()

        with patch(MODULE + ".ObservedMockAnalyticsSource.search", side_effect=slow_tool):
            report = await self.run_slow(http=0.5, llm=0.4, run=0.1)
        self.assertEqual(report["error"], "ScenarioTimeout")
        self.assertEqual(report["timeout_kind"], "scenario_deadline")
        self.assertEqual(report["llm_calls"], 1)
        self.assertEqual(report["calls"][0]["status"], "ok")
        self.assertIsNone(report["calls"][0]["timeout_kind"])
        self.assertIsNone(report["calls"][0]["cancellation_kind"])

    async def test_scenario_timeout_during_error_tracing_keeps_overall_cause(self):
        async def delayed_error_trace(step_name, description):
            if step_name == "Ошибка LLM":
                await asyncio.Future()

        with patch(MODULE + ".RecordingTracer.emit_step", side_effect=delayed_error_trace):
            report = await run_check(
                self.config, run_timeout_seconds=0.1,
                transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            )
        self.assertEqual(report["error"], "ScenarioTimeout")
        self.assertEqual(report["timeout_kind"], "scenario_deadline")
        self.assertEqual(report["llm_calls"], 1)
        self.assertEqual(report["calls"][0]["error"], "LLMHTTPError")
        self.assertIsNone(report["calls"][0]["timeout_kind"])
        self.assertIsNone(report["calls"][0]["cancellation_kind"])

    async def test_external_cancel_propagates_and_keeps_safe_call_timing(self):
        task = asyncio.create_task(self.run_slow(http=1, llm=1.5, run=2))
        try:
            await asyncio.wait_for(self.second_started.wait(), timeout=1)
            task.cancel("PRIVATE_CANCEL_REASON")
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertTrue(self.second_cancelled.is_set())
        self.assertEqual(len(self.requests), 2)
        call = self.client.calls[1]
        self.assertEqual(call["status"], "cancelled")
        self.assertEqual(call["cancellation_kind"], "external_or_unknown")
        self.assertIsNone(call["timeout_kind"])
        self.assertIsNone(call["usage"])
        self.assertNotIn("error", call)
        self.assertGreaterEqual(call["elapsed_ms"], 0)
        self.assertNotIn("PRIVATE_CANCEL_REASON", json.dumps(self.client.calls))

    async def test_external_deadline_is_not_converted_to_an_engine_timeout(self):
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.1):
                await self.run_slow(http=1, llm=1.5, run=2)
        self.assertTrue(self.second_cancelled.is_set())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.client.calls[1]["cancellation_kind"], "external_or_unknown")
        self.assertIsNone(self.client.calls[1]["timeout_kind"])

    async def test_http_read_timeout_and_other_failures_have_safe_elapsed_metadata(self):
        for failure, error, kind in (
            (httpx.ReadTimeout("PRIVATE_BODY AUTH_SECRET"), "LLMTimeoutError", "http_read"),
            (TimeoutError("PRIVATE_BODY AUTH_SECRET"), "LLMTimeoutError", "unknown_timeout"),
            (httpx.ConnectError("PRIVATE_BODY AUTH_SECRET"), "LLMTransportError", None),
            (RuntimeError("PRIVATE_BODY AUTH_SECRET"), "ClientError", None),
            (httpx.Response(503, text="PRIVATE_BODY AUTH_SECRET"), "LLMHTTPError", None),
            (httpx.Response(200, text="PRIVATE_BODY AUTH_SECRET"), "LLMResponseError", None),
            (completion("INVALID PRIVATE_BODY AUTH_SECRET"), "InvalidModelResponse", None),
        ):
            with self.subTest(error=error, kind=kind):
                self.requests.clear()

                def handler(request):
                    self.requests.append(request)
                    if len(self.requests) == 1:
                        return completion(TOOL)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                report = await run_check(self.config, transport=httpx.MockTransport(handler))
                self.assertFalse(report["success"])
                self.assertEqual(report["error"], error)
                self.assertEqual(report["timeout_kind"], kind)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(report["calls"][1]["timeout_kind"], kind)
                self.assertIsNone(report["calls"][1]["cancellation_kind"])
                self.assertGreaterEqual(report["calls"][1]["elapsed_ms"], 0)
                self.assertGreaterEqual(report["elapsed_ms"], report["calls"][1]["elapsed_ms"])
                for secret in ("PRIVATE_BODY", "AUTH_SECRET", self.scenario.quote):
                    self.assertNotIn(secret, json.dumps(report))

    async def test_client_timeout_error_is_not_mistaken_for_an_owned_deadline(self):
        client = AsyncMock()
        client.generate.side_effect = TimeoutError("PRIVATE_ERROR")
        engine = AgentInsightEngine(client, config=AgentConfig())
        tracer = RecordingTracer()
        with self.assertRaises(AgentLLMError) as caught:
            await engine.generate_insight(self.scenario.company, self.scenario.event, [], tracer)
        self.assertNotIsInstance(caught.exception, AgentLLMTimeoutError)
        self.assertNotIn("PRIVATE_ERROR", str(caught.exception) + str(tracer.steps))
        client.generate.assert_awaited_once()

    async def test_live_profile_uses_requested_timeouts_with_http_mock(self):
        config = self.config.model_copy(update={
            "timeout_seconds": 120, "auth_header": "X-Test-Key", "auth_value": SecretStr("AUTH_SECRET"),
        })

        def handler(request):
            self.requests.append(request)
            return completion(TOOL if len(self.requests) == 1 else mock_final_result(self.scenario))

        report = await run_check(
            config, send=True, transport=httpx.MockTransport(handler),
            llm_timeout_seconds=130, run_timeout_seconds=300,
        )
        self.assertTrue(report["success"])
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["timeouts_seconds"], {"http": 120, "llm": 130, "run": 300})
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["automatic_retries"], 0)
        for request, call in zip(self.requests, report["calls"]):
            self.assertTrue(all(value == 120 for value in request.extensions["timeout"].values()))
            self.assertEqual(json.loads(request.content)["max_tokens"], 4096)
            self.assertGreaterEqual(call["elapsed_ms"], 0)
            self.assertIsNone(call["timeout_kind"])
        self.assertNotIn("AUTH_SECRET", json.dumps(report))

    async def test_invalid_timeouts_are_rejected_before_creating_client(self):
        for name, maximum in (("http", 120), ("llm", 130), ("run", 300)):
            for value in (0, -1, True, "30", float("inf"), float("nan"), maximum + 1):
                with self.subTest(name=name, value=value), patch(MODULE + ".ChatCompletionsClient") as factory:
                    config = self.config
                    kwargs = {}
                    if name == "http":
                        config = config.model_copy(update={"timeout_seconds": value})
                    else:
                        kwargs[name + "_timeout_seconds"] = value
                    with self.assertRaises(ValueError):
                        await run_check(config, **kwargs)
                    factory.assert_not_called()


class TimeoutCommandTests(unittest.TestCase):
    def test_cli_selects_live_profile_explicitly_without_sending(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch(
            "httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("Network forbidden"),
        ), redirect_stdout(output):
            code = main([
                "--http-timeout-seconds", "120", "--llm-timeout-seconds", "130",
                "--run-timeout-seconds", "300", "--max-output-tokens", "4096",
            ])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "mock")
        self.assertEqual(report["timeouts_seconds"], {"http": 120, "llm": 130, "run": 300})
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["max_output_tokens_per_call"], 4096)
        self.assertGreaterEqual(report["elapsed_ms"], 0)

    def test_cli_defaults_and_environment_do_not_silently_raise_timeouts(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENT_LLM_TIMEOUT_SECONDS": "9999"}, clear=True), patch(
            "httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("Network forbidden"),
        ), redirect_stdout(output):
            code = main([])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["timeouts_seconds"], {"http": 30, "llm": 40, "run": 90})
        self.assertEqual(report["max_output_tokens_per_call"], 1536)

    def test_invalid_cli_timeouts_never_create_a_client(self):
        for flag, maximum in (("http", 120), ("llm", 130), ("run", 300)):
            for value in ("0", "-1", "nan", "inf", "not-a-number", str(maximum + 1)):
                with self.subTest(flag=flag, value=value), patch.dict(os.environ, {}, clear=True), patch(
                    MODULE + ".ChatCompletionsClient",
                ) as factory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        code = main([f"--{flag}-timeout-seconds={value}"])
                    except SystemExit as error:
                        code = error.code
                    self.assertEqual(code, 2)
                    factory.assert_not_called()

    def test_keyboard_interrupt_has_safe_timing_and_keeps_interrupt_exit_code(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch(
            MODULE + ".run_check", new=MagicMock(return_value=None),
        ), patch(MODULE + ".asyncio.run", side_effect=KeyboardInterrupt), redirect_stdout(output):
            code = main([])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 130)
        self.assertEqual(report["error"], "Interrupted")
        self.assertEqual(report["cancellation_kind"], "keyboard_interrupt")
        self.assertGreaterEqual(report["elapsed_ms"], 0)


if __name__ == "__main__":
    unittest.main()
