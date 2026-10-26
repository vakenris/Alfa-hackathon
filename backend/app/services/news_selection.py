"""Bounded rule-based selection of external news; no model calls or jobs."""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.company import Company
from backend.app.db.models.news import NewsArticle
from backend.app.schemas.event import NewsArticleResponse


CANDIDATE_LIMIT = 1000

SUFFIXES = (
    "иями", "ого", "ому", "ему", "ыми", "ими", "иях", "ями", "ами",
    "ия", "ии", "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие",
    "ых", "их", "ов", "ев", "ом", "ем", "ую",
    "ы", "и", "а", "я", "у", "ю", "е", "о",
)


def words(text: str) -> set[str]:
    normalized = (
        unicodedata.normalize("NFKC", text)
        .casefold()
        .replace("ё", "е")
    )
    result = set()

    for word in re.findall(r"[^\W\d_]{3,}", normalized):
        for suffix in SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[:-len(suffix)]
                break
        result.add(word)

    return result


GENERIC = words(
    "деятельность производство торговля прочие услуги изделия "
    "компания общество ограниченной ответственностью"
)


def relevance(company: Company, article: NewsArticle) -> int:
    text = " ".join((
        article.title,
        article.summary or "",
        article.content[:20_000],
    ))
    terms = words(text)
    industry = words(company.mainokved_name or "") - GENERIC

    score = 3 * min(len(industry & terms), 3)

    prefix = article.target_okved_prefix or ""
    code = company.mainokved_code or ""
    pattern = r"[0-9]{2}(?:\.[0-9]{1,2}){0,2}"

    if (
        re.fullmatch(pattern, prefix)
        and re.fullmatch(pattern, code)
        and code.startswith(prefix)
    ):
        score += 12

    def has(prefix: str) -> bool:
        return any(term.startswith(prefix) for term in terms)

    interest = (
        has("ставк")
        and (has("ключев") or has("процентн"))
    )
    if interest and (
        company.cl_product_loa_mark
        or company.cl_product_dep_mark
    ):
        score += 5

    currency = (
        has("валют")
        or (has("курс") and (has("рубл") or has("юан")))
    )
    if company.fts_flag and currency:
        score += 5

    if company.cl_product_lea_mark and has("лизинг"):
        score += 4

    if company.factoring_mark and has("факторинг"):
        score += 4

    if company.cl_product_zpp_mark and (
        has("зарплат") or has("заработн")
    ):
        score += 3

    # Geography refines an existing business match.
    geography = (
        words(company.city or "")
        | words(company.region or "")
    )
    if score and geography & terms:
        score += 2

    return score


class NewsSelectionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_for_company(
        self,
        company_id: int,
        *,
        limit: int = 10,
        days: int = 90,
        as_of: datetime | None = None,
    ) -> list[NewsArticleResponse]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit должен быть от 1 до 50")

        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("days должен быть от 1 до 365")

        cutoff = as_of or datetime.now(timezone.utc)
        cutoff = (
            cutoff.replace(tzinfo=timezone.utc)
            if cutoff.tzinfo is None
            else cutoff.astimezone(timezone.utc)
        ).replace(tzinfo=None)

        company = await self.session.get(Company, company_id)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail="Company not found",
            )

        statement = (
            select(NewsArticle)
            .where(
                NewsArticle.is_bank_analytics.is_(False),
                NewsArticle.published_at <= cutoff,
                NewsArticle.published_at
                >= cutoff - timedelta(days=days),
            )
            .order_by(
                NewsArticle.published_at.desc(),
                NewsArticle.id.desc(),
            )
            .limit(CANDIDATE_LIMIT)
        )

        rows = (
            await self.session.scalars(statement)
        ).all()

        ranked = []
        for row in rows:
            score = relevance(company, row)
            if score > 0:
                ranked.append((score, row))

        ranked.sort(
            key=lambda item: (
                item[0],
                item[1].published_at,
                -item[1].id,
            ),
            reverse=True,
        )

        return [
            NewsArticleResponse.model_validate(row).model_copy(
                update={"relevance_score": float(score)}
            )
            for score, row in ranked[:limit]
        ]