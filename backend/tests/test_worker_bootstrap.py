import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import ModuleType

import httpx
import pytest
from pydantic import ValidationError

from backend.app.worker.bootstrap import (
    RuntimeSettings,
    create_engine,
)
from backend.app.worker.config import worker_settings


pytest.importorskip(
    "backend.app.agent.runtime",
    reason="Requires the ML branch",
)


from backend.app.agent.tracer import RecordingTracer
from backend.app.ingestion.analytics_source import BankPdfAnalyticsSource
from backend.app.llm.budget_guard import (
    BudgetGuard,
    BudgetWindow,
    BudgetedChatCompletionsClient,
    RateCard,
)
from backend.app.llm.client import ChatCompletionsConfig
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse


class OfflineStore:
    async def reserve(self, **kwargs):
        return True

    async def settle(self, **kwargs):
        pass


@pytest.fixture
def runtime_factory(monkeypatch):
    monkeypatch.setenv(
        "WORKER_CLIENT_FACTORY",
        "offline_worker_client:make_client",
    )
    monkeypatch.setenv(
        "WORKER_APPROVED_MODEL_IDS",
        '["offline-test-model"]',
    )
    monkeypatch.setattr(
        worker_settings, "timeout_seconds", 450
    )

    module = ModuleType("offline_worker_client")
    requests = []
    now = datetime.now(timezone.utc)

    guard = BudgetGuard(
        store=OfflineStore(),
        team_id="offline-test-team",
        window_provider=lambda: BudgetWindow(
            "offline-test-window",
            now - timedelta(hours=1),
            now + timedelta(hours=1),
        ),
        rate_cards={
            "offline-test-model": RateCard(
                "offline-test-model",
                Decimal("1"),
                Decimal("2"),
                "synthetic offline test tariff",
            ),
        },
    )

    def respond(request):
        requests.append(request)

        draft = {
            "applicability": "insufficient",
            "missing_data": [
                "Связь события с компанией не установлена.",
            ],
        }

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                draft,
                                ensure_ascii=False,
                            ),
                        },
                    },
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                },
            },
        )

    def make_client():
        return BudgetedChatCompletionsClient(
            ChatCompletionsConfig(
                api_url=(
                    "https://example.invalid"
                    "/v1/chat/completions"
                ),
                model="offline-test-model",
            ),
            budget_guard=guard,
            input_token_bound=lambda prompt: 100_000,
            transport=httpx.MockTransport(respond),
        )

    module.make_client = make_client

    monkeypatch.setitem(
        sys.modules,
        module.__name__,
        module,
    )

    return module, requests


def test_bootstrap_connects_real_runtime_to_reviewed_pdf_source(
    runtime_factory,
):
    runtime = create_engine()

    assert isinstance(
        runtime._source_factory(),
        BankPdfAnalyticsSource,
    )
    assert (
        runtime._client_factory()
        is not runtime._client_factory()
    )
    assert runtime.config.max_llm_calls == 1
    assert runtime.config.max_recovery_attempts == 0


@pytest.mark.parametrize(
    "models",
    [
        [],
        [" "],
        ["model", "model"],
    ],
)
def test_invalid_model_lists_are_rejected(models):
    with pytest.raises(ValidationError):
        RuntimeSettings(
            client_factory="module:factory",
            approved_model_ids=models,
        )


def test_worker_timeout_must_exceed_agent_timeout(
    runtime_factory, monkeypatch,
):
    monkeypatch.setattr(
        worker_settings, "timeout_seconds", 180
    )

    with pytest.raises(ValueError, match="450"):
        create_engine()


def test_nonbudgeted_client_is_rejected_before_network(
    runtime_factory,
):
    module, requests = runtime_factory
    module.make_client = lambda: object()

    with pytest.raises(
        ValueError,
        match="BudgetedChat",
    ):
        create_engine()._client_factory()

    assert requests == []


def test_async_factory_is_rejected(runtime_factory):
    module, _ = runtime_factory

    async def invalid_factory():
        return object()

    module.make_client = invalid_factory

    with pytest.raises(
        ValueError,
        match="синхронно",
    ):
        create_engine()


def test_unapproved_model_is_rejected_before_network(
    runtime_factory, monkeypatch,
):
    _, requests = runtime_factory

    monkeypatch.setenv(
        "WORKER_APPROVED_MODEL_IDS",
        '["another-model"]',
    )

    with pytest.raises(ValueError, match="Model ID"):
        create_engine()._client_factory()

    assert requests == []


@pytest.mark.asyncio
async def test_runtime_public_contract_works_with_offline_budgeted_transport(
    runtime_factory,
):
    _, requests = runtime_factory
    runtime = create_engine()

    result = await runtime.generate_insight(
        CompanyResponse(
            id=1,
            synthetic_u="U_1",
            name="Тестовая компания",
        ),
        NewsArticleResponse(
            id=1,
            title="Шахматный турнир",
            source="Синтетический тест",
            published_at=datetime.now(timezone.utc),
            content=(
                "Завершился городской шахматный турнир."
            ),
            category="Тест",
        ),
        [],
        RecordingTracer(),
    )

    assert isinstance(result, MLInsightResult)
    assert len(requests) == 1