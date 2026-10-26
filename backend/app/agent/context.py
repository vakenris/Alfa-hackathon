"""Deterministic, bounded inputs and the evidence actually visible to the model."""

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .config import AgentConfig
from .models import AgentContextError, AnalyticsDocument, BusinessContext, Evidence, GroundedSource
from .tools import AnalyticsSourceProtocol
from .backend_source import _features, _normalize, _stem


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@dataclass
class PreparedContext:
    company: CompanyResponse
    event: NewsArticleResponse
    business: BusinessContext
    evidence: dict[str, Evidence]
    sources: list[GroundedSource]
    payload: dict
    warnings: list[str]
    bank_content_hashes: dict[str, str] = field(default_factory=dict)


FEATURE_KEYS = (
    "org_type", "okfs_name", "company_age_years", "amount_workers_fns", "amount_workers_spark", "cl_type",
    "mainokved_code", "mainokved_name", "region", "city", "group_id", "fts_country_name",
    "revenue_company", "cl_product_loa_mark", "cl_product_lea_mark", "cl_product_dep_mark",
    "cl_product_gar_mark", "cl_product_rko_mark", "cl_product_zpp_mark", "factoring_mark", "fts_flag",
)
PAYMENT_SCOPE = (
    "Только переданная выборка; валюта, год, конец периода и полнота неизвестны. "
    "Суммы не являются выручкой. Направления платежей не доказывают роли поставщика и покупателя. "
    "Собственные переводы исключены из концентрации внешних платежей."
)


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    """Keep an original prefix ending at a sentence boundary, never an invented ellipsis."""
    if len(text) <= limit:
        return text, False
    abbreviation = re.compile(r'\b(?:г|гг|тыс|млн|млрд|руб|стр|рис|им|др|п)\.$',re.I)
    ends = [match.end() for match in re.finditer(r'[.!?][»”\"]?(?=\s|$)', text[:limit])
            if not abbreviation.search(text[:match.end()])]
    return (text[:ends[-1]] if ends else ""), True


def _bank_excerpt(text: str, limit: int, query: str) -> tuple[str, bool]:
    """Select one original block, keeping whole sentences and adjacent context.

    This only prepares an already returned document; it does not search another
    corpus. Prefer a whole paragraph. A long paragraph may use whole sentences,
    but the immediately preceding/following sentences must fit too. Otherwise
    omit the excerpt rather than publish a claim with its neighbour cut away.
    Partial-document scope remains explicit; this is not semantic verification.
    """
    if len(text) <= limit:
        return text, False
    paragraphs = []
    sentences = []
    abbreviation = re.compile(r'\b(?:г|гг|тыс|млн|млрд|руб|стр|рис|им|др|п)\.$', re.I)
    for match in re.finditer(r'(.*?)(?:\n[ \t]*\n|\Z)', text, re.S):
        start, end = match.span(1)
        while start < end and text[start].isspace(): start += 1
        while end > start and text[end-1].isspace(): end -= 1
        if start == end: continue
        paragraphs.append((start, end))
        sentence_start = start
        for boundary in re.finditer(r'[.!?][»”\"]?(?=\s|$)', text[start:end]):
            sentence_end = start + boundary.end()
            if abbreviation.search(text[sentence_start:sentence_end]): continue
            sentences.append((sentence_start, sentence_end))
            sentence_start = sentence_end
            while sentence_start < end and text[sentence_start].isspace(): sentence_start += 1
        if sentence_start < end:
            sentences.append((sentence_start, end))
    if not paragraphs or not sentences:
        return "", True
    intent_terms, intent_topics = _features(query[:2048])
    literal_terms = {_stem(word) for word in re.findall(r'[^\W_]+', _normalize(query[:2048]))
                     if len(word) >= 4 and not word.isdecimal()}
    def score(span):
        block = text[span[0]:span[1]]
        terms, topics = _features(block)
        words = {_stem(word) for word in re.findall(r'[^\W_]+', _normalize(block))}
        # Retrieval stopwords deliberately drop "key/rate". Within one already
        # returned document their literal match identifies the relevant sentence
        # more precisely than another sentence merely mentioning inflation.
        return 8 * len(intent_topics & topics) + 3 * len(intent_terms & terms) + 4 * len(literal_terms & words)
    target = max(paragraphs, key=score)
    if target[1] - target[0] > limit:
        target = max((span for span in sentences if target[0] <= span[0] < target[1]), key=score)
    first = next(index for index, span in enumerate(sentences) if span[0] == target[0])
    last = next(index for index, span in enumerate(sentences) if span[1] == target[1])
    first, last = max(0, first-1), min(len(sentences)-1, last+1)
    if sentences[last][1] - sentences[first][0] > limit:
        return "", True
    # Fill remaining space with neighbouring complete sentences, never clauses
    # or a concatenation of independently selected favourable passages.
    while True:
        if first > 0 and sentences[last][1] - sentences[first-1][0] <= limit:
            first -= 1
        elif last+1 < len(sentences) and sentences[last+1][1] - sentences[first][0] <= limit:
            last += 1
        else:
            break
    return text[sentences[first][0]:sentences[last][1]], True


def build_business_context(company: CompanyResponse, counterparties: list[CounterpartyItem]) -> BusinessContext:
    raw = company.model_dump(mode="python")
    known = {}
    for key in FEATURE_KEYS:
        value = raw.get(key)
        valid = value is not None and value is not False and value != ""
        if isinstance(value, str):
            valid = valid and len(value) <= 512
        if key == "revenue_company":
            valid = type(value) in (float, int) and math.isfinite(value) and value > 0 and value != 1000
        if valid:
            known[key] = value
    return BusinessContext(
        known_features=known, unknown_features=[key for key in FEATURE_KEYS if key not in known],
        limitations=[
            "Флаг продукта не доказывает получение кредита, остаток долга, ставку или условия договора. "
            "False и отсутствующий флаг не доказывают отсутствие обязательств.",
            PAYMENT_SCOPE,
            "Год и валюта выручки не заданы DTO; данные группы и рыночная доля не выводятся из профиля. "
            "Страны ФТС не задают направление и объём торговли.",
            *(["Возраст и численность из профиля — исходные категории, не точные значения. "
               "Разные диапазоны ФНС и СПАРК сохраняются отдельно, без усреднения и выбора одного из них."]
              if any(key in known for key in ("company_age_years","amount_workers_fns","amount_workers_spark")) else []),
        ],
        payment_sample_size=len(counterparties),
        self_transfer_rows=sum(item.synthetic_u == company.synthetic_u for item in counterparties),
    )


def _payments(company: CompanyResponse, rows: list[CounterpartyItem]) -> tuple[dict, list[tuple[str, dict]]]:
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        if (type(row.total_amount) not in (int, float) or not math.isfinite(row.total_amount)
                or row.total_amount < 0 or type(row.transaction_count) is not int or row.transaction_count < 0):
            raise AgentContextError("Платёжная выборка содержит недопустимые суммы или счётчики.")
        direction = ("self_transfer" if row.synthetic_u == company.synthetic_u else {
            "PAYER": "incoming", "CUSTOMER": "incoming", "INCOMING": "incoming",
            "BENEFICIARY": "outgoing", "SUPPLIER": "outgoing", "OUTGOING": "outgoing",
        }.get(row.direction.upper(), "unknown"))
        group = groups.setdefault((row.synthetic_u, direction), {
            "synthetic_u": row.synthetic_u, "direction": direction, "amounts": [], "transaction_count": 0,
            "names": set(),
        })
        if row.name:
            group["names"].add(row.name)
        group["amounts"].append(row.total_amount)
        group["transaction_count"] += row.transaction_count
    totals = {}
    top = []
    try:
        for group in groups.values():
            group["total_amount"] = math.fsum(group.pop("amounts"))
            names = group.pop("names")
            group["name"] = next(iter(names)) if len(names) == 1 else None
            group["name_conflict"] = len(names) > 1
        for direction in ("incoming", "outgoing", "unknown", "self_transfer"):
            items = [group for group in groups.values() if group["direction"] == direction]
            amount = math.fsum(item["total_amount"] for item in items)
            totals[direction] = {
                "total_amount": amount, "transaction_count": sum(item["transaction_count"] for item in items),
                "unique_counterparties": len(items),
            }
            if direction != "self_transfer":
                largest = sorted(items, key=lambda value: (-value["total_amount"], value["synthetic_u"]))[:3]
                if direction in ("incoming", "outgoing"):
                    largest_amount = math.fsum(item["total_amount"] for item in largest)
                    totals[direction]["largest_group"] = {
                        "counterparties": len(largest), "total_amount": largest_amount,
                        "share_of_passed_direction_amount": largest_amount / amount if amount else None,
                        "synthetic_us": [item["synthetic_u"] for item in largest],
                    }
                for item in largest:
                    digest = hashlib.sha256(item["synthetic_u"].encode()).hexdigest()[:16]
                    top.append((f"payments:{direction}:{digest}", {
                        **item, "share_of_passed_direction_amount": item["total_amount"] / amount if amount else None,
                        "share_scope": "Только переданная выборка данного направления, не вся деятельность компании.",
                    }))
        if any(not math.isfinite(item["total_amount"]) for item in totals.values()):
            raise OverflowError
    except (OverflowError, ValueError):
        raise AgentContextError("Сумма платёжной выборки превышает допустимый диапазон.") from None
    return {"input_rows": len(rows), "directions": totals, "scope": PAYMENT_SCOPE}, top


def _instant(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _bank_documents(source, company, event, config, warnings, query=None) -> list[AnalyticsDocument]:
    if source is None or config.max_bank_documents == 0:
        return []
    try:
        async with asyncio.timeout(config.source_timeout_seconds):
            raw = await source.search(query or event.title, company=company.model_copy(deep=True),
                                      event=event.model_copy(deep=True), limit=config.max_bank_documents)
        if not isinstance(raw, list) or len(raw) > config.max_bank_documents:
            raise ValueError
        documents = [AnalyticsDocument.model_validate(item.model_dump(mode="python")
                     if isinstance(item, AnalyticsDocument) else item) for item in raw]
        seen, hashes = {}, {}
        for document in documents:
            if not document.article.is_bank_analytics:
                raise ValueError
            fingerprint = json_text(document.model_dump(mode="json"))
            if document.article.id in seen and seen[document.article.id] != fingerprint:
                warnings.append("BANK_SOURCE_ID_CONFLICT")
                return []
            seen[document.article.id] = fingerprint
            if document.source_document_id and document.source_hash:
                previous = hashes.setdefault(document.source_document_id, document.source_hash)
                if previous != document.source_hash:
                    warnings.append("BANK_DOCUMENT_HASH_CONFLICT")
                    return []
        result = []
        included = set()
        for document in documents:
            if _instant(document.article.published_at) > _instant(event.published_at):
                warnings.append("FUTURE_BANK_SOURCE_EXCLUDED")
            elif document.article.id not in included:
                result.append(document)
                included.add(document.article.id)
        return result
    except TimeoutError:
        warnings.append("BANK_SOURCE_TIMEOUT")
    except Exception:
        warnings.append("BANK_SOURCE_UNAVAILABLE_OR_INVALID")
    return []


async def prepare_context(
    company: CompanyResponse, event: NewsArticleResponse, counterparties: list[CounterpartyItem],
    source: AnalyticsSourceProtocol | None, config: AgentConfig, *, financial_inputs=None, loan_sensitivity=None, bank_query=None,
) -> PreparedContext:
    """One bounded search followed by pure aggregation. No implicit settings, database or LLM."""
    if len(counterparties) > config.max_input_counterparties:
        raise AgentContextError("Платёжная выборка превышает лимит входных строк.")
    company = company.model_copy(deep=True)
    event = event.model_copy(deep=True)
    rows = [item.model_copy(deep=True) for item in counterparties]
    business = build_business_context(company, rows)
    warnings: list[str] = []
    summary, top = _payments(company, rows)
    documents = await _bank_documents(source, company, event, config, warnings, bank_query)
    evidence: dict[str, Evidence] = {}
    sources: dict[str, GroundedSource] = {}
    bank_content_hashes = {}

    def add(source_record: GroundedSource, identifier: str, quote: str, kind: str, feature=None):
        if identifier in evidence:
            raise AgentContextError("Повторный идентификатор основания.")
        sources[source_record.source_id] = source_record
        evidence[identifier] = Evidence(
            id=identifier, source_id=source_record.source_id, source_title=source_record.source_title,
            quote=quote, kind=kind, company_feature=feature, source_url=source_record.source_url,
            published_at=source_record.published_at, provenance_status=source_record.provenance_status,
            source_document_id=source_record.source_document_id, source_page=source_record.source_page,
            source_hash=source_record.source_hash, text_truncated=source_record.text_truncated,
        )

    excerpt, truncated = _excerpt(event.content, config.max_article_chars)
    if not excerpt:
        excerpt, truncated = event.title, True
        warnings.append("EVENT_CONTENT_HAS_NO_BOUNDED_SENTENCE")
    add(GroundedSource(source_id=f"news:{event.id}", source_title=event.title,
                       published_at=event.published_at, is_bank_analytics=event.is_bank_analytics,
                       text_truncated=truncated), "news:event", excerpt, "news")
    profile_source = GroundedSource(source_id=f"company:{company.id}", source_title="Профиль компании")
    for field, value in business.known_features.items():
        add(profile_source, f"company:{field}", json_text({field: value}), "company", field)
    payment_source = GroundedSource(source_id=f"payments:{company.id}", source_title="Переданная платёжная выборка")
    if rows:
        add(payment_source, "payments:summary", json_text(summary), "payment")
        for identifier, item in top:
            add(payment_source, identifier, json_text(item), "payment")
    for document in documents:
        article = document.article
        excerpt, truncated = _bank_excerpt(article.content, config.max_article_chars, bank_query or event.title)
        if not excerpt:
            warnings.append("BANK_SOURCE_CONTEXT_DOES_NOT_FIT")
            continue
        if truncated:
            warnings.append("BANK_SOURCE_TRUNCATED")
        record = GroundedSource(
            source_id=f"bank:{article.id}", source_title=article.title,
            source_url=str(document.source_url) if document.source_url is not None else None,
            published_at=article.published_at, is_bank_analytics=True,
            provenance_status=document.provenance_status, source_document_id=document.source_document_id,
            source_page=document.source_page, source_hash=document.source_hash, text_truncated=truncated,
        )
        add(record, f"bank:{article.id}:excerpt", excerpt, "bank")
        bank_content_hashes[record.source_id] = hashlib.sha256(article.content.encode('utf-8')).hexdigest()

    payload = {
        "company": {"id": company.id, "synthetic_u": company.synthetic_u, "name": company.name,
                    "known_features": business.known_features},
        "event": {"id": event.id, "title": event.title, "source": event.source,
                  "published_at": event.published_at.isoformat(), "category": event.category,
                  "text_evidence_id": "news:event"},
        "business_context": business.model_dump(mode="json", exclude={"known_features"}),
        "payment_scope": PAYMENT_SCOPE,
    }
    # Drop whole optional evidence entries; a removed snippet is never left in the registry.
    optional = [key for key in evidence if key.startswith("bank:")] + [key for key, _ in top]
    while True:
        payload["evidence"] = [item.model_dump(mode="json", exclude_none=True) for item in evidence.values()]
        payload["warnings"] = list(dict.fromkeys(warnings))
        try:
            if len(json_text(payload)) <= config.max_context_chars:
                break
        except (ValueError, TypeError):
            raise AgentContextError("Контекст не сериализуется в конечный JSON.") from None
        if not optional:
            raise AgentContextError("Обязательный контекст превышает допустимый размер.")
        evidence.pop(optional.pop(), None)
        warnings.append("CONTEXT_OPTIONAL_EVIDENCE_OMITTED")
    visible_source_ids = {item.source_id for item in evidence.values()}
    return PreparedContext(company, event, business, evidence,
                           [item for key, item in sources.items() if key in visible_source_ids], payload,
                           list(dict.fromkeys(warnings)),
                           {key:value for key,value in bank_content_hashes.items() if key in visible_source_ids})


def merge_bank_context(original: PreparedContext, additional: PreparedContext, config: AgentConfig) -> PreparedContext:
    """Accumulate exact visible bank evidence; never silently replace earlier facts."""
    if original.company != additional.company or original.event != additional.event:
        raise AgentContextError("REFINED_CONTEXT_IDENTITY_CHANGED")
    evidence=dict(original.evidence)
    sources={item.source_id:item for item in original.sources}
    for key,item in additional.evidence.items():
        if item.kind!='bank':
            if key in evidence and evidence[key]!=item:
                raise AgentContextError("REFINED_CONTEXT_FACT_CHANGED")
            continue
        source=next(value for value in additional.sources if value.source_id==item.source_id)
        if source.source_id in sources and sources[source.source_id]!=source:
            raise AgentContextError("BANK_SOURCE_CHANGED_DURING_RUN")
        if source.source_id in original.bank_content_hashes and original.bank_content_hashes[source.source_id]!=additional.bank_content_hashes.get(source.source_id):
            raise AgentContextError("BANK_SOURCE_CHANGED_DURING_RUN")
        if key in evidence and evidence[key]!=item:
            if not original.bank_content_hashes.get(source.source_id):
                raise AgentContextError("BANK_SOURCE_CHANGED_DURING_RUN")
            # Same immutable article, another exact passage selected by the new
            # query. Preserve the earlier ID and quote instead of overwriting it.
            key=key+':'+hashlib.sha256(item.quote.encode('utf-8')).hexdigest()[:12]
            item=item.model_copy(update={'id':key})
            if key in evidence and evidence[key]!=item:
                raise AgentContextError("BANK_EXCERPT_ID_CONFLICT")
        for previous in evidence.values():
            if (previous.kind=='bank' and item.source_document_id and previous.source_document_id==item.source_document_id
                    and previous.source_hash and item.source_hash and previous.source_hash!=item.source_hash):
                raise AgentContextError("BANK_DOCUMENT_HASH_CHANGED_DURING_RUN")
        evidence[key]=item;sources[source.source_id]=source
    if sum(item.kind=='bank' for item in evidence.values())>config.max_bank_documents:
        raise AgentContextError("BANK_CONTEXT_DOCUMENT_LIMIT")
    warnings=list(dict.fromkeys([*original.warnings,*additional.warnings]))
    payload={**original.payload,'warnings':warnings,
             'evidence':[item.model_dump(mode='json',exclude_none=True) for item in evidence.values()]}
    if len(json_text(payload))>config.max_context_chars:
        raise AgentContextError("REFINED_CONTEXT_LIMIT")
    return PreparedContext(original.company,original.event,original.business,evidence,list(sources.values()),payload,warnings,
                           {**original.bank_content_hashes,**additional.bank_content_hashes})
