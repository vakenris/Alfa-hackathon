"""Tests for Блок A (analytics) + Блок B (audit/metrics) — Роль 1.

Covered endpoints:
  - GET /api/v1/companies/{id}/analytics/summary
  - GET /api/v1/companies/{id}/analytics/insights-history
  - GET /api/v1/companies/{id}/analytics/counterparty-exposure
  - GET /api/v1/metrics/pipeline
  - GET /api/v1/metrics/insights
  - GET /api/v1/metrics/audit
  - GET /api/v1/streams/jobs/{job_id}      (SSE — verify response type)
  - GET /api/v1/streams/feed/{company_id}  (SSE stub)

RBAC checks:
  - All C-level roles (CEO, CFO, TREASURER) can access analytics.
  - Invalid role header falls back to CEO (no 401/403).
"""

import asyncio
import pytest
import httpx


# ---------------------------------------------------------------------------
# Analytics summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analytics_summary_ceo(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/1/analytics/summary",
                            headers={"X-User-Role": "CEO"})
    assert resp.status_code == 200
    data = resp.json()
    assert "company_id" in data
    assert data["company_id"] == 1
    assert "company_name" in data
    assert "total_insights" in data
    assert "insights_last_7_days" in data
    assert "active_products" in data
    assert "rko" in data["active_products"]
    assert "revenue_rub" in data


@pytest.mark.asyncio
async def test_analytics_summary_cfо(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/1/analytics/summary",
                            headers={"X-User-Role": "CFO"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_analytics_summary_invalid_role_fallback(client: httpx.AsyncClient):
    """Invalid role should not raise 401/403 — falls back to CEO."""
    resp = await client.get("/api/v1/companies/1/analytics/summary",
                            headers={"X-User-Role": "UNKNOWN_ROLE"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_analytics_summary_company_not_found(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/99999/analytics/summary")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Insights history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analytics_insights_history(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/1/analytics/insights-history",
                            headers={"X-User-Role": "CEO"})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_analytics_insights_history_limit(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/1/analytics/insights-history?limit=5",
                            headers={"X-User-Role": "CFO"})
    assert resp.status_code == 200
    assert len(resp.json()) <= 5


@pytest.mark.asyncio
async def test_analytics_insights_history_company_not_found(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/99999/analytics/insights-history")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Counterparty exposure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analytics_counterparty_exposure(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/1/analytics/counterparty-exposure",
                            headers={"X-User-Role": "TREASURER"})
    assert resp.status_code == 200
    data = resp.json()
    assert "company_id" in data
    assert "total_outgoing_rub" in data
    assert "total_incoming_rub" in data
    assert "net_flow_rub" in data
    assert isinstance(data["counterparties"], list)
    for item in data["counterparties"]:
        assert item["direction"] in ("outgoing", "incoming")
        assert item["total_amount_rub"] >= 0


@pytest.mark.asyncio
async def test_analytics_counterparty_exposure_limit(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies/1/analytics/counterparty-exposure?limit=3")
    assert resp.status_code == 200
    # total counterparties list may have up to 3 outgoing + 3 incoming
    assert len(resp.json()["counterparties"]) <= 6


# ---------------------------------------------------------------------------
# Metrics — pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_pipeline(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/metrics/pipeline",
                            headers={"X-User-Role": "CEO"})
    assert resp.status_code == 200
    data = resp.json()
    assert "total_jobs" in data
    assert "success_rate_pct" in data
    assert 0.0 <= data["success_rate_pct"] <= 100.0
    assert "pending" in data
    assert "failed" in data


@pytest.mark.asyncio
async def test_metrics_pipeline_after_insight(client: httpx.AsyncClient):
    """Success counter should increase after a completed insight pipeline."""
    # Run a quick pipeline
    create_resp = await client.post(
        "/api/v1/insights",
        json={"company_id": 2, "event_id": 2},
        headers={"X-User-Role": "CFO"}
    )
    assert create_resp.status_code == 200
    await asyncio.sleep(0.6)

    resp = await client.get("/api/v1/metrics/pipeline")
    assert resp.status_code == 200
    assert resp.json()["total_jobs"] >= 1


# ---------------------------------------------------------------------------
# Metrics — insights
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_insights(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/metrics/insights")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_insights" in data
    assert "days" in data
    assert isinstance(data["days"], list)
    assert len(data["days"]) == 7   # default


@pytest.mark.asyncio
async def test_metrics_insights_custom_days(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/metrics/insights?days=14")
    assert resp.status_code == 200
    assert len(resp.json()["days"]) == 14


# ---------------------------------------------------------------------------
# Metrics — audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_audit_accessible(client: httpx.AsyncClient):
    # Make at least one request so there's something to audit
    await client.get("/api/v1/companies/1")
    resp = await client.get("/api/v1/metrics/audit?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_records" in data
    assert "entries" in data
    assert isinstance(data["entries"], list)


@pytest.mark.asyncio
async def test_metrics_audit_role_filter(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/metrics/audit?role=CEO")
    assert resp.status_code == 200
    for entry in resp.json()["entries"]:
        assert entry["user_role"] == "CEO"


# ---------------------------------------------------------------------------
# Streams — SSE response type check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_job_not_found_returns_error_event(client: httpx.AsyncClient):
    """Non-existent job_id should yield an SSE error event, not a 500."""
    resp = await client.get("/api/v1/streams/jobs/non-existent-job-id")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "error" in body


@pytest.mark.asyncio
async def test_stream_feed_connected_event(client: httpx.AsyncClient):
    """Feed stream must:
    - return 200 with text/event-stream content-type
    - emit a `connected` SSE event as the very first chunk

    We use stream=True + read a limited chunk so we don't block waiting
    for the 30-second idle heartbeat.
    """
    async with client.stream("GET", "/api/v1/streams/feed/1") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Read the first chunk only — must contain the `connected` event
        chunk = b""
        async for raw in resp.aiter_bytes(chunk_size=512):
            chunk += raw
            break   # stop after first chunk; don't wait for stream close
    assert b"connected" in chunk
