import hashlib
from datetime import date, datetime, time, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.models.bank_document import BankDocument, BankDocumentPage
from backend.app.db.repositories.bank_analytics_repo import BankAnalyticsRepository
from backend.app.ingestion.analytics_source import document_payload, event_cutoff_day
from backend.app.schemas.event import NewsArticleResponse


@pytest_asyncio.fixture
async def search_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[BankDocument.__table__, BankDocumentPage.__table__],
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def add_document(session, key="a", *, published_on=date(2026, 9, 11),
                       text="КЛЮЧЕВУЮ СТАВКУ сохранили. Инфляция ещё высокая.",
                       historical=False, title="Ключевая ставка"):
    document = BankDocument(
        id=key * 64, source_file=f"{key}.pdf", title=title,
        category="Макроэкономика", source_origin="hackathon_bank_corpus",
        published_on=published_on, historical=historical, page_count=1,
        extraction_hash=key * 64, extraction_metadata={}, empty_pages=[],
        requires_review=True, provenance_status="unverified",
    )
    session.add(document)
    await session.flush()
    page = BankDocumentPage(
        document_id=document.id, page_number=1, text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), extraction_method="ocr",
    )
    session.add(page)
    await session.commit()
    return document, page


@pytest.mark.asyncio
async def test_russian_case_endings_and_word_boundaries(search_session):
    await add_document(search_session)
    await add_document(search_session, "b", title="Поставки", text="Поставки оборудования выросли.")
    hits = await BankAnalyticsRepository(search_session).search(
        "ключевая ставка", as_of=date(2026, 9, 15), reviewed_only=False
    )
    assert len(hits) == 1 and hits[0].document_id == "a" * 64
    assert hits[0].text.startswith("КЛЮЧЕВУЮ")
    assert hits[0].provenance_status == "unverified"


@pytest.mark.asyncio
async def test_future_unknown_dates_and_historical_documents_are_filtered(search_session):
    await add_document(search_session)
    await add_document(search_session, "b", published_on=date(2027, 1, 1))
    await add_document(search_session, "c", published_on=None)
    await add_document(search_session, "d", published_on=date(2021, 4, 15), historical=True)
    repository = BankAnalyticsRepository(search_session)
    hits = await repository.search("ставка", as_of=date(2026, 9, 15), limit=10, reviewed_only=False)
    assert [hit.document_id for hit in hits] == ["a" * 64]
    hits = await repository.search(
        "ставка", as_of=date(2026, 9, 15), limit=10, include_historical=True, reviewed_only=False
    )
    assert {hit.document_id for hit in hits} == {"a" * 64, "d" * 64}


@pytest.mark.asyncio
async def test_relevance_ranks_before_recency_and_results_are_limited(search_session):
    await add_document(search_session, published_on=date(2026, 9, 10))
    await add_document(search_session, "b", published_on=date(2026, 9, 14), text="Ставку сохранили.")
    hits = await BankAnalyticsRepository(search_session).search(
        "ключевая ставка", as_of=date(2026, 9, 15), limit=1, reviewed_only=False
    )
    assert len(hits) == 1 and hits[0].document_id == "a" * 64


@pytest.mark.asyncio
async def test_only_best_page_of_each_document_is_returned(search_session):
    document, page = await add_document(search_session)
    document.page_count = 2
    search_session.add(BankDocumentPage(
        document_id=document.id, page_number=2, text=page.text,
        text_sha256=page.text_sha256, extraction_method="ocr",
    ))
    await search_session.commit()