import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select

from backend.app.db.models.agent_trace import JobRecord
from backend.app.db.models.news import NewsArticle
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.db.session import async_session_factory
from backend.app.services import automatic_insights


@pytest.mark.asyncio
async def test_feed_analyzes_all_news_and_reuses_results(client):
    async with async_session_factory() as db:
        expected_ids = set((await db.execute(select(NewsArticle.id))).scalars())
    # A paginated feed must still schedule the entire corpus.
    response = await client.get("/api/v1/companies/3/feed?limit=1")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    snapshot = (await client.get("/api/v1/companies/3/analytics/automatic-insights")).json()
    assert {item["event_id"] for item in snapshot["items"]} == expected_ids
    assert all(job["status"] == "SUCCESS" for job in snapshot["jobs"])
    async with async_session_factory() as db:
        before = (await db.execute(select(func.count(JobRecord.id)).where(JobRecord.company_id == 3))).scalar_one()
    await client.get("/api/v1/companies/3/feed")
    async with async_session_factory() as db:
        after = (await db.execute(select(func.count(JobRecord.id)).where(JobRecord.company_id == 3))).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_two_tabs_share_automatic_jobs(client):
    first, second = await asyncio.gather(
        client.get("/api/v1/companies/2/feed"), client.get("/api/v1/companies/2/feed"),
    )
    assert first.status_code == second.status_code == 200
    async with async_session_factory() as db:
        rows = (await db.execute(select(JobRecord.event_id, func.count(JobRecord.id))
                                .where(JobRecord.company_id == 2).group_by(JobRecord.event_id))).all()
    assert rows
    assert all(count == 1 for _, count in rows)


@pytest.mark.asyncio
async def test_explicit_request_reuses_active_automatic_job(client, monkeypatch):
    job_id = str(uuid.uuid4())
    async with async_session_factory() as db:
        db.add(JobRecord(id=job_id, company_id=2, event_id=1, status="PROCESSING"))
        await db.commit()
    async def no_cached_result(self, company_id, event_id):
        return None
    async def unexpected_pipeline(**kwargs):
        pytest.fail("An already active job must not launch a second pipeline")
    monkeypatch.setattr(InsightRepository, "get_by_company_and_event", no_cached_result)
    monkeypatch.setattr(automatic_insights, "run_insight_pipeline", unexpected_pipeline)
    try:
        response = await client.post("/api/v1/insights", json={"company_id": 2, "event_id": 1})
        assert response.status_code == 200
        assert response.json()["job_id"] == job_id
        assert response.json()["status"] == "PROCESSING"
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(JobRecord).where(JobRecord.id == job_id))
            await db.commit()


@pytest.mark.asyncio
async def test_new_article_is_analyzed_and_failed_job_is_retried(client, monkeypatch):
    async with async_session_factory() as db:
        article = NewsArticle(title="New automatic analysis event", content="A new event for the company.", category="Industry")
        db.add(article)
        await db.commit()
        event_id = article.id
    original_pipeline = automatic_insights.run_insight_pipeline
    async def fail_new_article(job_id, company_id, event_id):
        async with async_session_factory() as db:
            await JobRepository(db).update_status(job_id, "FAILED", "Temporary test failure")
    monkeypatch.setattr(automatic_insights, "run_insight_pipeline", fail_new_article)
    await client.get("/api/v1/companies/3/feed?limit=1")
    snapshot = (await client.get("/api/v1/companies/3/analytics/automatic-insights")).json()
    job = next(item for item in snapshot["jobs"] if item["event_id"] == event_id)
    assert job["status"] == "FAILED"
    assert job["error_message"] == "Temporary test failure"
    monkeypatch.setattr(automatic_insights, "run_insight_pipeline", original_pipeline)
    await client.get("/api/v1/companies/3/feed?limit=1")
    snapshot = (await client.get("/api/v1/companies/3/analytics/automatic-insights")).json()
    assert next(item for item in snapshot["jobs"] if item["event_id"] == event_id)["status"] == "SUCCESS"
    assert any(item["event_id"] == event_id for item in snapshot["items"])


@pytest.mark.asyncio
async def test_unknown_company_does_not_trigger_analysis(client):
    assert (await client.get("/api/v1/companies/999999/feed")).status_code == 404
    assert (await client.get("/api/v1/companies/999999/analytics/automatic-insights")).status_code == 404
