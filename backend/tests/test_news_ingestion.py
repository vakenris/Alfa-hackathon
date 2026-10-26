import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.models.company import Company
from backend.app.db.models.news import NewsArticle
from backend.app.db.models.news_source import ExternalNewsSource
from backend.app.ingestion.news import download_feed, parse_feed, save_news
from backend.app.services.news_selection import NewsSelectionService


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)

BODY = (
    "<p>Ключевая ставка не снижена: 14,00% годовых.</p>"
    "<p>Это синтетический текст для проверки импорта, "
    "не настоящая публикация.</p>"
)


def feed(*items):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")

    for values in items:
        item = ET.SubElement(channel, "item")
        data = {
            "title": "Тест: ключевая ставка",
            "description": BODY,
            "link": "https://www.cbr.ru/press/PR/?file=test.htm",
            "pubDate": "Fri, 23 Oct 2026 13:30:00 +0300",
            "category": "Тест",
            **values,
        }

        for key, value in data.items():
            ET.SubElement(item, key).text = value

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )


@pytest_asyncio.fixture
async def news_import_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with async_sessionmaker(
            engine, expire_on_commit=False
        )() as session:
            session.add(Company(
                id=1,
                synthetic_u="U_1",
                name="Test",
                cl_product_loa_mark=True,
            ))
            await session.commit()
            yield session
    finally:
        await engine.dispose()


def test_parser_preserves_decimal_negation_and_utc_date():
    records, report = parse_feed(feed({}), now=NOW)

    assert report.selected_items == 1
    assert report.rejected_items == 0
    assert "не снижена: 14,00%" in records[0]["article"]["content"]
    assert records[0]["article"]["published_at"] == datetime(
        2026, 10, 23, 10, 30
    )
    assert records[0]["content_scope"] == "rss_description"
    assert records[0]["original_html"] == BODY
    assert records[0]["article"]["is_bank_analytics"] is False


def test_parser_reports_bad_future_and_duplicate_items():
    records, report = parse_feed(
        feed(
            {},
            {},
            {"link": "https://example.com/story"},
            {
                "link": "https://www.cbr.ru/press/PR/?file=future.htm",
                "pubDate": "Fri, 23 Oct 2027 13:30:00 +0300",
            },
            {
                "link": "https://www.cbr.ru/press/PR/?file=short.htm",
                "description": "short",
            },
        ),
        now=NOW,
    )

    assert len(records) == 1
    assert report.read_items == 5
    assert report.duplicate_items == 1
    assert report.future_items == 1
    assert report.rejected_items == 2
    assert len(report.errors) == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"not XML",
        b"<html/>",
        b"<rss><channel/></rss>",
        b'<!DOCTYPE rss [<!ENTITY x "x">]><rss><channel/></rss>',
    ],
)
def test_invalid_or_empty_feeds_are_rejected(payload):
    with pytest.raises(ValueError):
        parse_feed(payload, now=NOW)


def test_conflicting_versions_in_one_feed_are_rejected():
    with pytest.raises(ValueError, match="разные тексты"):
        parse_feed(
            feed(
                {},
                {"title": "Изменённый тестовый заголовок"},
            ),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_http_feed_download_and_http_failure():
    payload = feed({})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=payload)
        )
    ) as client:
        assert await download_feed(client) == payload

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503)
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await download_feed(client)


@pytest.mark.asyncio
async def test_import_is_replay_safe_and_available_to_ml_selection(
    news_import_session,
):
    records, report = parse_feed(feed({}), now=NOW)
    await save_news(news_import_session, records, report)

    assert report.inserted == 1
    assert report.skipped == 0

    records, replay = parse_feed(feed({}), now=NOW)
    await save_news(news_import_session, records, replay)

    assert replay.inserted == 0
    assert replay.skipped == 1

    assert await news_import_session.scalar(
        select(func.count()).select_from(NewsArticle)
    ) == 1

    source = await news_import_session.scalar(
        select(ExternalNewsSource)
    )
    assert source.source_url.endswith("file=test.htm")
    assert source.content_scope == "rss_description"

    items = await NewsSelectionService(
        news_import_session
    ).list_for_company(1, as_of=NOW)

    assert len(items) == 1
    assert "14,00%" in items[0].content


@pytest.mark.asyncio
async def test_publisher_revision_rolls_back_the_entire_import(
    news_import_session,
):
    records, report = parse_feed(feed({}), now=NOW)
    await save_news(news_import_session, records, report)

    records, report = parse_feed(
        feed(
            {
                "link": "https://www.cbr.ru/press/PR/?file=new.htm",
                "pubDate": "Sat, 24 Oct 2026 13:30:00 +0300",
            },
            {"title": "Изменённая публикация"},
        ),
        now=NOW,
    )

    with pytest.raises(ValueError, match="Источник изменил"):
        await save_news(news_import_session, records, report)

    assert await news_import_session.scalar(
        select(func.count()).select_from(NewsArticle)
    ) == 1
    assert await news_import_session.scalar(
        select(func.count()).select_from(ExternalNewsSource)
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("content", "Повреждённый текст"),
        ("is_bank_analytics", True),
    ],
)
async def test_modified_database_content_is_rejected(
    news_import_session, field, value,
):
    records, report = parse_feed(feed({}), now=NOW)
    await save_news(news_import_session, records, report)

    article = await news_import_session.scalar(
        select(NewsArticle)
    )
    setattr(article, field, value)
    await news_import_session.commit()

    with pytest.raises(ValueError, match="целостность|Изменены"):
        await save_news(news_import_session, records, report)


@pytest.mark.asyncio
async def test_source_api_returns_link_without_changing_news_list(
    news_import_session,
):
    from backend.app.api.deps import get_db
    from backend.app.main import app

    records, report = parse_feed(feed({}), now=NOW)
    await save_news(news_import_session, records, report)

    article = await news_import_session.scalar(
        select(NewsArticle)
    )
    news_id = article.id

    async def override_db():
        yield news_import_session

    app.dependency_overrides[get_db] = override_db

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/news/{news_id}/source"
            )
            assert response.status_code == 200
            assert response.json()["news_id"] == news_id
            assert response.json()["content_scope"] == "rss_description"

            response = await client.get(
                "/api/v1/news/999/source"
            )
            assert response.status_code == 404

            article.content = "Изменённый текст"
            await news_import_session.commit()

            response = await client.get(
                f"/api/v1/news/{news_id}/source"
            )
            assert response.status_code == 409
    finally:
        app.dependency_overrides.pop(get_db, None)