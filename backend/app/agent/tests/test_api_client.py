import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from backend.app.agent import AgentInsightEngine, FakeLLMClient, LLMClientProtocol, RecordingTracer
from backend.app.agent.api_client import (
    ChatCompletionsClient,
    LLMHTTPError,
    LLMOutputLimitError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
)
from backend.app.agent.config import ChatCompletionsConfig
from backend.app.agent.models import AgentResponseError, BANK_VIEW_UNAVAILABLE
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse


def completion(content='{"ok": true}', finish_reason="stop", **extra):
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        **extra,
    }


class ChatCompletionsConfigTests(unittest.TestCase):
    def test_settings_are_read_from_environment(self):
        env = {
            "AGENT_LLM_API_URL": "https://llm.invalid/custom/complete",
            "AGENT_LLM_MODEL": "test-model",
            "AGENT_LLM_AUTH_HEADER": "X-Test-Key",
            "AGENT_LLM_AUTH_VALUE": "test-secret",
            "AGENT_LLM_TIMEOUT_SECONDS": "2.5",
            "AGENT_LLM_MAX_OUTPUT_TOKENS": "64",
            "AGENT_LLM_TOKEN_LIMIT_FIELD": "max_completion_tokens",
        }
        with patch.dict(os.environ, env, clear=True):
            config = ChatCompletionsConfig.from_env()
        self.assertEqual(config.api_url, env["AGENT_LLM_API_URL"])
        self.assertEqual(config.model, "test-model")
        self.assertEqual(config.auth_header, "X-Test-Key")
        self.assertEqual(config.auth_value.get_secret_value(), "test-secret")
        self.assertEqual(config.timeout_seconds, 2.5)
        self.assertEqual(config.max_output_tokens, 64)
        self.assertEqual(config.token_limit_field, "max_completion_tokens")

    def test_endpoint_and_model_have_no_inferred_defaults(self):
        for env in (
            {},
            {"AGENT_LLM_API_URL": "https://llm.invalid/complete"},
            {"AGENT_LLM_MODEL": "test-model"},
            {"LLM_API_BASE_URL": "https://api.example.com/v1", "LLM_API_KEY": "mock-key"},
        ):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValidationError):
                    ChatCompletionsConfig.from_env()

    def test_invalid_configuration_is_rejected(self):
        for override in (
            {"api_url": "relative/path"},
            {"api_url": "ftp://llm.invalid/complete"},
            {"api_url": "https://user:password@llm.invalid/complete"},
            {"api_url": "https://llm.invalid:99999/complete"},
            {"model": " "},
            {"timeout_seconds": 0},
            {"timeout_seconds": -1},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": float("nan")},
            {"max_output_tokens": 0},
            {"max_output_tokens": -1},
            {"token_limit_field": "unknown_field"},
        ):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                ChatCompletionsConfig(
                    **{"api_url": "https://llm.invalid/complete", "model": "test-model", **override}
                )

    def test_auth_requires_a_complete_valid_header_pair(self):
        for auth in (
            {"auth_header": "X-Test-Key"},
            {"auth_value": "test-secret"},
            {"auth_header": "Bad Header", "auth_value": "test-secret"},
            {"auth_header": "X-Test-Key", "auth_value": ""},
            {"auth_header": "X-Test-Key", "auth_value": "test\r\nInjected: value"},
        ):
            with self.subTest(auth=auth), self.assertRaises(ValidationError):
                ChatCompletionsConfig(api_url="https://llm.invalid/complete", model="test-model", **auth)

    def test_secret_is_hidden_in_repr_and_validation_message(self):
        config = ChatCompletionsConfig(
            api_url="https://llm.invalid/complete",
            model="test-model",
            auth_header="X-Test-Key",
            auth_value="test-secret",
        )
        self.assertNotIn("test-secret", repr(config))
        with self.assertRaises(ValidationError) as caught:
            ChatCompletionsConfig(
                api_url="https://llm.invalid/complete",
                model="test-model",
                auth_header="X-Test-Key",
                auth_value="test-secret",
                timeout_seconds=0,
            )
        self.assertNotIn("test-secret", str(caught.exception))


class ChatCompletionsClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config_values = {
            "api_url": "https://llm.invalid/custom/complete",
            "model": "test-model",
            "auth_header": "X-Test-Key",
            "auth_value": "test-secret",
            "timeout_seconds": 2.5,
            "max_output_tokens": 128,
        }
        self.requests = []

    def make_client(self, handler, **overrides):
        def record(request):
            self.requests.append(request)
            return handler(request)

        return ChatCompletionsClient(
            ChatCompletionsConfig(**{**self.config_values, **overrides}),
            transport=httpx.MockTransport(record),
        )

    async def test_request_and_raw_usage(self):
        usage = {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
        client = self.make_client(lambda request: httpx.Response(200, json=completion(usage=usage)))

        text = await client.generate("Тестовый промпт")

        self.assertIsInstance(client, LLMClientProtocol)
        self.assertIsInstance(FakeLLMClient(), LLMClientProtocol)
        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(client.last_usage, usage)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), self.config_values["api_url"])
        self.assertEqual(request.headers["X-Test-Key"], "test-secret")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(
            json.loads(request.content),
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "Тестовый промпт"}],
                "stream": False,
                "max_tokens": 128,
            },
        )
        self.assertTrue(all(value == 2.5 for value in request.extensions["timeout"].values()))

    async def test_alternative_token_limit_field(self):
        client = self.make_client(
            lambda request: httpx.Response(200, json=completion()),
            token_limit_field="max_completion_tokens",
        )
        await client.generate("test")
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", payload)

    async def test_no_auth_header_is_invented(self):
        client = self.make_client(
            lambda request: httpx.Response(200, json=completion()),
            auth_header=None,
            auth_value=None,
        )
        await client.generate("test")
        self.assertNotIn("Authorization", self.requests[0].headers)
        self.assertNotIn("X-Test-Key", self.requests[0].headers)

    async def test_absent_null_empty_and_partial_usage_are_not_estimated(self):
        for extra, expected in (
            ({}, None),
            ({"usage": None}, None),
            ({"usage": {}}, {}),
            ({"usage": {"prompt_tokens": 0}}, {"prompt_tokens": 0}),
            ({"usage": {"completion_tokens": 7}}, {"completion_tokens": 7}),
        ):
            with self.subTest(extra=extra):
                client = self.make_client(
                    lambda request: httpx.Response(200, json=completion(**extra))
                )
                client.last_usage = {"total_tokens": 999}
                await client.generate("test")
                self.assertEqual(client.last_usage, expected)

    async def test_http_errors_and_redirects_are_not_retried_or_exposed(self):
        for status in (302, 400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.requests.clear()
                client = self.make_client(
                    lambda request: httpx.Response(
                        status,
                        text="test-secret: private response body",
                        headers={"Location": "https://other.invalid/complete"},
                    )
                )
                client.last_usage = {"total_tokens": 999}
                with self.assertRaises(LLMHTTPError) as caught:
                    await client.generate("private prompt")
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(len(self.requests), 1)
                self.assertIsNone(client.last_usage)
                self.assertNotIn("test-secret", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))

    async def test_network_error_is_reported_without_retry(self):
        def fail(request):
            raise httpx.ConnectError("test-secret private prompt", request=request)

        client = self.make_client(fail)
        with self.assertRaises(LLMTransportError) as caught:
            await client.generate("private prompt")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("test-secret", str(caught.exception))

    async def test_http_timeouts_are_reported_without_retry(self):
        for error_type, kind in (
            (httpx.ConnectTimeout, "http_connect"), (httpx.ReadTimeout, "http_read"),
            (httpx.WriteTimeout, "http_write"), (httpx.PoolTimeout, "http_pool"),
            (httpx.TimeoutException, "http_timeout"),
        ):
            with self.subTest(error_type=error_type):
                self.requests.clear()

                def fail(request):
                    raise error_type("test-secret", request=request)

                client = self.make_client(fail)
                with self.assertRaises(LLMTimeoutError) as caught:
                    await client.generate("test")
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(caught.exception.timeout_kind, kind)
                self.assertNotIn("test-secret", str(caught.exception))

    async def test_total_deadline_is_enforced(self):
        async def slow_response(request):
            self.requests.append(request)
            await asyncio.sleep(1)
            return httpx.Response(200, json=completion())

        client = ChatCompletionsClient(
            ChatCompletionsConfig(**{**self.config_values, "timeout_seconds": 0.01}),
            transport=httpx.MockTransport(slow_response),
        )
        with self.assertRaises(LLMTimeoutError) as caught:
            await client.generate("test")
        self.assertEqual(caught.exception.timeout_kind, "http_deadline")
        self.assertEqual(len(self.requests), 1)

    async def test_non_json_http_response_is_rejected(self):
        client = self.make_client(lambda request: httpx.Response(200, text="<html>private</html>"))
        with self.assertRaises(LLMResponseError) as caught:
            await client.generate("test")
        self.assertNotIn("private", str(caught.exception))

    async def test_invalid_response_envelopes_are_rejected(self):
        for payload in (
            [],
            {},
            {"choices": []},
            {"choices": [None]},
            {"choices": [{"finish_reason": "stop"}]},
            completion(content=None),
            completion(content=""),
            completion(content=" "),
            completion(content=[{"text": "unsupported"}]),
            completion(finish_reason=None),
            completion(finish_reason="content_filter"),
            completion(finish_reason="tool_calls"),
            completion(usage=[]),
            {"choices": [{"finish_reason": "stop", "message": {"refusal": "private"}}]},
        ):
            with self.subTest(payload=payload):
                client = self.make_client(lambda request: httpx.Response(200, json=payload))
                with self.assertRaises(LLMResponseError):
                    await client.generate("test")

    async def test_truncated_response_keeps_usage_and_raises(self):
        usage = {"completion_tokens": 128}
        client = self.make_client(
            lambda request: httpx.Response(
                200, json=completion(content='{"incomplete":', finish_reason="length", usage=usage)
            )
        )
        with self.assertRaises(LLMOutputLimitError):
            await client.generate("test")
        self.assertEqual(client.last_usage, usage)
        self.assertEqual(len(self.requests), 1)

    async def test_truncation_keeps_safe_metadata_for_empty_partial_and_complete_content(self):
        for content in (None, "", '{"incomplete":', '{"ok":true}'):
            with self.subTest(content=content):
                self.requests.clear()
                client = self.make_client(lambda request: httpx.Response(
                    200, json=completion(content=content, finish_reason="length"),
                ))
                with self.assertRaises(LLMOutputLimitError):
                    await client.generate("Test prompt")
                self.assertEqual(client.last_finish_reason, "length")
                self.assertEqual(client.last_content_chars, None if content is None else len(content))
                self.assertEqual(len(self.requests), 1)

    async def test_response_metadata_is_reset_before_http_or_transport_failure(self):
        for failed in (httpx.Response(401), httpx.ConnectError("PRIVATE ERROR")):
            with self.subTest(error=type(failed).__name__):
                replies = [httpx.Response(200, json=completion()), failed]
                def handler(request):
                    reply = replies.pop(0)
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
                client = self.make_client(handler)
                self.assertIsNone(client.last_finish_reason)
                self.assertIsNone(client.last_content_chars)
                await client.generate("First prompt")
                self.assertEqual(client.last_finish_reason, "stop")
                self.assertEqual(client.last_content_chars, len('{"ok": true}'))
                with self.assertRaises((LLMHTTPError, LLMTransportError)):
                    await client.generate("Second prompt")
                self.assertIsNone(client.last_finish_reason)
                self.assertIsNone(client.last_content_chars)
                self.assertIsNone(client.last_usage)

    async def test_api_client_works_with_existing_engine_contract(self):
        response_json = FakeLLMClient().response_json
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        client = self.make_client(
            lambda request: httpx.Response(200, json=completion(content=response_json, usage=usage))
        )

        result = await AgentInsightEngine(client).generate_insight(
            CompanyResponse(id=1, synthetic_u="TEST", name="Тестовая компания"),
            NewsArticleResponse(
                id=1,
                title="Тестовая новость",
                source="Тестовый источник",
                published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                content="Тестовый текст новости.",
                category="Тест",
            ),
            [],
            RecordingTracer(),
        )

        expected = MLInsightResult.model_validate_json(response_json)
        expected.bank_view = BANK_VIEW_UNAVAILABLE
        self.assertEqual(result, expected)
        self.assertEqual(client.last_usage, usage)
        self.assertNotIn("usage", result.model_dump())
        self.assertEqual(len(self.requests), 1)

    async def test_engine_still_validates_generated_json_and_usage_survives(self):
        client = self.make_client(
            lambda request: httpx.Response(
                200, json=completion(content="not JSON", usage={"completion_tokens": 2})
            )
        )
        with self.assertRaises(AgentResponseError):
            await AgentInsightEngine(client).generate_insight(
                CompanyResponse(id=1, synthetic_u="TEST", name="Тест"),
                NewsArticleResponse(
                    id=1,
                    title="Тест",
                    source="Тест",
                    published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                    content="Тест",
                    category="Тест",
                ),
                [],
                RecordingTracer(),
            )
        self.assertEqual(client.last_usage, {"completion_tokens": 2})


if __name__ == "__main__":
    unittest.main()
