from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.api.deps import get_db
from backend.app.db.base import Base
from backend.app.db.models.agent_trace import JobRecord
from backend.app.db.models.company import Company
from backend.app.db.models.news import NewsArticle
from backend.app.main import app
from backend.app.schemas.event import NewsArticleResponse
from backend.app.services.news_selection import NewsSelectionService


@pytest_asyncio.fixture
async def news_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with async_sessionmaker(
            engine, expire_on_commit=False
        )() as session:
            session.add_all([
                Company(
                    id=1,
                    synthetic_u="U_1",
                    name="Test",
                    mainokved_code="24.10",
                    mainokved_name="Производство стали",
                    cl_product_loa_mark=True,
                ),
                Company(
                    id=2,
                    synthetic_u="U_2",
                    name="Other",
                    mainokved_code="62.01",
                    mainokved_name="Разработка программного обеспечения",
                ),
            ])
            await session.commit()
            yield session
    finally:
        await engine.dispose()


async def add_news(
    session,
    title,
    *,
    target=None,
    bank=False,
    published_at=datetime(2026, 9, 10),
):
    item = NewsArticle(
        title=title,
        content=title,
        category="Тест",
        source="Тестовый источник",
        published_at=published_at,
        target_okved_prefix=target,
        is_bank_analytics=bank,
    )
    session.add(item)
    await session.commit()
    return item.id


@pytest.mark.asyncio
async def test_selection_filters_bank_future_old_and_unrelated_news(
    news_session,
):
    relevant = await add_news(
        news_session, "Экспорт стали вырос", target="24"
    )
    await add_news(news_session, "Экспорт стали вырос", bank=True)
    await add_news(
        news_session,
        "Экспорт стали вырос",
        published_at=datetime(2026, 10, 1),
    )
    await add_news(
        news_session,
        "Экспорт стали вырос",
        published_at=datetime(2025, 1, 1),
    )
    await add_news(
        news_session, "Премьера нового фильма", target="59"
    )

    rows = await NewsSelectionService(news_session).list_for_company(
        1, as_of=datetime(2026, 9, 15)
    )

    assert [row.id for row in rows] == [relevant]
    assert rows[0].content == "Экспорт стали вырос"


@pytest.mark.asyncio
async def test_rank_then_limit_within_candidate_window(news_session):
    best = await add_news(
        news_session,
        "Поддержка сталелитейной отрасли",
        target="24",
    )
    await add_news(
        news_session,
        "Процентная ставка изменена",
        published_at=datetime(2026, 9, 14),
    )

    rows = await NewsSelectionService(news_session).list_for_company(
        1, limit=1, as_of=datetime(2026, 9, 15)
    )

    assert [row.id for row in rows] == [best]


@pytest.mark.asyncio
async def test_macro_match_depends_on_company_products(news_session):
    relevant = await add_news(
        news_session, "Ключевая ставка изменена"
    )
    service = NewsSelectionService(news_session)

    rows = await service.list_for_company(
        1, as_of=datetime(2026, 9, 15)
    )
    assert [row.id for row in rows] == [relevant]

    assert await service.list_for_company(
        2, as_of=datetime(2026, 9, 15)
    ) == []


@pytest.mark.asyncio
async def test_no_padding_and_no_single_digit_okved_match(news_session):
    await add_news(news_session, "Премьера фильма", target="2")

    assert await NewsSelectionService(
        news_session
    ).list_for_company(
        1, as_of=datetime(2026, 9, 15)
    ) == []


@pytest_asyncio.fixture
async def news_client(news_session):
    async def override_db():
        yield news_session

    app.dependency_overrides[get_db] = override_db

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_returns_bare_dto_list_and_does_not_schedule_jobs(
    news_client, news_session,
):
    news_id = await add_news(
        news_session, "Экспорт стали вырос", target="24"
    )

    response = await news_client.get(
        "/api/v1/companies/1/news"
        "?as_of=2026-09-15T00:00:00Z&limit=5"
    )

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["id"] == news_id

    article = NewsArticleResponse.model_validate(response.json()[0])
    assert article.is_bank_analytics is False

    assert response.headers["X-News-Candidate-Limit"] == "1000"
    assert await news_session.scalar(
        select(func.count()).select_from(JobRecord)
    ) == 0


@pytest.mark.asyncio
async def test_unknown_company_is_404(news_client):
    response = await news_client.get(
        "/api/v1/companies/999/news"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "days=0",
        "days=366",
        "as_of=bad",
    ],
)
async def test_invalid_parameters_are_422(news_client, query):
    response = await news_client.get(
        "/api/v1/companies/1/news?" + query
    )
    assert response.status_code == 422