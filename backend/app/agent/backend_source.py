"""Read-only, bounded lexical search over existing bank-analytics articles.

This adapter is not PDF ingestion or RAG. Its coverage is the newest at most 50
articles supplied by a loader, not the entire bank corpus. Articles published after
the event are excluded before top-N; dates break score ties among usable articles;
old publications are not discarded because they can be valid historical sources.
An empty result means no match within that coverage. Loader/repository failures
are deliberately raised, so callers can distinguish a failure from no matches.

Importing this module or building the repository adapter does not import backend
database/settings modules or open a session. Only an explicit search with a
meaningful query/event title invokes the injected loader. No URL is fetched.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
import inspect
import re
import unicodedata
from typing import Any

from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse

from .models import AnalyticsDocument


DocumentLoader = Callable[[int], Awaitable[list[AnalyticsDocument]]]
ProvenanceResolver = Callable[
    [object], Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
]

# A small, explicit vocabulary is intentionally auditable. It supplies lexical
# recall for common Russian inflections and related terms, not semantic search.
_TOPICS: dict[str, tuple[str, ...]] = {
    "monetary": (
        "инфляц", "фондирован", "рефинанс", "кредит", "заимств", "заем", "монетар",
    ),
    "currency": ("валют", "доллар", "юан", "евро", "курсов", "рубл"),
    "logistics": (
        "логист", "фрахт", "контейнер", "перевоз", "транспорт", "портов", "железнодорож",
    ),
    "metals": ("металл", "сталь", "стали", "сталелит", "руда", "руды", "ферросплав"),
    "agriculture": ("аграр", "агро", "зерн", "пшениц", "урожа", "сельскохозяй"),
    "energy": ("нефт", "нефтепр", "энерг", "газов", "электр"),
    "taxation": ("налог", "акциз", "ндс", "пошлин", "таможен"),
    "labor": ("заработ", "зарплат", "кадр", "персонал", "безработ", "труд"),
    "working_capital": ("факторинг", "лизинг", "ликвидност", "дебитор", "оборотн"),
}
_MONETARY_PHRASES = (
    ("ключев", "ставк"), ("процентн", "ставк"), ("центральн", "банк"),
    ("банк", "росси"), ("денежн", "политик"),
)
_SUFFIXES = (
    "ования", "ование", "ирован", "иями", "ями", "ами", "ого", "ему", "ому", "ыми",
    "ими", "иях", "ениях", "ение", "ения", "ией", "ий", "ый", "ой", "ая", "яя",
    "ое", "ее", "ые", "ие", "ых", "их", "ам", "ям", "ах", "ях", "ов", "ев",
    "ом", "ем", "ую", "юю", "ы", "и", "а", "я", "у", "ю", "е", "о",
)
_STOP_WORDS = {
    "для", "или", "как", "при", "это", "этот", "этой", "этого", "есть", "будет",
    "после", "перед", "между", "через", "также", "может", "могут", "свой", "своих",
    "банк", "банка", "банки", "альфа", "новости", "новость", "событие", "обзор",
    "аналитика", "анализ", "позиция", "компания", "компании", "бизнес", "материалы",
    "материал", "новый", "новые", "новая", "рост", "снижение", "повышение", "изменение",
    "ставка", "ставки", "процент", "проценты", "ключевой", "ключевая", "ключевые",
    "прогноз", "рынок", "рынке", "россии", "россия", "года", "году", "год", "рублей",
    "the", "and", "for", "with", "from", "news", "bank", "report", "market",
}
_PROVENANCE_FIELDS = {
    "source_url", "provenance_status", "source_document_id", "source_page", "source_hash",
}


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _stem(token: str) -> str:
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[:-len(suffix)]
    return token


_STOP_STEMS = {_stem(word) for word in _STOP_WORDS}


def _features(text: str) -> tuple[set[str], set[str]]:
    words = set(re.findall(r"[^\W_]+", _normalize(text), flags=re.UNICODE))
    terms = {
        _stem(word) for word in words
        if len(word) >= 4 and not word.isdecimal() and _stem(word) not in _STOP_STEMS
    }
    topics = {
        topic for topic, prefixes in _TOPICS.items()
        if any(word.startswith(prefix) for word in words for prefix in prefixes)
    }
    if any(
        all(any(word.startswith(prefix) for word in words) for prefix in phrase)
        for phrase in _MONETARY_PHRASES
    ):
        topics.add("monetary")
    return terms, topics


def _date_key(value: datetime) -> int:
    # Naive repository timestamps are interpreted as UTC, never host local time.
    utc = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return ((utc.toordinal() * 86400 + utc.hour * 3600 + utc.minute * 60 + utc.second)
            * 1_000_000 + utc.microsecond)


class BackendAnalyticsSource:
    """AnalyticsSourceProtocol implementation with an explicit read boundary.

    ``document_loader(candidate_limit)`` must return articles in newest-first
    order. Only its first ``candidate_limit`` entries are examined. A trusted
    custom loader may supply provenance, but the repository bridge defaults to
    unverified even when ``article.is_bank_analytics`` is true.

    ``coverage`` and ``candidate_limit`` describe the search limitation to the
    caller. Neither a match nor an empty result proves exhaustive corpus search.
    No recency cutoff, embedding generation, filesystem access or URL retrieval
    is performed here. ``company`` is accepted for protocol compatibility; it
    does not expand topical intent merely because a document mentions its sector.
    """

    coverage = "newest_bounded_bank_articles"
    retrieval_method = "lexical_topics_and_terms"
    max_candidates = 50

    def __init__(self, document_loader: DocumentLoader, *, candidate_limit: int = 50):
        if not callable(document_loader):
            raise TypeError("document_loader must be callable")
        if type(candidate_limit) is not int or not 1 <= candidate_limit <= self.max_candidates:
            raise ValueError("candidate_limit must be an integer between 1 and 50")
        self.document_loader = document_loader
        self.candidate_limit = candidate_limit

    @classmethod
    def from_repository_factory(
        cls, session_factory: Callable[[], Any], provenance_resolver: ProvenanceResolver | None = None,
    ) -> "BackendAnalyticsSource":
        """Bridge to NewsRepository using only list_bank_analytics(limit<=50).

        ``session_factory`` supplies an async context manager when search runs.
        ``provenance_resolver(row)`` is an optional trusted, injected mapping
        lookup, sync or async. It must not fetch links from document text. Its
        mapping may contain only AnalyticsDocument provenance fields; content,
        titles, dates and bank flags always come from the repository row.
        Without a resolver no URL, document identity or verified status is
        inferred from the row, its title, source label or bank-analytics flag.
        """
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if provenance_resolver is not None and not callable(provenance_resolver):
            raise TypeError("provenance_resolver must be callable")

        async def load_documents(limit: int) -> list[AnalyticsDocument]:
            # Import intentionally stays inside the explicit search operation.
            from backend.app.db.repositories.news_repo import NewsRepository

            async with session_factory() as session:
                rows = await NewsRepository(session).list_bank_analytics(limit=min(limit, cls.max_candidates))
                if not isinstance(rows, list):
                    raise TypeError("Repository must return a list of bank articles")
                documents = []
                for row in rows[:limit]:
                    metadata: Mapping[str, Any] = {}
                    if provenance_resolver is not None:
                        resolved = provenance_resolver(row)
                        if inspect.isawaitable(resolved):
                            resolved = await resolved
                        if resolved is not None:
                            if not isinstance(resolved, Mapping) or set(resolved) - _PROVENANCE_FIELDS:
                                raise ValueError("Invalid provenance metadata")
                            metadata = resolved
                    documents.append(AnalyticsDocument.model_validate({
                        "article": NewsArticleResponse.model_validate(row), **metadata,
                    }))
                return documents

        return cls(load_documents)

    async def search(
        self, query: str, *, company: CompanyResponse, event: NewsArticleResponse, limit: int,
    ) -> list[AnalyticsDocument]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        intent_terms, intent_topics = _features(query[:2048] + " " + event.title[:2048])
        if not intent_terms and not intent_topics:
            return []

        loaded = await self.document_loader(self.candidate_limit)
        if not isinstance(loaded, list):
            raise TypeError("document_loader must return a list of AnalyticsDocument")
        ranked: list[tuple[int, int, int, AnalyticsDocument]] = []
        seen_ids: set[int] = set()
        for item in loaded[:self.candidate_limit]:
            document = AnalyticsDocument.model_validate(
                item.model_dump(mode="python") if isinstance(item, AnalyticsDocument) else item,
            )
            article = document.article
            if (not article.is_bank_analytics or article.id in seen_ids
                    or _date_key(article.published_at) > _date_key(event.published_at)):
                continue
            seen_ids.add(article.id)
            title_terms, title_topics = _features(article.title[:2048])
            body_terms, body_topics = _features(
                (article.summary or "")[:4000] + " " + article.content[:12000],
            )
            score = (8 * len(intent_topics & (title_topics | body_topics))
                     + 3 * len(intent_terms & title_terms) + len(intent_terms & body_terms))
            if score:
                ranked.append((score, _date_key(article.published_at), article.id, document))
        ranked.sort(key=lambda entry: (-entry[0], -entry[1], entry[2]))
        return [entry[3].model_copy(deep=True) for entry in ranked[:min(limit, self.candidate_limit)]]
