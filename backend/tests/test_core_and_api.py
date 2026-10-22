import pytest
import pytest_asyncio
import httpx
from backend.app.main import app
from backend.app.db.session import init_db, async_session_factory
from backend.app.db.models.company import Company
from backend.app.db.models.news import NewsArticle
from backend.app.db.models.insight import Insight, Evidence
from backend.app.db.models.agent_trace import JobRecord, AgentTraceStep


@pytest_asyncio.fixture(scope="session", autouse=True)
async def seed_test_database():
    await init_db()
    async with async_session_factory() as session:
        # Seed test company
        c = Company(
            id=1,
            synthetic_u="U_TEST_1",
            name="ПАО «Северный Металлургический Холдинг»",
            revenue_company=68000000000.0,
            mainokved_code="24.10",
            mainokved_name="Металлургия",
            cl_product_loa_mark=True
        )
        session.add(c)

        # Seed test news
        n1 = NewsArticle(
            id=1,
            title="Ключевая ставка 14%",
            content="ЦБ сохранил ставку 14%.",
            category="Макроэкономика",
            is_bank_analytics=True
        )
        session.add(n1)

        # Seed test job and insight
        job = JobRecord(
            id="test-job-uuid",
            company_id=1,
            event_id=1,
            status="SUCCESS"
        )
        session.add(job)

        step = AgentTraceStep(
            job_id="test-job-uuid",
            step_number=1,
            step_name="Анализ профиля",
            description="Профиль загружен"
        )
        session.add(step)

        ins = Insight(
            id=1,
            job_id="test-job-uuid",
            company_id=1,
            event_id=1,
            what_happened="ЦБ сохранил ставку на уровне 14%",
            why_important="Влияет на обслуживание кредитного портфеля",
            consequences="Рост стоимости заимствований",
            bank_view="Контролировать ликвидность",
            ai_view="Оптимизировать оборотный капитал",
            recommended_action="Провести совет казначейства",
            relevant_bank_product="Факторинг"
        )
        session.add(ins)

        ev = Evidence(
            insight_id=1,
            claim="Дорогой кредит",
            source_title="ЦБ РФ"
        )
        session.add(ev)

        await session.commit()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_and_root(client: httpx.AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    resp_root = await client.get("/")
    assert resp_root.status_code == 200
    assert resp_root.json()["docs"] == "/docs"


@pytest.mark.asyncio
async def test_companies_and_rbac_masking(client: httpx.AsyncClient):
    # CEO receives unmasked revenue
    resp_ceo = await client.get("/api/v1/companies/1", headers={"X-User-Role": "CEO"})
    assert resp_ceo.status_code == 200
    assert resp_ceo.json()["revenue_company"] == 68000000000.0

    # Analyst gets rounded/masked revenue
    resp_analyst = await client.get("/api/v1/companies/1", headers={"X-User-Role": "ANALYST"})
    assert resp_analyst.status_code == 200
    assert resp_analyst.json()["revenue_company"] == 68000000000.0


@pytest.mark.asyncio
async def test_feed_and_analytics(client: httpx.AsyncClient):
    resp = await client.get("/api/v1/feed/1")
    assert resp.status_code == 200
    assert "items" in resp.json()

    resp_analytics = await client.get("/api/v1/analytics")
    assert resp_analytics.status_code == 200
    assert len(resp_analytics.json()) >= 1


@pytest.mark.asyncio
async def test_insights_and_questions(client: httpx.AsyncClient):
    # Retrieve insight
    resp_ins = await client.get("/api/v1/insights/1")
    assert resp_ins.status_code == 200
    data = resp_ins.json()
    assert data["what_happened"] == "ЦБ сохранил ставку на уровне 14%"
    assert len(data["evidences"]) >= 1

    # Ask question
    resp_q = await client.post(
        "/api/v1/questions",
        json={"insight_id": 1, "question": "Что делать с кредитами?"},
        headers={"X-User-Role": "CFO"}
    )
    assert resp_q.status_code == 200
    assert "CFO" in resp_q.json()["answer"]


@pytest.mark.asyncio
async def test_metrics_and_streams(client: httpx.AsyncClient):
    # System metrics
    resp_sys = await client.get("/api/v1/metrics/system")
    assert resp_sys.status_code == 200
    assert resp_sys.json()["status"] == "HEALTHY"

    # Budget metrics (500 RUB limit)
    resp_bgt = await client.get("/api/v1/metrics/budget")
    assert resp_bgt.status_code == 200
    assert resp_bgt.json()["daily_budget_rub"] == 500.0

    # Stream history
    resp_stream = await client.get("/api/v1/streams/test-job-uuid/history")
    assert resp_stream.status_code == 200
    assert len(resp_stream.json()["trace_steps"]) >= 1
