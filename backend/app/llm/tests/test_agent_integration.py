"""Shared budgeted transport through the actual agent, without a DB or API."""

import unittest
from decimal import Decimal
from unittest.mock import patch

import httpx

from backend.app.agent import RecordingTracer
from backend.app.agent.context import json_text
from backend.app.agent.models import AgentLLMError
from backend.app.agent.runtime import RuntimeAgentEngine
from backend.app.agent.tests.test_grounded import EmptySearchSource, fixtures, valid_draft
from backend.app.llm.budget_guard import BudgetedChatCompletionsClient
from backend.app.llm.client import ChatCompletionsConfig
from backend.app.llm.tests.test_budget_guard import FakeAtomicStore, MODEL, guard
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.insight import InsightResponse


class AgentSharedClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.company, self.event = fixtures()
        self.store = FakeAtomicStore()
        self.budget = guard(self.store)
        self.requests = []

    def runtime(self, *, approved=None):
        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": json_text(valid_draft())}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50,
                          "completion_tokens_details": {"reasoning_tokens": 10}},
            })
        return RuntimeAgentEngine(
            client_factory=lambda: BudgetedChatCompletionsClient(
                ChatCompletionsConfig(api_url="https://offline.invalid/chat", model=MODEL, max_output_tokens=200),
                budget_guard=self.budget, input_token_bound=lambda prompt: 100,
                transport=httpx.MockTransport(respond),
            ),
            source_factory=EmptySearchSource,
            approved_model_ids={MODEL} if approved is None else approved,
        )

    async def test_budgeted_client_preserves_grounding_telemetry_and_public_schema(self):
        result = await self.runtime().generate_analysis(self.company, self.event, [], RecordingTracer())
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.report["validation_summary"]["impacts"], 1)
        self.assertEqual(result.report["totals"]["total_tokens"], 50)
        self.assertEqual(result.report["calls"][0]["reasoning_tokens"], 10)
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("50"))
        self.assertEqual(self.store.held(), Decimal(0))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(set(result.insight.model_dump()), set(MLInsightResult.model_fields))
        public = InsightResponse.model_validate({
            "id": 1, "job_id": "offline-job", "company_id": self.company.id, "event_id": self.event.id,
            "created_at": self.event.published_at, **result.insight.model_dump(),
        })
        self.assertEqual(public.evidences[0].claim, self.event.content)
        self.assertNotIn("report", public.model_dump())

    async def test_budgeted_subclass_cannot_bypass_runtime_model_approval(self):
        with self.assertRaises(ValueError):
            await self.runtime(approved={"another-offline-model"}).generate_analysis(
                self.company, self.event, [], RecordingTracer(),
            )
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.reserve_calls, [])

    async def test_global_budget_refusal_blocks_provider_and_keeps_run_report(self):
        await self.budget.reserve(MODEL, max_input_tokens=200, max_completion_tokens=300)
        with self.assertRaises(AgentLLMError) as caught:
            await self.runtime().generate_analysis(self.company, self.event, [], RecordingTracer())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.held(), Decimal("500"))
        self.assertEqual(len(caught.exception.run_report["calls"]), 1)
        self.assertEqual(caught.exception.run_report["calls"][0]["status"], "error")
        self.assertIsNone(caught.exception.run_report["totals"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
