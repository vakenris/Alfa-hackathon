"""Bounded lexical retrieval over stored PDF pages, without external services."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.bank_document import BankDocument, BankDocumentPage


_SUFFIXES = (
    "иями", "ого", "ему", "ому", "ыми", "ими", "иях", "ения", "ение",
    "ией", "ями", "ами", "ия", "ии", "ий", "ый", "ой", "ая", "яя",
    "ое", "ее", "ые", "ие", "ых", "их", "ам", "ям", "ах", "ях",
    "ов", "ев", "ом", "ем", "ую", "юю", "ы", "и", "а", "я", "у", "ю", "е", "о",
)

_STOP_WORDS = {
    "это", "для", "при", "как", "что", "или", "после", "перед", "между",
    "через", "также", "может", "могут", "будет", "года", "году", "год",
    "банк", "банка", "альфа", "обзор", "аналитика", "новости", "новость",
    "компания", "компании", "the", "and", "for", "with", "from",
}


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


_STOP_TERMS = {_stem(word) for word in _STOP_WORDS}


def page_is_reviewed(document, page) -> bool:
    reviews = document.extraction_metadata.get("page_reviews", {})
    review = (
        reviews.get(str(page.page_number), {})
        if isinstance(reviews, dict)
        else {}
    )

    return (
        document.source_origin == "hackathon_bank_corpus"
        and isinstance(review, dict)
        and review.get("extraction_hash") == document.extraction_hash
        and review.get("text_sha256") == page.text_sha256
        and hashlib.sha256(page.text.encode("utf-8")).hexdigest()
        == page.text_sha256
    )


def search_terms(text: str) -> set[str]:
    normalized = (
        unicodedata.normalize("NFKC", text)
        .casefold()
        .replace("ё", "е")
    )
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)

    return {
        _stem(word)
        for word in words
        if (len(word) >= 3 or word == "цб")
        and not word.isdecimal()
        and _stem(word) not in _STOP_TERMS
    }


@dataclass(frozen=True)
class BankSearchHit:
    page_id: int
    document_id: str
    source_file: str
    title: str
    category: str
    published_on: date
    page_number: int
    text: str
    text_sha256: str
    extraction_hash: str
    provenance_status: str
    score: int


class BankAnalyticsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search(
        self,
        query: str,
        *,
        as_of: date,
        limit: int = 3,
        candidate_limit: int = 300,
        include_historical: bool = False,
        reviewed_only: bool = True,
    ) -> list[BankSearchHit]:
        if not isinstance(query, str):
            raise ValueError("Поисковый запрос должен быть строкой")

        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit должен быть от 1 до 20")

        if type(candidate_limit) is not int or not 1 <= candidate_limit <= 300:
            raise ValueError("candidate_limit должен быть от 1 до 300")

        if type(as_of) is not date:
            raise ValueError("as_of должен быть датой")

        terms = set(sorted(search_terms(query[:2048]))[:12])
        if not terms:
            return []

        statement = (
            select(BankDocument, BankDocumentPage)
            .join(
                BankDocumentPage,
                BankDocumentPage.document_id == BankDocument.id,
            )
            .where(
                BankDocument.source_origin == "hackathon_bank_corpus",
                BankDocument.published_on.is_not(None),
                BankDocument.published_on <= as_of,
            )
        )

        if not include_historical:
            statement = statement.where(
                BankDocument.historical.is_(False)
            )

        statement = (
            statement
            .order_by(
                BankDocument.published_on.desc(),
                BankDocument.id,
                BankDocumentPage.page_number,
            )
            .limit(candidate_limit)
        )

        rows = (await self.session.execute(statement)).all()
        hits: list[BankSearchHit] = []

        for document, page in rows:
            actual_hash = hashlib.sha256(
                page.text.encode("utf-8")
            ).hexdigest()

            if actual_hash != page.text_sha256:
                raise ValueError(
                    f"Нарушена целостность страницы {page.id}"
                )

            verified = page_is_reviewed(document, page)
            if reviewed_only and not verified:
                continue

            body_matches = terms & search_terms(page.text[:50_000])
            if not body_matches:
                continue

            title_matches = terms & search_terms(document.title)

            hits.append(
                BankSearchHit(
                    page_id=page.id,
                    document_id=document.id,
                    source_file=document.source_file,
                    title=document.title,
                    category=document.category,
                    published_on=document.published_on,
                    page_number=page.page_number,
                    text=page.text,
                    text_sha256=page.text_sha256,
                    extraction_hash=document.extraction_hash,
                    provenance_status=(
                        "verified" if verified else "unverified"
                    ),
                    score=(
                        5 * len(body_matches)
                        + 3 * len(title_matches)
                    ),
                )
            )

        hits.sort(
            key=lambda hit: (
                -hit.score,
                -hit.published_on.toordinal(),
                hit.document_id,
                hit.page_number,
            )
        )

        selected: list[BankSearchHit] = []
        seen_documents: set[str] = set()

        for hit in hits:
            if hit.document_id in seen_documents:
                continue

            selected.append(hit)
            seen_documents.add(hit.document_id)

            if len(selected) == limit:
                break

        # Возвращаем список даже тогда, когда ничего не найдено.
        # Эта строка находится ВНЕ обоих циклов.
        return selected

    async def get_document(self, document_id: str):
        document = await self.session.get(BankDocument, document_id)
        if document is None:
            raise ValueError("Документ не найден")

        pages = list(
            (
                await self.session.scalars(
                    select(BankDocumentPage)
                    .where(
                        BankDocumentPage.document_id == document_id
                    )
                    .order_by(BankDocumentPage.page_number)
                )
            ).all()
        )

        return document, pages

    async def mark_reviewed(
        self,
        document_id: str,
        extraction_hash: str,
        page_numbers: list[int],
    ) -> dict:
        """Record manual comparison of explicitly selected PDF pages and OCR."""
        try:
            if (
                not page_numbers
                or any(
                    type(number) is not int or number < 1
                    for number in page_numbers
                )
                or len(page_numbers) != len(set(page_numbers))
            ):
                raise ValueError(
                    "Нужен непустой список уникальных номеров страниц"
                )

            document, pages = await self.get_document(document_id)

            if document.extraction_hash != extraction_hash:
                raise ValueError(
                    "Версия извлечённого текста изменилась"
                )

            if document.source_origin != "hackathon_bank_corpus":
                raise ValueError(
                    "Документ не относится к банковскому корпусу"
                )

            expected_numbers = list(
                range(1, document.page_count + 1)
            )
            actual_numbers = [
                page.page_number for page in pages
            ]

            if actual_numbers != expected_numbers:
                raise ValueError(
                    "Нарушена последовательность страниц документа"
                )

            selected = [
                page
                for page in pages
                if page.page_number in page_numbers
            ]

            if len(selected) != len(page_numbers):
                raise ValueError(
                    "Указанной страницы нет в документе"
                )

            existing = document.extraction_metadata.get(
                "page_reviews", {}
            )
            if not isinstance(existing, dict):
                raise ValueError(
                    "Повреждены метаданные сверки страниц"
                )

            reviews = dict(existing)

            for page in selected:
                if not page.text.strip():
                    raise ValueError(
                        f"Страница {page.page_number} пустая"
                    )

                actual_hash = hashlib.sha256(
                    page.text.encode("utf-8")
                ).hexdigest()

                if actual_hash != page.text_sha256:
                    raise ValueError(
                        f"Нарушена целостность страницы {page.id}"
                    )

                reviews[str(page.page_number)] = {
                    "extraction_hash": extraction_hash,
                    "text_sha256": page.text_sha256,
                    "reviewed_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }

            document.extraction_metadata = {
                **document.extraction_metadata,
                "page_reviews": reviews,
            }

            reviewed_pages = [
                page.page_number
                for page in pages
                if page_is_reviewed(document, page)
            ]
            pending_pages = [
                page.page_number
                for page in pages
                if page.page_number not in reviewed_pages
            ]

            document.requires_review = bool(pending_pages)
            document.provenance_status = (
                "unverified" if pending_pages else "verified"
            )

            await self.session.commit()

            return {
                "document_id": document_id,
                "reviewed_pages": reviewed_pages,
                "pending_pages": pending_pages,
                "document_provenance_status": (
                    document.provenance_status
                ),
            }

        except Exception:
            await self.session.rollback()
            raise