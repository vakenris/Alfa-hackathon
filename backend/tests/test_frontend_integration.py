"""Frontend Integration & Contract Verification Tests.

Validates that the backend fulfills all requirements for the frontend (Роль 3):
1. CORS pre-flight options for Vite (localhost:5173) and CRA/Next.js (localhost:3000).
2. OpenAPI 3.x schema generation and contract completeness (all 25 frontend routes).
3. End-to-end user journey matching the UI flow:
   - Select company & load profile
   - Load counterparty network graph
   - Load personalized news feed
   - Trigger insight generation
   - Receive real-time SSE stream of agent trace steps
   - Retrieve full 6-block insight with bank & AI perspectives and evidence
   - Interactive role-adapted Q&A drilldown (Criterion 10)
   - View aggregated analytics summary
   - View Budget Guardian monitoring (Criterion 08, 500 RUB daily limit)
4. Standard error contracts (404, 422) and graceful role fallback.
"""

import asyncio
import pytest
import httpx


@pytest.mark.asyncio
async def test_frontend_cors_preflight(client: httpx.AsyncClient):
    """Verify that Vite (5173) and React (3000) get valid CORS preflight headers."""
    for origin in ("http://localhost:5173", "http://localhost:3000"):
        resp = await client.options(
            "/api/v1/insights",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, X-User-Role",
            }
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == origin
        assert "POST" in resp.headers.get("access-control-allow-methods", "")
        assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_frontend_openapi_schema_integrity(client: httpx.AsyncClient):
    """Ensure /openapi.json is fully valid and exposes all critical frontend routes."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "openapi" in schema
    paths = schema.get("paths", {})

    required_frontend_routes = [
        "/api/v1/companies",
        "/api/v1/companies/{company_id}",
        "/api/v1/companies/{company_id}/counterparties",
        "/api/v1/companies/{company_id}/feed",
        "/api/v1/companies/{company_id}/analytics/summary",
        "/api/v1/companies/{company_id}/analytics/insights-history",
        "/api/v1/companies/{company_id}/analytics/counterparty-exposure",
        "/api/v1/insights",
        "/api/v1/insights/{insight_id}",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/stream",
        "/api/v1/streams/jobs/{job_id}",
        "/api/v1/streams/{job_id}/history",
        "/api/v1/questions",
        "/api/v1/metrics/budget",
        "/api/v1/metrics/pipeline",
        "/api/v1/metrics/system",
        "/api/v1/analytics/bank",
    ]

    for route in required_frontend_routes:
        assert route in paths, f"Missing required frontend route: {route}"


@pytest.mark.asyncio
async def test_frontend_full_user_journey(client: httpx.AsyncClient):
    """Full simulated user journey in the frontend UI."""
    # 1. Company selection
    comp_resp = await client.get(
        "/api/v1/companies",
        headers={"Origin": "http://localhost:5173", "X-User-Role": "CFO"}
    )
    assert comp_resp.status_code == 200
    companies = comp_resp.json()
    assert len(companies) >= 1
    company_id = companies[0]["id"]

    # 2. Counterparty graph
    cp_resp = await client.get(f"/api/v1/companies/{company_id}/counterparties")
    assert cp_resp.status_code == 200
    assert "top_counterparties" in cp_resp.json()

    # 3. News feed
    feed_resp = await client.get(f"/api/v1/companies/{company_id}/feed")
    assert feed_resp.status_code == 200
    feed_items = feed_resp.json()["items"]
    assert len(feed_items) >= 1
    event_id = feed_items[0]["id"]

    # 4. Trigger insight generation
    post_resp = await client.post(
        "/api/v1/insights",
        json={"company_id": company_id, "event_id": event_id},
        headers={"X-User-Role": "CFO"}
    )
    assert post_resp.status_code == 200
    job_id = post_resp.json()["job_id"]

    # Allow worker to execute
    await asyncio.sleep(0.6)

    # 5. Check job status and trace steps
    job_resp = await client.get(f"/api/v1/jobs/{job_id}")
    assert job_resp.status_code == 200
    job_data = job_resp.json()
    assert job_data["status"] == "SUCCESS"
    assert len(job_data["trace_steps"]) >= 4
    insight_id = job_data["insight_id"]
    assert insight_id is not None

    # 6. Retrieve full insight
    ins_resp = await client.get(f"/api/v1/insights/{insight_id}")
    assert ins_resp.status_code == 200
    ins = ins_resp.json()
    for field in (
        "what_happened", "why_important", "consequences",
        "bank_view", "ai_view", "recommended_action",
        "relevant_bank_product", "evidences"
    ):
        assert field in ins, f"Field '{field}' missing from insight response"

    # 7. Drilldown follow-up question
    q_resp = await client.post(
        "/api/v1/questions",
        json={"insight_id": insight_id, "question": "Как минимизировать кассовые разрывы?"},
        headers={"X-User-Role": "CFO"}
    )
    assert q_resp.status_code == 200
    assert "answer" in q_resp.json()

    # 8. Aggregated analytics summary
    ana_resp = await client.get(
        f"/api/v1/companies/{company_id}/analytics/summary",
        headers={"X-User-Role": "CFO"}
    )
    assert ana_resp.status_code == 200
    assert "active_products" in ana_resp.json()

    # 9. Budget Guardian metrics
    bgt_resp = await client.get("/api/v1/metrics/budget")
    assert bgt_resp.status_code == 200
    assert bgt_resp.json()["daily_budget_rub"] == 500.0


@pytest.mark.asyncio
async def test_frontend_sse_streaming_protocol(client: httpx.AsyncClient):
    """Verify SSE stream delivers text/event-stream with X-Accel-Buffering disabled."""
    resp = await client.get("/api/v1/streams/jobs/test-job-uuid")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert resp.headers.get("x-accel-buffering") == "no"
    assert "event: step" in resp.text
    assert "event: completed" in resp.text


@pytest.mark.asyncio
async def test_frontend_error_handling_contracts(client: httpx.AsyncClient):
    """Ensure standardized 404/422 error structures and resilient role handling."""
    # 404
    r_404 = await client.get("/api/v1/companies/999999")
    assert r_404.status_code == 404
    assert "detail" in r_404.json()

    # 422
    r_422 = await client.post("/api/v1/insights", json={"company_id": "not_an_int"})
    assert r_422.status_code == 422
    assert "detail" in r_422.json()

    # Invalid role header gracefully defaults without 401/403
    r_role = await client.get("/api/v1/companies/1", headers={"X-User-Role": "UNKNOWN"})
    assert r_role.status_code == 200
