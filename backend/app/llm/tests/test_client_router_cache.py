"""Autonomous shared infrastructure tests; all HTTP uses MockTransport."""

import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx

from backend.app.llm import (
    ChatCompletionsClient, ChatCompletionsConfig, KNOWN_LIVE_MODEL_ID,
    LLMHTTPError, LLMOutputLimitError, ModelRouter, VerifiedJsonCache,
    make_verified_cache_key,
)


def configuration(**updates):
    return ChatCompletionsConfig(**{
        "api_url": "https://gateway.invalid/v1/chat/completions",
        "model": KNOWN_LIVE_MODEL_ID,
        "auth_header": "X-Test-Authorization", "auth_value": "offline-fixture-only",
        "max_output_tokens": 128, **updates,
    })


def companion():
    return {
        "company_id": 7, "event_id": 9,
        "insight": {"what_happened": "Synthetic event.", "evidences": []},
        "analysis": {"action": "draft", "applicability": "conditional",
                     "facts": [{"id": "fact:event", "quote": "Synthetic event."}]},
        "business_context": {"known_features": {"mainokved_code": "24.10"}},
        "sources": [{"source_id": "news:9"}],
        "verification": {"passed": True, "issues": [], "warnings": []},
        "report": {"calls": [{"usage": {"total_tokens": 100}}]},
    }


class SharedTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def test_agent_imports_preserve_class_identity(self):
        from backend.app.agent.api_client import ChatCompletionsClient as AgentClient
        from backend.app.agent.api_client import LLMHTTPError as AgentHTTPError
        from backend.app.agent.api_client import ChatCompletionsConfig as AgentAPIConfig
        from backend.app.agent.config import ChatCompletionsConfig as AgentConfig
        self.assertIs(AgentClient, ChatCompletionsClient)
        self.assertIs(AgentConfig, ChatCompletionsConfig)
        self.assertIs(AgentAPIConfig, ChatCompletionsConfig)
        self.assertIs(AgentHTTPError, LLMHTTPError)

    async def test_router_creates_fresh_existing_transport_and_one_request(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": "fixture"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            })
        router = ModelRouter(configuration(), {"insight": KNOWN_LIVE_MODEL_ID},
                             approved_model_ids={KNOWN_LIVE_MODEL_ID})
        first = router.client_for("insight", transport=httpx.MockTransport(respond))
        second = router.client_for("insight", transport=httpx.MockTransport(respond))
        self.assertIs(type(first), ChatCompletionsClient)
        self.assertIsNot(first, second)
        self.assertIsNot(first.config, second.config)
        self.assertEqual(requests, [])
        self.assertEqual(await first.generate("exact prompt"), "fixture")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], KNOWN_LIVE_MODEL_ID)
        self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "exact prompt"}])
        self.assertEqual(first.last_usage["total_tokens"], 3)
        self.assertIsNone(second.last_usage)

    async def test_single_default_model_mode_uses_one_request(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": "fixture"}}],
            })
        router = ModelRouter(configuration(), approved_model_ids={KNOWN_LIVE_MODEL_ID})
        self.assertEqual(router.config_for("draft").model, KNOWN_LIVE_MODEL_ID)
        self.assertEqual(router.config_for("repair").model, KNOWN_LIVE_MODEL_ID)
        client = router.client_for("insight", transport=httpx.MockTransport(respond))
        self.assertEqual(await client.generate("fixture"), "fixture")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], KNOWN_LIVE_MODEL_ID)

    async def test_failure_does_not_retry_or_fallback(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(503)
        router = ModelRouter(configuration(), {"insight": KNOWN_LIVE_MODEL_ID},
                             approved_model_ids={KNOWN_LIVE_MODEL_ID})
        client = router.client_for("insight", transport=httpx.MockTransport(respond))
        with self.assertRaises(LLMHTTPError):
            await client.generate("fixture")
        self.assertEqual(len(requests), 1)
        self.assertIsNone(client.last_usage)

    async def test_output_truncation_still_rejected_with_usage(self):
        def respond(request):
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
                "usage": {"total_tokens": 20},
            })
        client = ChatCompletionsClient(configuration(), transport=httpx.MockTransport(respond))
        with self.assertRaises(LLMOutputLimitError):
            await client.generate("fixture")
        self.assertEqual(client.last_finish_reason, "length")
        self.assertEqual(client.last_usage["total_tokens"], 20)


class ModelRouterTests(unittest.TestCase):
    def test_exact_approval_is_required_without_inferred_aliases(self):
        for approved in ([], "deepseek", {"deepseek"}, {" "}, {KNOWN_LIVE_MODEL_ID + " "}):
            with self.subTest(approved=approved), self.assertRaises(ValueError):
                ModelRouter(configuration(), {"insight": KNOWN_LIVE_MODEL_ID},
                            approved_model_ids=approved)
        with self.assertRaises(TypeError):
            ModelRouter(configuration(), {"insight": KNOWN_LIVE_MODEL_ID})
        with self.assertRaises(ValueError):
            ModelRouter(configuration(), {"insight": "unconfirmed/qwen-alias"},
                        approved_model_ids={KNOWN_LIVE_MODEL_ID})

    def test_routes_are_explicit_immutable_and_config_isolated(self):
        routes = {"insight": KNOWN_LIVE_MODEL_ID}
        base = configuration()
        router = ModelRouter(base, routes, approved_model_ids={KNOWN_LIVE_MODEL_ID})
        routes["insight"] = "changed"
        with self.assertRaises(ValueError):
            base.model = "changed"
        selected = router.config_for("insight")
        with self.assertRaises(ValueError):
            selected.model = "changed-again"
        self.assertIsNot(selected, router.config_for("insight"))
        self.assertEqual(router.config_for("insight").model, KNOWN_LIVE_MODEL_ID)
        with self.assertRaises(TypeError):
            router.routes["insight"] = "changed"
        with self.assertRaises(ValueError):
            router.client_for("unknown")

    def test_default_mode_rejects_malformed_tasks_and_explicit_maps_reject_unknown(self):
        default = ModelRouter(configuration(), approved_model_ids={KNOWN_LIVE_MODEL_ID})
        explicit = ModelRouter(configuration(), {"insight": KNOWN_LIVE_MODEL_ID},
                               approved_model_ids={KNOWN_LIVE_MODEL_ID})
        for task in (None, 1, "", " ", "\n", " insight", "insight ", "x" * 129):
            for router in (default, explicit):
                with self.subTest(task=task, default=router is default), self.assertRaises(ValueError):
                    router.config_for(task)
        with self.assertRaises(ValueError):
            explicit.config_for("repair")
        with self.assertRaises(ValueError):
            ModelRouter(configuration(), {}, approved_model_ids={KNOWN_LIVE_MODEL_ID})

    def test_shared_package_import_has_no_backend_or_agent_dependency(self):
        script = (
            "import sys; import backend.app.llm; "
            "assert 'backend.app.agent' not in sys.modules; "
            "assert 'backend.app.main' not in sys.modules; "
            "assert 'backend.app.core.config' not in sys.modules; "
            "assert 'backend.app.db.session' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class VerifiedCacheTests(unittest.TestCase):
    def key(self, **updates):
        return make_verified_cache_key(**{
            "tenant_id": "tenant-a", "user_id": "user-a", "company_id": 7,
            "config": configuration(), "prompt_version": "grounded-v1",
            "prompt": "exact synthetic prompt", "company_context": {"industry": "24.10"},
            "sources_version": "corpus-verified-v1", **updates,
        })

    def test_every_scope_or_input_change_misses(self):
        cache = VerifiedJsonCache()
        original = self.key()
        cache.put_verified(original, companion())
        for update in (
            {"tenant_id": "tenant-b"}, {"user_id": "user-b"}, {"company_id": 8},
            {"prompt_version": "v2"}, {"prompt": "different"},
            {"company_context": {"industry": "25.10"}}, {"sources_version": "corpus-v2"},
            {"config": configuration(api_url="https://other.invalid/chat")},
            {"config": configuration(model="explicit-test-model")},
            {"config": configuration(max_output_tokens=64)},
            {"config": configuration(token_limit_field="max_completion_tokens")},
        ):
            with self.subTest(update=update):
                self.assertNotEqual(original, self.key(**update))
                self.assertIsNone(cache.get(self.key(**update)))

    def test_no_secret_prompt_or_context_is_exposed_in_key(self):
        key = self.key()
        rendered = repr(key)
        self.assertNotIn("offline-fixture-only", rendered)
        self.assertNotIn("exact synthetic prompt", rendered)
        self.assertNotIn("24.10", rendered)
        self.assertNotIn("gateway.invalid", rendered)
        # Credential rotation is outside the key; authorized tenant/user scope
        # must identify the same policy/account and is supplied by the app.
        self.assertEqual(key, self.key(config=configuration(auth_value="new-offline-secret")))

    def test_get_is_copy_isolated_and_never_replays_original_report(self):
        cache = VerifiedJsonCache()
        key = self.key()
        original = companion()
        cache.put_verified(key, original)
        original["analysis"]["facts"][0]["quote"] = "changed"
        first = cache.get(key)
        self.assertNotIn("report", first)
        self.assertEqual(first["analysis"]["facts"][0]["quote"], "Synthetic event.")
        first["verification"]["passed"] = False
        self.assertTrue(cache.get(key)["verification"]["passed"])

    def test_unverified_failed_partial_or_wrong_company_cannot_be_cached(self):
        invalid = []
        for field, value in (("verification", {"passed": False, "issues": []}),
                             ("verification", {"passed": True, "issues": ["bad"]}),
                             ("company_id", 8), ("company_id", True),
                             ("error", "provider-failed"), ("status", "failed"),
                             ("analysis", {})):
            payload = companion()
            payload[field] = value
            invalid.append(payload)
        partial = companion()
        del partial["report"]
        invalid.append(partial)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                VerifiedJsonCache().put_verified(self.key(), payload)

    def test_ttl_lru_eviction_and_clear(self):
        clock = [100.0]
        cache = VerifiedJsonCache(max_entries=2, ttl_seconds=10, clock=lambda: clock[0])
        one, two, three = (self.key(prompt=text) for text in ("one", "two", "three"))
        cache.put_verified(one, companion())
        cache.put_verified(two, companion())
        self.assertIsNotNone(cache.get(one))
        cache.put_verified(three, companion())
        self.assertIsNone(cache.get(two))
        self.assertIsNotNone(cache.get(one))
        clock[0] = 110.0
        self.assertIsNone(cache.get(one))
        self.assertIsNone(cache.get(three))
        cache.put_verified(one, companion())
        cache.clear()
        self.assertIsNone(cache.get(one))

    def test_finite_json_and_payload_bounds(self):
        for bad in (float("nan"), float("inf"), object()):
            payload = companion()
            payload["analysis"]["extra"] = bad
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                VerifiedJsonCache().put_verified(self.key(), payload)
        payload = companion()
        payload["analysis"]["extra"] = "x" * 2000
        with self.assertRaises(ValueError):
            VerifiedJsonCache(max_payload_bytes=1024).put_verified(self.key(), payload)
        for kwargs in ({"ttl_seconds": 0}, {"ttl_seconds": float("inf")},
                       {"max_entries": True}, {"max_payload_bytes": 100}, {"clock": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                VerifiedJsonCache(**kwargs)


if __name__ == "__main__":
    unittest.main()
