"""Validate a declared PDF corpus and atomically store its extracted pages."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.bank_document import BankDocument, BankDocumentPage


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentSpec(StrictModel):
    source_file: str = Field(min_length=1, max_length=255)
    source_sha256: Sha256
    title: str = Field(min_length=1, max_length=512)
    category: str = Field(min_length=1, max_length=128)
    published_on: date | None
    publication_period: str | None = Field(
        default=None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"
    )
    historical: bool = False

    @field_validator("source_file")
    @classmethod
    def flat_pdf_name(cls, value):
        if any(char in value for char in "/\\:\0") or not value.lower().endswith(".pdf"):
            raise ValueError("Нужно имя PDF без пути к папке")
        return value

    @field_validator("published_on", mode="before")
    @classmethod
    def publication_date(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Дата должна иметь формат YYYY-MM-DD или null")
        return date.fromisoformat(value)


class Manifest(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    source_origin: Literal["hackathon_bank_corpus"]
    documents: list[DocumentSpec] = Field(min_length=1, max_length=1000)


class ExtractedPage(StrictModel):
    page_number: int = Field(ge=1, le=300)
    text: str = Field(max_length=1_000_000)
    text_sha256: Sha256
    extraction_method: Literal["native", "ocr"]


class Extraction(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    source_file: str
    source_sha256: Sha256
    page_count: int = Field(ge=1, le=300)
    extractor: dict[str, Any]
    requires_review: bool
    empty_pages: list[int]
    pages: list[ExtractedPage] = Field(min_length=1, max_length=300)


@dataclass
class AnalyticsReport:
    documents: int = 0
    pages: int = 0
    inserted_documents: int = 0
    inserted_pages: int = 0
    skipped_documents: int = 0
    undated_documents: list[str] = field(default_factory=list)
    historical_documents: list[str] = field(default_factory=list)
    review_required_documents: list[str] = field(default_factory=list)


@dataclass
class PreparedDocument:
    values: dict
    pages: list[dict]


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющийся ключ JSON: {key}")
        result[key] = value
    return result


def _load_json(path: Path, model):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"JSON превышает 32 МБ: {path.name}")
    with path.open(encoding="utf-8-sig") as stream:
        data = json.load(stream, object_pairs_hook=_unique_keys)
    try:
        return model.model_validate(data)
    except ValidationError as error:
        issue = error.errors(include_input=False)[0]
        location = ".".join(map(str, issue["loc"]))
        raise ValueError(f"{path.name}: {location}: {issue['msg']}") from error


def _contained_file(directory: Path, name: str) -> Path:
    root = directory.resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError(f"Файл отсутствует или находится вне папки: {name}")
    return path


def read_analytics(
    manifest_path: Path,
    pdf_dir: Path,
    extracted_dir: Path,
) -> tuple[list[PreparedDocument], AnalyticsReport]:
    manifest = _load_json(manifest_path, Manifest)
    report = AnalyticsReport(documents=len(manifest.documents))
    prepared = []
    seen_hashes, seen_names = set(), set()

    for spec in manifest.documents:
        if spec.source_sha256 in seen_hashes or spec.source_file in seen_names:
            raise ValueError(f"Документ повторяется в манифесте: {spec.source_file}")
        seen_hashes.add(spec.source_sha256)
        seen_names.add(spec.source_file)

        pdf = _contained_file(pdf_dir, spec.source_file)
        with pdf.open("rb") as stream:
            actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_hash != spec.source_sha256:
            raise ValueError(f"Хеш PDF не совпадает с манифестом: {spec.source_file}")

        payload = _load_json(
            _contained_file(extracted_dir, spec.source_sha256 + ".json"), Extraction
        )
        if payload.source_sha256 != spec.source_sha256 or payload.source_file != spec.source_file:
            raise ValueError(f"JSON относится к другому PDF: {spec.source_file}")
        if [p.page_number for p in payload.pages] != list(range(1, payload.page_count + 1)):
            raise ValueError(f"Нарушены число или порядок страниц: {spec.source_file}")
        if payload.empty_pages != [p.page_number for p in payload.pages if not p.text.strip()]:
            raise ValueError(f"Неверный список пустых страниц: {spec.source_file}")
        if not any(any(c.isalnum() for c in p.text) for p in payload.pages):
            raise ValueError(f"Документ не содержит распознанного текста: {spec.source_file}")
        for page in payload.pages:
            if hashlib.sha256(page.text.encode("utf-8")).hexdigest() != page.text_sha256:
                raise ValueError(f"Изменён текст страницы {page.page_number}: {spec.source_file}")
        if any(p.extraction_method == "ocr" for p in payload.pages) and not payload.requires_review:
            raise ValueError(f"Для OCR обязателен requires_review: {spec.source_file}")

        canonical = json.dumps(
            {
                "metadata": spec.model_dump(mode="json"),
                "source_origin": manifest.source_origin,
                "extraction": payload.model_dump(mode="json"),
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        needs_review = payload.requires_review or bool(payload.empty_pages)
        prepared.append(PreparedDocument(
            values={
                "id": spec.source_sha256,
                "source_file": spec.source_file,
                "title": spec.title,
                "category": spec.category,
                "source_origin": manifest.source_origin,
                "published_on": spec.published_on,
                "publication_period": spec.publication_period,
                "historical": spec.historical,
                "page_count": payload.page_count,
                "extraction_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "extraction_metadata": payload.extractor,
                "empty_pages": payload.empty_pages,
                "requires_review": needs_review,
                "provenance_status": "unverified",
            },
            pages=[
                {"document_id": spec.source_sha256, **page.model_dump()}
                for page in payload.pages
            ],
        ))
        report.pages += payload.page_count
        if spec.published_on is None:
            report.undated_documents.append(spec.source_file)
        if spec.historical:
            report.historical_documents.append(spec.source_file)
        if needs_review:
            report.review_required_documents.append(spec.source_file)

    return prepared, report


async def save_analytics(
    session: AsyncSession,
    documents: list[PreparedDocument],
    report: AnalyticsReport,
) -> None:
    inserted_documents = inserted_pages = skipped = 0
    try:
        for document in documents:
            existing = await session.get(BankDocument, document.values["id"])
            if existing is not None:
                if existing.extraction_hash != document.values["extraction_hash"]:
                    raise ValueError(
                        "Для уже загруженного PDF изменились текст или метаданные: "
                        + document.values["source_file"]
                    )
                skipped += 1
                continue
            session.add(BankDocument(**document.values))
            await session.flush()
            await session.execute(insert(BankDocumentPage), document.pages)
            inserted_documents += 1
            inserted_pages += len(document.pages)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    report.inserted_documents = inserted_documents
    report.inserted_pages = inserted_pages
    report.skipped_documents = skipped