import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.models.agent_trace import AgentTraceStep, JobRecord
from backend.app.db.models.company import Company
from backend.app.db.models.insight import Insight
from backend.app.db.models.news import NewsArticle
from backend.app.db.repositories.insight_repo import InsightRepository
from backend.app.ml.interface import MLInsightResult
from backend.app.worker import tasks
from backend.app.worker.config import worker_settings
from backend.app.worker.queue import DatabaseJobQueue, utc_now
from backend.app.worker.runner import load_engine


class TestBus:
    __test__ = False

    def __init__(self):
        self.messages = []
        self.fail = False

    async def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError("Test Redis outage")
        self.messages.append(payload)


def result():
    return MLInsightResult(
        what_happened="test",
        why_important="test",
        consequences="test",
        bank_view="test fixture",
        ai_view="test fixture",
        recommended_action="test",
    )


class TestEngine:
    __test__ = False

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.delay = 0

    async def generate_insight(self, *, tracer, **kwargs):
        self.calls += 1

        if self.fail:
            raise RuntimeError("Test ML failure")

        await asyncio.sleep(self.delay)
        await tracer.emit_step("test step", "test description")

        return result()


@pytest_asyncio.fixture
async def worker_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///"
        + (tmp_path / "worker.db").as_posix()
    )
    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session:
        session.add_all([
            Company(
                id=1,
                synthetic_u="U_1",
                name="Test",
                revenue_company=100,
            ),
            NewsArticle(
                id=1,
                title="Test",
                content="Test",
            ),
        ])
        await session.commit()

    ml = TestEngine()
    bus = TestBus()

    monkeypatch.setattr(
        tasks, "async_session_factory", factory
    )
    monkeypatch.setattr(tasks, "current_ml_engine", ml)
    monkeypatch.setattr(tasks, "get_event_bus", lambda: bus)
    monkeypatch.setattr(worker_settings, "mode", "inline")

    try:
        yield factory, ml, bus
    finally:
        await engine.dispose()


async def add_job(factory, job_id="job", **kwargs):
    async with factory() as session:
        session.add(JobRecord(
            id=job_id,
            company_id=1,
            event_id=1,
            **kwargs,
        ))
        await session.commit()


async def status(factory, job_id="job"):
    async with factory() as session:
        return (
            await session.get(JobRecord, job_id)
        ).status


@pytest.mark.asyncio
async def test_inline_pipeline_is_idempotent_and_persists_trace(
    worker_db,
):
    factory, ml, bus = worker_db
    await add_job(factory)

    await tasks.run_insight_pipeline("job", 1, 1)
    await tasks.run_insight_pipeline("job", 1, 1)

    assert ml.calls == 1
    assert await status(factory) == "SUCCESS"

    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(Insight)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(AgentTraceStep)
        ) == 1

    assert bus.messages[-1]["event"] == "completed"

    step = next(
        item for item in bus.messages
        if item["event"] == "step"
    )
    assert step["timestamp"].endswith("+00:00")


@pytest.mark.asyncio
async def test_two_claimers_cannot_execute_same_job(worker_db):
    factory, _, _ = worker_db
    await add_job(factory)

    jobs = await asyncio.gather(
        DatabaseJobQueue(factory).claim(),
        DatabaseJobQueue(factory).claim(),
    )

    assert sum(job is not None for job in jobs) == 1
    assert await status(factory) == "PROCESSING"


@pytest.mark.asyncio
async def test_database_mode_leaves_work_for_external_process(
    worker_db, monkeypatch,
):
    factory, ml, _ = worker_db
    await add_job(factory)

    monkeypatch.setattr(
        worker_settings, "mode", "database"
    )

    await tasks.run_insight_pipeline("job", 1, 1)

    assert ml.calls == 0
    assert await status(factory) == "PENDING"

    job = await DatabaseJobQueue(factory).claim()
    await tasks.execute_job(job)

    assert ml.calls == 1
    assert await status(factory) == "SUCCESS"


@pytest.mark.asyncio
async def test_broker_failure_does_not_discard_saved_analysis(
    worker_db,
):
    factory, _, bus = worker_db
    await add_job(factory)
    bus.fail = True

    await tasks.run_insight_pipeline("job", 1, 1)

    assert await status(factory) == "SUCCESS"


@pytest.mark.asyncio
async def test_engine_failure_persists_failed_status(worker_db):
    factory, ml, _ = worker_db
    await add_job(factory)
    ml.fail = True

    await tasks.run_insight_pipeline("job", 1, 1)

    assert await status(factory) == "FAILED"

    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(Insight)
        ) == 0


@pytest.mark.asyncio
async def test_engine_timeout_is_persisted(
    worker_db, monkeypatch,
):
    factory, ml, _ = worker_db
    await add_job(factory)
    ml.delay = 1

    monkeypatch.setattr(
        worker_settings, "timeout_seconds", 0.01
    )

    await tasks.run_insight_pipeline("job", 1, 1)

    assert await status(factory) == "FAILED"

    async with factory() as session:
        job = await session.get(JobRecord, "job")
        assert job.error_message == "Analysis timeout"


@pytest.mark.asyncio
async def test_status_failure_after_commit_keeps_success(
    worker_db, monkeypatch,
):
    factory, ml, _ = worker_db
    await add_job(factory)

    finish = tasks._finish
    first = True

    async def fail_once(*args, **kwargs):
        nonlocal first

        if first:
            first = False
            raise RuntimeError("Test status write failure")

        return await finish(*args, **kwargs)

    monkeypatch.setattr(tasks, "_finish", fail_once)

    await tasks.run_insight_pipeline("job", 1, 1)

    assert ml.calls == 1
    assert await status(factory) == "SUCCESS"


@pytest.mark.asyncio
async def test_stale_jobs_recover_from_database_without_regeneration(
    worker_db,
):
    factory, ml, _ = worker_db
    old = utc_now() - timedelta(minutes=11)

    await add_job(
        factory, "empty",
        status="PROCESSING", updated_at=old,
    )
    await add_job(
        factory, "saved",
        status="PROCESSING", updated_at=old,
    )
    await add_job(
        factory, "active",
        status="PROCESSING",
    )

    async with factory() as session:
        await InsightRepository(session).save_insight(
            job_id="saved",
            company_id=1,
            event_id=1,
            ml_result=result(),
        )

    await DatabaseJobQueue(factory).expire_stale()

    assert await status(factory, "empty") == "FAILED"
    assert await status(factory, "saved") == "SUCCESS"
    assert await status(factory, "active") == "PROCESSING"
    assert ml.calls == 0


def test_mock_engine_requires_explicit_selection(monkeypatch):
    monkeypatch.setattr(
        worker_settings, "engine_factory", None
    )

    with pytest.raises(
        ValueError, match="WORKER_ENGINE_FACTORY"
    ):
        load_engine(False)

    assert load_engine(True) is not None


@pytest.mark.asyncio
async def test_cancellation_after_commit_preserves_success(
    worker_db, monkeypatch,
):
    factory, ml, _ = worker_db
    await add_job(factory)

    finish = tasks._finish
    first = True

    async def cancel_once(*args, **kwargs):
        nonlocal first

        if first:
            first = False
            raise asyncio.CancelledError()

        return await finish(*args, **kwargs)

    monkeypatch.setattr(tasks, "_finish", cancel_once)

    with pytest.raises(asyncio.CancelledError):
        await tasks.run_insight_pipeline("job", 1, 1)

    assert ml.calls == 1
    assert await status(factory) == "SUCCESS"