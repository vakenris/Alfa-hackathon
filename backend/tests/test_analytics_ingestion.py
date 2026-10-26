import hashlib
import json

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.models.bank_document import BankDocument, BankDocumentPage
from backend.app.ingestion.analytics import read_analytics, save_analytics


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_corpus(tmp_path, count=1):
    pdfs, extracted = tmp_path / "pdfs", tmp_path / "extracted"
    pdfs.mkdir()
    extracted.mkdir()
    specs, payloads = [], []
    for number in range(count):
        name = f"report-{number}.pdf"
        content = f"synthetic PDF bytes {number}".encode()
        digest = hashlib.sha256(content).hexdigest()
        (pdfs / name).write_bytes(content)
        text = "Ключевая ставка не изменилась.\n"
        payload = {
            "schema_version": 1, "source_file": name, "source_sha256": digest,
            "page_count": 1, "extractor": {"ocr_engine": "test"},
            "requires_review": True, "empty_pages": [],
            "pages": [{
                "page_number": 1, "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "extraction_method": "ocr",
            }],
        }
        write_json(extracted / f"{digest}.json", payload)
        specs.append({
            "source_file": name, "source_sha256": digest,
            "title": f"Тестовый обзор {number}", "category": "Макроэкономика",
            "published_on": None if number == 0 else "2021-04-15",
            "historical": number > 0,
        })
        payloads.append(payload)
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {
        "schema_version": 1, "source_origin": "hackathon_bank_corpus",
        "documents": specs,
    })
    return manifest, pdfs, extracted, specs, payloads


@pytest_asyncio.fixture
async def analytics_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


def test_validation_keeps_unknown_dates_historical_flags_and_raw_text(tmp_path):
    manifest, pdfs, extracted, specs, payloads = make_corpus(tmp_path, 2)
    documents, report = read_analytics(manifest, pdfs, extracted)
    assert report.documents == 2 and report.pages == 2
    assert report.undated_documents == [specs[0]["source_file"]]
    assert report.historical_documents == [specs[1]["source_file"]]
    assert report.inserted_documents == 0
    assert documents[0].values["published_on"] is None
    assert documents[0].values["provenance_status"] == "unverified"
    assert documents[0].pages[0]["text"] == payloads[0]["pages"][0]["text"]


@pytest.mark.parametrize("change", ["pdf", "text", "number", "count", "review", "duplicates"])
def test_invalid_or_changed_sources_are_rejected(tmp_path, change):
    manifest, pdfs, extracted, specs, payloads = make_corpus(tmp_path)
    payload = payloads[0]
    if change == "pdf":
        (pdfs / specs[0]["source_file"]).write_bytes(b"changed PDF")
    elif change == "text":
        payload["pages"][0]["text"] = "Изменённый текст"
    elif change == "number":
        payload["pages"][0]["page_number"] = 2
    elif change == "count":
        payload["page_count"] = 2
    elif change == "review":
        payload["requires_review"] = False
    else:
        manifest.write_text(
            '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
        )
    write_json(extracted / f"{specs[0]['source_sha256']}.json", payload)
    with pytest.raises(ValueError):
        read_analytics(manifest, pdfs, extracted)


@pytest.mark.asyncio
async def test_replay_does_not_duplicate_documents_or_pages(tmp_path, analytics_session):
    manifest, pdfs, extracted, specs, _ = make_corpus(tmp_path)
    documents, first = read_analytics(manifest, pdfs, extracted)
    await save_analytics(analytics_session, documents, first)
    documents, second = read_analytics(manifest, pdfs, extracted)
    await save_analytics(analytics_session, documents, second)
    assert first.inserted_documents == first.inserted_pages == 1
    assert second.inserted_documents == second.inserted_pages == 0
    assert second.skipped_documents == 1
    assert await analytics_session.scalar(select(func.count()).select_from(BankDocumentPage)) == 1
    stored = await analytics_session.get(BankDocument, specs[0]["source_sha256"])
    assert stored.requires_review is True and stored.provenance_status == "unverified"


@pytest.mark.asyncio
async def test_conflict_rolls_back_new_documents_and_keeps_original_text(tmp_path, analytics_session):
    manifest, pdfs, extracted, specs, payloads = make_corpus(tmp_path, 2)
    documents, report = read_analytics(manifest, pdfs, extracted)
    await save_analytics(analytics_session, [documents[1]], report)
    payloads[1]["pages"][0]["text"] = "Другой результат OCR."
    payloads[1]["pages"][0]["text_sha256"] = hashlib.sha256(
        payloads[1]["pages"][0]["text"].encode()
    ).hexdigest()
    write_json(extracted / f"{specs[1]['source_sha256']}.json", payloads[1])
    documents, report = read_analytics(manifest, pdfs, extracted)
    with pytest.raises(ValueError, match="изменились"):
        await save_analytics(analytics_session, documents, report)
    assert report.inserted_documents == 0
    assert await analytics_session.scalar(select(func.count()).select_from(BankDocument)) == 1
    stored = await analytics_session.scalar(select(BankDocumentPage.text))
    assert stored == "Ключевая ставка не изменилась.\n"