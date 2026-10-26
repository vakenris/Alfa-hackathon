import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient

from backend.app.services.budget_service import (
    AppBudgetStore,
    get_budget_store,
    get_current_budget_window,
    get_budget_guard,
)
from backend.app.agent.companion import companion_store
from backend.app.worker.tasks import create_default_ml_engine


@pytest.mark.asyncio
async def test_budget_metrics_endpoint(client: AsyncClient):
    """Verifies that GET /api/v1/metrics/budget returns valid guardian metrics under 500 RUB cap."""
    response = await client.get(
        "/api/v1/metrics/budget",
        headers={"X-User-Role": "CFO"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["daily_budget_rub"] == 500.0
    assert data["spent_budget_rub"] >= 0.0
    assert data["remaining_budget_rub"] <= 500.0
    assert data["total_tokens_used"] > 0
    assert data["budget_exhausted"] is False


@pytest.mark.asyncio
async def test_budget_store_atomic_operations():
    """Verifies reservation, settlement, and limit enforcement in AppBudgetStore."""
    store = AppBudgetStore()
    window = get_current_budget_window()
    limit = Decimal("500")

    # 1. Successful reservation
    res1 = await store.reserve(
        team_id="test-team",
        window=window,
        limit_rub=limit,
        reservation_id="res-1",
        amount_rub=Decimal("50.0")
    )
    assert res1 is True

    # Idempotent reservation with identical amount
    res1_idempotent = await store.reserve(
        team_id="test-team",
        window=window,
        limit_rub=limit,
        reservation_id="res-1",
        amount_rub=Decimal("50.0")
    )
    assert res1_idempotent is True

    # 2. Reject reservation exceeding daily limit
    res_huge = await store.reserve(
        team_id="test-team",
        window=window,
        limit_rub=limit,
        reservation_id="res-huge",
        amount_rub=Decimal("460.0")  # 50 + 460 = 510 > 500
    )
    assert res_huge is False

    # 3. Settle reservation
    await store.settle(
        team_id="test-team",
        window_id=window.id,
        reservation_id="res-1",
        actual_rub=Decimal("42.50")
    )
    assert store.settled["res-1"] == Decimal("42.50")

    # 4. Token recording
    store.record_token_usage(prompt_tokens=100, completion_tokens=50)
    metrics = store.get_metrics()
    assert metrics.daily_budget_rub == 500.0
    assert metrics.spent_budget_rub >= 42.50


@pytest.mark.asyncio
async def test_questions_endpoint_drilldown(client: AsyncClient):
    """Verifies follow-up drilldown Q&A with role adaptation and grounded sources."""
    # Using seeded insight id = 1
    response = await client.post(
        "/api/v1/questions",
        json={
            "insight_id": 1,
            "question": "Каково влияние изменения ставки на оборотный капитал?"
        },
        headers={"X-User-Role": "CFO"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["insight_id"] == 1
    assert "CFO" in data["answer"]
    assert "Суть события:" in data["answer"]
    assert "Экспертиза Альфа-Банка:" in data["answer"]
    assert data["relevant_source"] is not None


@pytest.mark.asyncio
async def test_questions_endpoint_not_found(client: AsyncClient):
    """Verifies 404 response for nonexistent insight."""
    response = await client.post(
        "/api/v1/questions",
        json={
            "insight_id": 99999,
            "question": "Что произошло?"
        },
        headers={"X-User-Role": "CEO"}
    )
    assert response.status_code == 404


def test_default_engine_factory():
    """Ensures create_default_ml_engine returns a functional engine with generate_insight."""
    engine = create_default_ml_engine()
    assert hasattr(engine, "generate_insight")
