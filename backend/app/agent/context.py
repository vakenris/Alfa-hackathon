"""Bounded JSON context and a catalog of sources actually shown to the model."""

import copy
import json
import hashlib
import math

from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .config import AgentConfig
from .applicability import event_qualitative_channels
from .models import (
    AgentContextError, AgentEvidenceError, SourceRecord, ToolObservation,
    BusinessContext, GroundedSource,
)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def clip_article(article: dict, limit: int) -> dict:
    article = copy.deepcopy(article)
    shortened = False
    for field in ("content", "summary"):
        text = article.get(field)
        if isinstance(text, str) and len(text) > limit:
            article[field] = text[:limit] + " [сокращено]"
            shortened = True
    return {"article": article, "text_truncated": shortened}


class RunContext:
    def __init__(
        self, company: CompanyResponse, event: NewsArticleResponse,
        counterparties: list[CounterpartyItem], config: AgentConfig,
    ):
        self.config = config
        self.force_finalize = False
        event_data = event.model_dump(mode="json", exclude_unset=True)
        company_data = company.model_dump(mode="json", exclude_unset=True)
        sample = counterparties[:config.max_counterparties]
        article_limit = config.max_article_chars
        # Leave space for error observations even when the initial input is large.
        reserve = min(config.max_context_chars // 2, min(config.max_steps, config.max_llm_calls) * 512)
        target_size = config.max_context_chars - reserve
        while True:
            event_view = clip_article(event_data, article_limit)
            sources = [SourceRecord(source_id=f"company:{company.id}", source_title="Профиль компании")]
            sources.append(SourceRecord(
                source_id=f"news:{event.id}", source_title=event.title,
                is_bank_analytics=event.is_bank_analytics,
            ))
            if sample:
                sources.append(SourceRecord(
                    source_id=f"counterparties:{company.id}", source_title="Контрагенты компании",
                ))
            self.payload = {
                "company": company_data, "event": event_view["article"],
                "counterparties": [item.model_dump(mode="json", exclude_unset=True) for item in sample],
                "sources": [item.model_dump(mode="json") for item in sources],
                "observations": [],
                "limits": {
                    "event_text_truncated": event_view["text_truncated"],
                    "counterparties_omitted": len(counterparties) - len(sample),
                    "counterparties_are_sample": True,
                },
            }
            try:
                fits = len(json_text(self.payload)) <= target_size
            except (ValueError, TypeError):
                raise AgentContextError("Входной контекст не сериализуется в корректный JSON.") from None
            if fits:
                break
            if sample:
                sample = sample[:-1]
            elif article_limit > 64:
                article_limit = max(64, article_limit // 2)
            else:
                raise AgentContextError("Профиль и метаданные новости превышают лимит контекста.")
        self.counterparties = list(sample)

    def add_observation(self, observation: ToolObservation) -> ToolObservation:
        item = observation.model_dump(mode="json")
        added_sources: list[dict] = []
        if observation.status == "ok" and observation.tool == "get_bank_analytics":
            documents = []
            for document in item["data"]["documents"]:
                view = clip_article(document["article"], self.config.max_article_chars)
                documents.append({**view, "source_url": document["source_url"]})
                article = view["article"]
                added_sources.append(SourceRecord(
                    source_id=f"news:{article['id']}", source_title=article["title"],
                    source_url=document["source_url"], is_bank_analytics=article["is_bank_analytics"],
                ).model_dump(mode="json"))
            item["data"] = {"documents": documents}

        candidate = copy.deepcopy(self.payload)
        candidate["observations"].append(item)
        for source in added_sources:
            if source not in candidate["sources"]:
                candidate["sources"].append(source)
        try:
            fits = (
                len(json_text(item)) <= self.config.max_tool_result_chars
                and len(json_text(candidate)) <= self.config.max_context_chars
            )
        except (ValueError, TypeError):
            fits = False
        if fits:
            self.payload = candidate
            return observation

        # Reject the entire oversized observation: its sources were never shown.
        self.force_finalize = True
        error = ToolObservation(
            tool=observation.tool, arguments={}, status="error", error="CONTEXT_LIMIT",
        )
        candidate = copy.deepcopy(self.payload)
        candidate["observations"].append(error.model_dump(mode="json"))
        if len(json_text(candidate)) > self.config.max_context_chars:
            raise AgentContextError("Недостаточно места для записи ошибки инструмента в контекст.")
        self.payload = candidate
        return error

    @property
    def has_bank_materials(self) -> bool:
        return any(item["is_bank_analytics"] for item in self.payload["sources"])

    def validate_evidences(self, result: MLInsightResult) -> bool:
        """Validate provenance only; return whether a bank source was cited."""
        bank_cited = False
        features = {
            f"{key}={json_text(value)}" for key, value in self.payload["company"].items()
        }
        for evidence in result.evidences:
            matching = [
                source for source in self.payload["sources"]
                if source["source_title"] == evidence.source_title
            ]
            identities = {
                (source["source_id"], source["source_url"], source["is_bank_analytics"])
                for source in matching
            }
            if len(identities) != 1:
                raise AgentEvidenceError("Evidence ссылается на неизвестный или неоднозначный источник.")
            source = matching[0]
            if evidence.source_url is not None and evidence.source_url != source["source_url"]:
                raise AgentEvidenceError("URL в evidence не был получен от указанного источника.")
            if evidence.company_feature is not None and evidence.company_feature not in features:
                raise AgentEvidenceError("Признак компании в evidence отсутствует во входном контексте.")
            bank_cited = bank_cited or source["is_bank_analytics"]
        return bank_cited


FEATURE_KEYS = (
    "mainokved_code", "mainokved_name", "region", "city", "group_id",
    "fts_country_name", "revenue_company", "cl_product_loa_mark",
    "cl_product_lea_mark", "cl_product_dep_mark", "cl_product_gar_mark",
    "cl_product_rko_mark", "cl_product_zpp_mark", "factoring_mark", "fts_flag",
)


def build_business_context(company: CompanyResponse, counterparties: list[CounterpartyItem]) -> BusinessContext:
    """Case-specific interpretation; raw DTOs remain unchanged and no group is inferred."""
    raw = company.model_dump(mode="python", exclude_unset=True)
    known = {}
    unknown = []
    for key in FEATURE_KEYS:
        value = raw.get(key)
        valid = value is not None and value is not False and value != ""
        if isinstance(value, str) and len(value) > 512:
            valid = False
        if key == "revenue_company":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 and value != 1000
        if valid:
            known[key] = value
        else:
            unknown.append(key)
    return BusinessContext(
        known_features=known, unknown_features=unknown,
        limitations=[
            "Флаги продуктов не определяют остатки, ставки и условия договоров; False не доказывает отсутствие обязательств.",
            "Платежи описывают только выборку, не выручку и не отношения поставщик–покупатель.",
            "Для исходного набора FAQ указывает период с 1 января; год и конец периода не предоставлены.",
            "Данные группы и рыночная доля не вычисляются из одного профиля. Страны ФТС не задают направление и объём торговли.",
        ], payment_sample_size=len(counterparties),
        self_transfer_rows=sum(item.synthetic_u == company.synthetic_u for item in counterparties),
        revenue_year=2025 if "revenue_company" in known else None,
        revenue_currency="RUB" if "revenue_company" in known else None,
    )


class GroundedRunContext(RunContext):
    """Reuse input clipping; source registry contains only text actually sent to the model."""

    def __init__(self, company, event, counterparties, config):
        super().__init__(company, event, counterparties, config)
        self.business = build_business_context(company, self.counterparties)
        self.payload["business_context"] = self.business.model_dump(mode="json")
        self.payload["analysis_plan"] = {
            "goals": ["Факты события", "Связь с компанией", "Применимые источники", "Проверка оснований"],
            "missing_features": self.business.unknown_features,
            "completed_tools": [],
        }
        self.documents: dict[str, dict] = {}
        self.sources: dict[str, GroundedSource] = {}
        self.event_source_id = f"news:{event.id}"
        initial = GroundedSource(
            source_id=self.event_source_id, source_title=event.title,
            published_at=event.published_at, is_bank_analytics=event.is_bank_analytics,
            text_truncated=self.payload["limits"]["event_text_truncated"],
        )
        self.sources[initial.source_id] = initial
        # Model sees neutral financial directions, never inferred business roles.
        for item in self.payload["counterparties"]:
            item["direction"] = (
                "self_transfer" if item["synthetic_u"] == company.synthetic_u
                else {"SUPPLIER": "outgoing", "BENEFICIARY": "outgoing", "CUSTOMER": "incoming", "PAYER": "incoming"}.get(item["direction"], "unknown")
            )
        self.counterparties = [item.model_copy(update={"direction": view["direction"]})
                               for item, view in zip(self.counterparties, self.payload["counterparties"])]
        self._sync_sources()
        while len(json_text(self.payload)) > config.max_context_chars:
            text = self.payload["event"].get("content", "")
            if len(text) > 80:
                self.payload["event"]["content"] = text[:max(64, len(text) // 2)]
                self.payload["limits"]["event_text_truncated"] = True
                initial.text_truncated = True
                self._sync_sources()
            elif self.payload["counterparties"]:
                self.payload["counterparties"].pop()
                self.counterparties = self.counterparties[:-1]
                self.payload["limits"]["counterparties_omitted"] += 1
            else:
                raise AgentContextError("Бизнес-контекст превышает допустимый размер.")
        self.business.payment_sample_size = len(self.counterparties)
        self.business.self_transfer_rows = sum(item.synthetic_u == company.synthetic_u for item in self.counterparties)
        self.payload["business_context"] = self.business.model_dump(mode="json")
        self.documents[initial.source_id] = copy.deepcopy(self.payload["event"])
        candidates = event_qualitative_channels(self.business.known_features, self.payload["event"])
        if candidates:
            self.payload["analysis_plan"]["qualitative_candidates"] = candidates
            if len(json_text(self.payload)) > config.max_context_chars:
                raise AgentContextError("Бизнес-контекст превышает допустимый размер.")

    def _sync_sources(self):
        self.payload["sources"] = [item.model_dump(mode="json") for item in self.sources.values()]

    def add_observation(self, observation: ToolObservation) -> ToolObservation:
        item = observation.model_dump(mode="json")
        pending = []
        if observation.status == "ok" and observation.tool == "get_bank_analytics":
            docs = []
            # Budget both content and summary across documents rather than
            # discarding an ordinary multi-document result wholesale.
            document_count = max(1, len(item["data"]["documents"]))
            excerpt_limit = min(self.config.max_article_chars,
                                max(64, (self.config.max_tool_result_chars - 2048) // document_count // 2))
            for document in item["data"]["documents"]:
                view = clip_article(document["article"], excerpt_limit)
                article = view["article"]
                fingerprint = hashlib.sha256(json_text(document).encode("utf-8")).hexdigest()[:16]
                source = GroundedSource(
                    source_id=f"bank:{article['id']}:{fingerprint}", source_title=article["title"],
                    source_url=document["source_url"], published_at=article["published_at"],
                    is_bank_analytics=article["is_bank_analytics"],
                    provenance_status=document.get("provenance_status", "unverified"),
                    source_document_id=document.get("source_document_id"),
                    source_page=document.get("source_page"), source_hash=document.get("source_hash"),
                    text_truncated=view["text_truncated"],
                )
                pending.append((source, article))
                docs.append({**view, "source_id": source.source_id, "source_url": source.source_url})
            item["data"] = {**item["data"], "documents": docs}
        candidate = copy.deepcopy(self.payload)
        candidate["observations"].append(item)
        for source, _ in pending:
            if source.source_id not in self.sources:
                candidate["sources"].append(source.model_dump(mode="json"))
        if (len(json_text(item)) > self.config.max_tool_result_chars
                or len(json_text(candidate)) > self.config.max_context_chars):
            self.force_finalize = True
            error = ToolObservation(tool=observation.tool, arguments={}, status="error", error="CONTEXT_LIMIT")
            candidate = copy.deepcopy(self.payload)
            candidate["observations"].append(error.model_dump(mode="json"))
            if len(json_text(candidate)) > self.config.max_context_chars:
                raise AgentContextError("Недостаточно места для ошибки инструмента.")
            self.payload = candidate
            return error
        self.payload = candidate
        for source, article in pending:
            self.sources[source.source_id] = source
            self.documents[source.source_id] = copy.deepcopy(article)
        return observation
