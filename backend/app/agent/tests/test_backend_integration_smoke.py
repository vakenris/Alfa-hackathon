"""Backend contract smoke: real DTO/adapter/runtime, fake repository and HTTP.

No app startup, SQLAlchemy, DB, environment configuration or network is needed.
HTTP responses and verified provenance are explicitly synthetic fixtures. This
checks integration plumbing, not LLM answer quality or real database persistence.
"""

from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from backend.app.agent import (
    AgentConfig, BackendAnalyticsSource, ManagementActionEngine, RecordingTracer,
    RuntimeAgentEngine,
)
from backend.app.agent.context import json_text
from backend.app.agent.models import AgentAnalysisResult, BANK_VIEW_UNAVAILABLE
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.llm.budget_guard import BudgetedChatCompletionsClient
from backend.app.llm.client import ChatCompletionsConfig
from backend.app.llm.tests.test_budget_guard import FakeAtomicStore, MODEL, guard, rate
from backend.app.ml.interface import MLInsightEngineProtocol, MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse
from backend.app.schemas.insight import InsightResponse
from backend.app.schemas.question import QuestionAnswerResponse


class BackendIntegrationSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        network.start()
        self.addCleanup(network.stop)
        company, event = fixtures()
        # Match the worker's from_attributes conversion, not a local runner DTO.
        self.company = CompanyResponse.model_validate(SimpleNamespace(
            **{**company.model_dump(), "cl_product_loa_mark": True},
        ))
        self.event = NewsArticleResponse.model_validate(SimpleNamespace(**event.model_dump()))
        self.counterparties = [CounterpartyItem.model_validate(row) for row in (
            {"synthetic_u": "SMOKE_OUT", "name": "Исходящий участник",
             "direction": "SUPPLIER", "total_amount": 1200, "transaction_count": 3},
            {"synthetic_u": "SMOKE_IN", "name": "Входящий участник",
             "direction": "CUSTOMER", "total_amount": 700, "transaction_count": 2},
        )]
        self.bank_row = SimpleNamespace(**{
            **self.event.model_dump(), "id": 9, "title": "Учебный банковский обзор ставки",
            "source": "Синтетический банковский источник",
            "content": "Прогноз ключевой ставки на конец 2027 года составляет 12% годовых.",
            "published_at": self.event.published_at.replace(day=20), "is_bank_analytics": True,
        })
        self.requests = []
        self.sessions = {"opened": 0, "closed": 0, "limits": []}
        self.store = FakeAtomicStore()
        # Fixture rates are deliberately artificial; never organizer pricing.
        self.budget = guard(self.store, rate_cards={MODEL: rate(
            input_rub_per_million=Decimal("1000"), output_rub_per_million=Decimal("1000"),
        )})

    def runtime(self, *, verified):
        state, row = self.sessions, self.bank_row
        session = object()

        @asynccontextmanager
        async def session_factory():
            state["opened"] += 1
            try:
                yield session
            finally:
                state["closed"] += 1

        class FixtureNewsRepository:
            def __init__(self, supplied_session):
                if supplied_session is not session:
                    raise AssertionError("Wrong repository session")

            async def list_bank_analytics(self, limit):
                state["limits"].append(limit)
                return [row]

        # Patch only the adapter's lazy repository import. Never import the
        # application DB session/settings or substitute the agent/search logic.
        modules = {}
        for name in ("backend.app.db", "backend.app.db.repositories"):
            modules[name] = ModuleType(name)
            modules[name].__path__ = []
        module_name = "backend.app.db.repositories.news_repo"
        modules[module_name] = ModuleType(module_name)
        modules[module_name].NewsRepository = FixtureNewsRepository
        repository_patch = patch.dict(sys.modules, modules)
        repository_patch.start()
        self.addCleanup(repository_patch.stop)

        self.metadata = {
            "source_url": "https://offline.invalid/smoke-report.pdf",
            "source_document_id": "synthetic:smoke-report", "source_page": 2,
            "source_hash": hashlib.sha256(row.content.encode()).hexdigest(),
            "provenance_status": "verified",
        }

        def resolve_metadata(article):
            self.assertEqual(article.id, row.id)
            return self.metadata.copy()

        def respond(request):
            self.assertEqual(str(request.url), "https://offline.invalid/chat")
            self.assertNotIn("authorization", request.headers)
            payload = json.loads(request.content)
            self.requests.append(payload)
            draft = valid_draft()
            draft["impacts"][0]["evidence_ids"] += ["payments:summary", "bank:9:excerpt"]
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": json_text(draft)}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50,
                          "completion_tokens_details": {"reasoning_tokens": 10}},
            })

        return RuntimeAgentEngine(
            client_factory=lambda: BudgetedChatCompletionsClient(
                ChatCompletionsConfig(api_url="https://offline.invalid/chat", model=MODEL,
                                      max_output_tokens=2048),
                budget_guard=self.budget,
                # Bounds this known synthetic response usage only. This is NOT
                # a production tokenizer or a bound for a real gateway request.
                input_token_bound=lambda prompt: len(prompt.encode("utf-8")) + 4096,
                transport=httpx.MockTransport(respond),
            ),
            source_factory=lambda: BackendAnalyticsSource.from_repository_factory(
                session_factory, resolve_metadata if verified else None,
            ),
            approved_model_ids={MODEL},
            config=AgentConfig(max_llm_calls=1, max_recovery_attempts=0),
        )

    def assert_inputs_and_lifecycle(self):
        self.assertEqual(self.sessions, {"opened": 1, "closed": 1, "limits": [50]})
        self.assertEqual(len(self.requests), 1)
        prompt = self.requests[0]["messages"][0]["content"]
        for text in (self.company.synthetic_u, "24.10", self.event.content,
                     "SMOKE_OUT", "SMOKE_IN", self.bank_row.content):
            self.assertIn(text, prompt)
        self.assertEqual(self.store.held(), Decimal(0))
        self.assertEqual(len(self.store.reserve_calls), 1)
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("0.05"))

    async def test_worker_public_contract_and_frontend_response_projection(self):
        engine = self.runtime(verified=False)
        self.assertIsInstance(engine, MLInsightEngineProtocol)
        result = await engine.generate_insight(
            company=self.company, event=self.event, counterparties=self.counterparties,
            tracer=RecordingTracer(),
        )
        self.assertIsInstance(result, MLInsightResult)
        self.assertEqual(set(result.model_dump()), set(MLInsightResult.model_fields))
        # ORM-shaped fixture for InsightResponse, not a simulated DB write.
        saved_row = SimpleNamespace(
            **{**result.model_dump(exclude={"evidences"}),
               "evidences": [SimpleNamespace(**ev.model_dump()) for ev in result.evidences]},
            id=100, job_id="offline-job", company_id=self.company.id,
            event_id=self.event.id, created_at=self.event.published_at,
        )
        public = InsightResponse.model_validate(saved_row)
        for field in MLInsightResult.model_fields:
            self.assertEqual(public.model_dump()[field], result.model_dump()[field])
        self.assertEqual(public.bank_view, BANK_VIEW_UNAVAILABLE)
        self.assertIn(self.bank_row.content, public.ai_view)
        self.assertTrue(public.evidences)
        self.assert_inputs_and_lifecycle()

    async def test_saved_companion_grounded_qa_and_company_boundary_without_extra_http(self):
        result = await self.runtime(verified=True).generate_analysis(
            self.company, self.event, self.counterparties, RecordingTracer(),
        )
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.report["totals"]["total_tokens"], 50)
        self.assertEqual(result.report["calls"][0]["reasoning_tokens"], 10)
        self.assertIn(self.bank_row.content, result.insight.bank_view)
        # What a backend-owned JSON companion store must preserve on reload.
        saved = result.model_dump_json()
        restored = AgentAnalysisResult.model_validate_json(saved)
        self.assertEqual(restored.model_dump(mode="json"), result.model_dump(mode="json"))
        qa = ManagementActionEngine(None)
        answer = await qa.answer_question(
            "Что пишет банковский обзор и каковы источники прогноза?",
            analysis=restored, company=self.company, tracer=RecordingTracer(),
        )
        self.assertEqual(answer.status, "complete")
        bank_ref = next(ref for ref in answer.source_refs if ref.source_id == "bank:9")
        for key, value in self.metadata.items():
            self.assertEqual(getattr(bank_ref, key), value)
        self.assertEqual(bank_ref.quote, self.bank_row.content)
        self.assertEqual(bank_ref.published_at, self.bank_row.published_at)
        response = QuestionAnswerResponse(
            insight_id=100, question="Что пишет банковский обзор?",
            answer="\n".join([answer.summary, *answer.limitations, answer.disclaimer]),
            relevant_source="; ".join(f"{ref.source_title} [{ref.fact_id}]" for ref in answer.source_refs),
        )
        self.assertIn(self.bank_row.content, response.answer)
        self.assertIn("bank:9:excerpt", response.relevant_source)
        payments = await qa.answer_question(
            "Какие исходящие платежи есть в выборке?",
            analysis=restored, company=self.company, tracer=RecordingTracer(),
        )
        self.assertEqual(payments.status, "complete")
        self.assertIn("1200", payments.summary)
        debt = await qa.answer_question(
            "Какой фактический долг у компании?",
            analysis=restored, company=self.company, tracer=RecordingTracer(),
        )
        self.assertEqual(debt.status, "needs_data")  # loan product flag != debt
        with self.assertRaisesRegex(ValueError, "ACTION_COMPANY_MISMATCH"):
            await qa.answer_question(
                "Какие источники?", analysis=restored,
                company=self.company.model_copy(update={"id": self.company.id + 1}),
                tracer=RecordingTracer(),
            )
        tampered = AgentAnalysisResult.model_validate_json(saved)
        next(source for source in tampered.sources if source.source_id == "bank:9").source_hash = "changed"
        refused = await qa.answer_question(
            "Что пишет банковский обзор?", analysis=tampered,
            company=self.company, tracer=RecordingTracer(),
        )
        self.assertEqual(refused.status, "insufficient_evidence")
        self.assertEqual(answer.report["provider_attempts"], 0)
        self.assert_inputs_and_lifecycle()  # Q&A never opens a session or HTTP call.


if __name__ == "__main__":
    unittest.main()
