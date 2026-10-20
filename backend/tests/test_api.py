import asyncio
import pytest
import httpx


@pytest.mark.asyncio
async def test_health_check(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_companies_list_and_profile(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/companies")
    assert resp.status_code == 200
    companies = resp.json()
    assert len(companies) >= 3
    first_company = companies[0]
    assert "synthetic_u" in first_company
    assert first_company["revenue_company"] > 15_000_000_000  # Hackathon requirement: > 15 млрд руб.

    # Check profile
    profile_resp = await client.get(f"/api/v1/companies/{first_company['id']}")
    assert profile_resp.status_code == 200
    assert profile_resp.json()["name"] == first_company["name"]

    # Check counterparties graph
    cp_resp = await client.get(f"/api/v1/companies/{first_company['id']}/counterparties")
    assert cp_resp.status_code == 200
    assert "top_counterparties" in cp_resp.json()


@pytest.mark.asyncio
async def test_feed_and_bank_analytics(client: httpx.AsyncClient):
    # Company feed
    resp = await client.get("/api/v1/companies/1/feed")
    assert resp.status_code == 200
    feed = resp.json()
    assert "items" in feed
    assert len(feed["items"]) > 0

    # Bank analytics
    bank_resp = await client.get("/api/v1/analytics/bank")
    assert bank_resp.status_code == 200
    items = bank_resp.json()
    assert len(items) > 0
    assert any(item["is_bank_analytics"] for item in items)


@pytest.mark.asyncio
async def test_insight_pipeline_end_to_end(client: httpx.AsyncClient):
    # 1. Trigger insight generation
    create_resp = await client.post(
        "/api/v1/insights",
        json={"company_id": 1, "event_id": 1},
        headers={"X-User-Role": "CFO"}
    )
    assert create_resp.status_code == 200
    job_id = create_resp.json()["job_id"]
    assert job_id is not None

    # Allow background task to execute
    await asyncio.sleep(0.5)

    # 2. Check job status and trace steps
    job_resp = await client.get(f"/api/v1/jobs/{job_id}")
    assert job_resp.status_code == 200
    job_data = job_resp.json()
    assert job_data["status"] == "SUCCESS"
    assert len(job_data["trace_steps"]) >= 4  # Agentic multi-step execution logged
    insight_id = job_data["insight_id"]
    assert insight_id is not None

    # 3. Retrieve final insight
    insight_resp = await client.get(f"/api/v1/insights/{insight_id}")
    assert insight_resp.status_code == 200
    insight_data = insight_resp.json()
    assert "what_happened" in insight_data
    assert "why_important" in insight_data
    assert "consequences" in insight_data
    assert "bank_view" in insight_data
    assert "ai_view" in insight_data
    assert "recommended_action" in insight_data
    assert "relevant_bank_product" in insight_data
    assert len(insight_data["evidences"]) >= 2

    # 4. Ask follow up question (Criterion 10 drilldown)
    q_resp = await client.post(
        "/api/v1/questions",
        json={"insight_id": insight_id, "question": "Как это повлияет на кредитный портфель?"},
        headers={"X-User-Role": "CFO"}
    )
    assert q_resp.status_code == 200
    assert "answer" in q_resp.json()
