"""Offline checks for call accounting and per-job dependency ownership."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.agent.api_client import (
    ChatCompletionsClient, LLMHTTPError, LLMOutputLimitError, LLMTimeoutError, LLMTransportError,
)
from backend.app.agent.config import ChatCompletionsConfig
from backend.app.agent.runtime import ObservedLLMClient, RunTelemetry, RuntimeAgentEngine, RuntimeCallLimitError
from backend.app.agent.llm import FakeLLMClient
from backend.app.agent.tools import MockAnalyticsSource


class TinyClient:
    def __init__(self, *, usage=None, error=None, gate=None):
        self.usage = usage
        self.error = error
        self.gate = gate
        self.calls = 0
        self.last_usage = None
        self.last_finish_reason = None
        self.last_content_chars = None

    async def generate(self, prompt):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        self.last_usage = self.usage
        self.last_finish_reason = "length" if isinstance(self.error, LLMOutputLimitError) else "stop"
        self.last_content_chars = 2
        if self.error is not None:
            raise self.error
        return "OK"


class TinySource:
    async def search(self, query, *, company, event, limit):
        return []


class RunTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_safe_fields_and_known_totals_are_retained(self):
        raw = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11,
               "private": "PRIVATE_USAGE", "completion_tokens_details": {"reasoning_tokens": 2, "private": "PRIVATE_DETAILS"}}
        telemetry = RunTelemetry(max_calls=2)
        client = TinyClient(usage=raw)
        observed = ObservedLLMClient(client, telemetry=telemetry)
        self.assertEqual(await observed.generate("PRIVATE_PROMPT"), "OK")
        client.usage = {"prompt_tokens": 4, "completion_tokens": True, "total_tokens": -1}
        await observed.generate("OTHER_PROMPT")
        report = telemetry.build_report()
        self.assertEqual(report.totals.prompt_tokens, 12)
        self.assertIsNone(report.totals.completion_tokens)
        self.assertIsNone(report.totals.total_tokens)
        self.assertEqual(report.known_subtotal.completion_tokens, 3)
        self.assertEqual(report.known_subtotal.total_tokens, 11)
        self.assertFalse(report.usage_complete)
        self.assertEqual(report.calls[0].reasoning_tokens, 2)
        serialized = report.model_dump_json()
        self.assertNotIn("PRIVATE", serialized)
        self.assertIn("USAGE_INCOMPLETE", report.warnings)

    async def test_complete_usage_does_not_add_reasoning_tokens(self):
        telemetry = RunTelemetry(max_calls=1)
        observed = ObservedLLMClient(TinyClient(usage={
            "prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }), telemetry=telemetry)
        await observed.generate("prompt")
        report = telemetry.build_report()
        self.assertTrue(report.usage_complete)
        self.assertEqual(report.totals.total_tokens, 11)
        self.assertEqual(report.warnings, [])

    async def test_hard_budget_is_consumed_before_await(self):
        gate = asyncio.Event()
        client = TinyClient(gate=gate)
        telemetry = RunTelemetry(max_calls=2)
        observed = ObservedLLMClient(client, telemetry=telemetry)
        tasks = [asyncio.create_task(observed.generate("prompt")) for _ in range(5)]
        await asyncio.sleep(0)
        self.assertEqual(len(telemetry.calls), 2)
        self.assertEqual(client.calls, 1)  # Calls on one adapter are serialized.
        gate.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(sum(isinstance(item, RuntimeCallLimitError) for item in outcomes), 3)
        self.assertEqual(client.calls, 2)
        self.assertIn("CALL_LIMIT_EXCEEDED", telemetry.build_report().error_codes)

    async def test_failure_keeps_usage_without_copying_exception_text(self):
        original = LLMOutputLimitError("PRIVATE_EXCEPTION")
        telemetry = RunTelemetry(max_calls=1)
        observed = ObservedLLMClient(TinyClient(usage={"completion_tokens": 16}, error=original), telemetry=telemetry)
        with self.assertRaises(LLMOutputLimitError) as caught:
            await observed.generate("PRIVATE_PROMPT")
        self.assertIs(caught.exception, original)
        report = telemetry.build_report()
        self.assertEqual(report.calls[0].usage.completion_tokens, 16)
        self.assertEqual(report.calls[0].finish_reason, "length")
        self.assertEqual(report.calls[0].error_code, "LLM_OUTPUT_LIMIT")
        self.assertNotIn("PRIVATE", report.model_dump_json())

    async def test_timeout_and_http_error_are_fixed_metadata(self):
        for error, code, kind, status in (
            (LLMTimeoutError("PRIVATE", timeout_kind="PRIVATE_KIND"), "LLM_TIMEOUT", "unknown_timeout", None),
            (TimeoutError("PRIVATE"), "LLM_TIMEOUT", "unknown_timeout", None),
            (LLMHTTPError(429), "LLM_HTTP_ERROR", None, 429),
            (RuntimeError("PRIVATE"), "LLM_CALL_FAILED", None, None),
        ):
            with self.subTest(code=code):
                telemetry = RunTelemetry(max_calls=1)
                observed = ObservedLLMClient(TinyClient(error=error), telemetry=telemetry)
                with self.assertRaises(type(error)):
                    await observed.generate("prompt")
                record = telemetry.build_report().calls[0]
                self.assertEqual(record.error_code, code)
                self.assertEqual(record.timeout_kind, kind)
                self.assertEqual(record.http_status, status)
                self.assertNotIn("PRIVATE", record.model_dump_json())

    async def test_cancellation_propagates_and_deadline_can_be_classified(self):
        telemetry = RunTelemetry(max_calls=1)
        client = TinyClient(gate=asyncio.Event())
        observed = ObservedLLMClient(client, telemetry=telemetry)
        task = asyncio.create_task(observed.generate("prompt"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        report = telemetry.build_report()
        self.assertEqual(report.calls[0].status, "cancelled")
        self.assertIsNone(report.calls[0].usage)
        telemetry.record_deadline("run_deadline")
        refined = telemetry.build_report()
        self.assertEqual(refined.calls[0].timeout_kind, "run_deadline")
        self.assertEqual(refined.calls[0].error_code, "RUN_TIMEOUT")
        self.assertEqual(report.calls[0].error_code, "CANCELLED")  # Snapshot is independent.

    async def test_waiting_cancellation_does_not_capture_previous_call_usage(self):
        gate = asyncio.Event()
        client = TinyClient(usage={"total_tokens": 7}, gate=gate)
        telemetry = RunTelemetry(max_calls=2)
        observed = ObservedLLMClient(client, telemetry=telemetry)
        first = asyncio.create_task(observed.generate("first"))
        second = asyncio.create_task(observed.generate("second"))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        gate.set()
        await first
        self.assertEqual(client.calls, 1)
        self.assertIsNone(telemetry.build_report().calls[1].usage)

    async def test_concurrent_runs_keep_clients_and_usage_separate(self):
        async def one_run(tokens):
            telemetry = RunTelemetry(max_calls=1)
            client = ObservedLLMClient(TinyClient(usage={"total_tokens": tokens}), telemetry=telemetry)
            await client.generate("prompt")
            return telemetry.build_report()

        first, second = await asyncio.gather(one_run(7), one_run(19))
        self.assertEqual(first.totals.total_tokens, 7)
        self.assertEqual(second.totals.total_tokens, 19)
        self.assertIsNot(first.calls[0], second.calls[0])

    async def test_failed_attempt_does_not_reuse_mutable_metadata_from_previous_success(self):
        class FailsAfterSuccess(TinyClient):
            async def generate(self, prompt):
                if self.calls:
                    self.calls += 1
                    raise LLMTransportError("PRIVATE_FAILURE")
                return await super().generate(prompt)

        telemetry = RunTelemetry(max_calls=2)
        observed = ObservedLLMClient(FailsAfterSuccess(usage={
            "prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
        }), telemetry=telemetry)
        await observed.generate("first")
        with self.assertRaises(LLMTransportError):
            await observed.generate("second")
        report = telemetry.build_report()
        self.assertIsNone(report.calls[1].usage)
        self.assertIsNone(report.calls[1].finish_reason)
        self.assertIsNone(report.calls[1].content_chars)
        self.assertFalse(report.usage_complete)
        self.assertIsNone(report.totals.total_tokens)
        self.assertEqual(report.known_subtotal.total_tokens, 13)
        self.assertNotIn("PRIVATE", report.model_dump_json())

    async def test_readonly_stale_metadata_is_unknown_without_a_fresh_attempt_marker(self):
        class ReadonlyClient:
            @property
            def last_usage(self):
                return {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}

            @property
            def last_finish_reason(self):
                return "stop"

            @property
            def last_content_chars(self):
                return 2

            async def generate(self, prompt):
                return "OK"

        telemetry = RunTelemetry(max_calls=2)
        observed = ObservedLLMClient(ReadonlyClient(), telemetry=telemetry)
        await observed.generate("first")
        await observed.generate("second")
        report = telemetry.build_report()
        self.assertTrue(all(call.usage is None for call in report.calls))
        self.assertTrue(all(call.finish_reason is None for call in report.calls))
        self.assertTrue(all(call.content_chars is None for call in report.calls))
        self.assertIsNone(report.known_subtotal.total_tokens)
        self.assertFalse(report.usage_complete)

    async def test_readonly_metadata_requires_a_marker_changed_by_the_current_attempt(self):
        class MarkedClient:
            def __init__(self):
                self.attempt = 0

            @property
            def last_attempt_id(self):
                return self.attempt

            @property
            def last_usage(self):
                return {"total_tokens": 7 * self.attempt}

            async def generate(self, prompt):
                if self.attempt:
                    raise LLMTransportError("PRIVATE_FAILURE")
                self.attempt += 1
                return "OK"

        telemetry = RunTelemetry(max_calls=2)
        observed = ObservedLLMClient(MarkedClient(), telemetry=telemetry)
        await observed.generate("first")
        with self.assertRaises(LLMTransportError):
            await observed.generate("second")
        report = telemetry.build_report()
        self.assertEqual(report.calls[0].usage.total_tokens, 7)
        self.assertIsNone(report.calls[1].usage)
        self.assertEqual(report.known_subtotal.total_tokens, 7)
        self.assertIsNone(report.totals.total_tokens)
        self.assertNotIn("last_attempt_id", report.model_dump_json())

    async def test_malformed_optional_properties_do_not_replace_a_result_or_cancellation(self):
        class BrokenMetadataClient:
            @property
            def last_usage(self):
                raise RuntimeError("PRIVATE_METADATA")

            @property
            def last_finish_reason(self):
                raise asyncio.CancelledError("PRIVATE_METADATA_CANCEL")

            @property
            def last_content_chars(self):
                return object()

            async def generate(self, prompt):
                if prompt == "cancel":
                    raise original
                return "OK"

        original = asyncio.CancelledError("PRIVATE_ORIGINAL_CANCEL")
        telemetry = RunTelemetry(max_calls=2)
        observed = ObservedLLMClient(BrokenMetadataClient(), telemetry=telemetry)
        self.assertEqual(await observed.generate("success"), "OK")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await observed.generate("cancel")
        self.assertIs(caught.exception, original)
        report = telemetry.build_report()
        self.assertEqual([call.status for call in report.calls], ["ok", "cancelled"])
        self.assertTrue(all(call.usage is None for call in report.calls))
        self.assertNotIn("PRIVATE", report.model_dump_json())


class RuntimeFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_factories_are_called_for_each_job_and_analysis_is_delegated(self):
        clients, sources = [], []

        def make_client():
            client = TinyClient()
            clients.append(client)
            return client

        def make_source():
            source = TinySource()
            sources.append(source)
            return source

        class Engine:
            def __init__(self, client, *, analytics_source, config):
                self.client = client
                self.source = analytics_source

            async def generate_insight(self, *args):
                await asyncio.sleep(0)
                return (self.client, self.source)

            async def generate_analysis(self, *args):
                return "analysis"

        config = SimpleNamespace(analysis_mode="grounded")
        facade = RuntimeAgentEngine(client_factory=make_client, source_factory=make_source, config=config)
        with patch("backend.app.agent.orchestrator.AgentInsightEngine", Engine):
            first, second = await asyncio.gather(
                facade.generate_insight(None, None, [], None),
                facade.generate_insight(None, None, [], None),
            )
            self.assertEqual(await facade.generate_analysis(None, None, [], None), "analysis")
        self.assertEqual(len(clients), 3)
        self.assertEqual(len(sources), 3)
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[1], second[1])
        self.assertFalse(hasattr(facade, "last_report"))

    async def test_compatibility_mode_is_rejected_before_factory_execution(self):
        def forbidden():
            raise AssertionError("Factory must not be called")

        with self.assertRaisesRegex(ValueError, "grounded"):
            RuntimeAgentEngine(client_factory=forbidden, source_factory=forbidden,
                               config=SimpleNamespace(analysis_mode="compatible"))

    async def test_runtime_rejects_offline_dependencies_unless_enabled(self):
        options = dict(client_factory=FakeLLMClient, source_factory=MockAnalyticsSource,
                       config=SimpleNamespace(analysis_mode="grounded"))
        facade = RuntimeAgentEngine(**options)
        with self.assertRaisesRegex(ValueError, "production dependencies"):
            await facade.generate_insight(None, None, [], None)
        enabled = RuntimeAgentEngine(**options, allow_test_dependencies=True)
        self.assertIsInstance(enabled._new_engine().llm_client, FakeLLMClient)

    async def test_none_or_invalid_factories_are_rejected_before_dependency_creation(self):
        for key, value in (("client_factory", None), ("source_factory", None),
                           ("client_factory", object()), ("source_factory", object())):
            with self.subTest(key=key, value_type=type(value).__name__):
                options = dict(client_factory=TinyClient, source_factory=TinySource)
                options[key] = value
                with self.assertRaisesRegex(ValueError, "factories"):
                    RuntimeAgentEngine(**options)

    async def test_async_factory_is_not_silently_treated_as_a_dependency(self):
        async def async_factory():
            raise AssertionError("Factory body must not execute")

        with self.assertRaisesRegex(ValueError, "factories"):
            RuntimeAgentEngine(client_factory=async_factory, source_factory=TinySource)

    async def test_none_missing_or_synchronous_protocol_methods_fail_before_engine_creation(self):
        class SyncClient:
            def generate(self, prompt):
                raise AssertionError("Synchronous generate must not execute")

        class SyncSource:
            def search(self, *args, **kwargs):
                raise AssertionError("Synchronous search must not execute")

        cases = [
            (None, TinySource()), (TinyClient(), None),
            (object(), TinySource()), (TinyClient(), object()),
            (SyncClient(), TinySource()), (TinyClient(), SyncSource()),
        ]
        for client, source in cases:
            with self.subTest(client=type(client).__name__, source=type(source).__name__):
                facade = RuntimeAgentEngine(client_factory=lambda: client, source_factory=lambda: source)
                with patch("backend.app.agent.orchestrator.AgentInsightEngine") as constructor:
                    with self.assertRaisesRegex(ValueError, "async generate and search"):
                        facade._new_engine()
                    constructor.assert_not_called()

    def chat_client(self):
        # No generate() call: this factory never creates an HTTP connection.
        return ChatCompletionsClient(ChatCompletionsConfig(
            api_url="https://synthetic.example.test/chat", model="synthetic-test-model",
        ))

    async def test_production_http_adapter_requires_the_exact_explicit_approved_model(self):
        for approved in (None, set(), {"another-synthetic-test-model"}):
            with self.subTest(approved=approved):
                facade = RuntimeAgentEngine(client_factory=self.chat_client, source_factory=TinySource,
                                            approved_model_ids=approved)
                with self.assertRaisesRegex(ValueError, "approved model"):
                    facade._new_engine()
        facade = RuntimeAgentEngine(client_factory=self.chat_client, source_factory=TinySource,
                                    approved_model_ids={"synthetic-test-model"})
        self.assertEqual(facade._new_engine().llm_client.config.model, "synthetic-test-model")

    async def test_only_explicit_test_dependencies_bypass_the_model_policy(self):
        facade = RuntimeAgentEngine(client_factory=self.chat_client, source_factory=TinySource,
                                    allow_test_dependencies=True)
        self.assertIsInstance(facade._new_engine().llm_client, ChatCompletionsClient)
        for approved in ("synthetic-test-model", {""}, {None}, [17]):
            with self.subTest(approved=approved):
                with self.assertRaisesRegex(ValueError, "approved_model_ids"):
                    RuntimeAgentEngine(client_factory=TinyClient, source_factory=TinySource,
                                       approved_model_ids=approved)


if __name__ == "__main__":
    unittest.main()
