"""Bounded JSON context and a catalog of sources actually shown to the model."""

import copy
import json

from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .config import AgentConfig
from .models import AgentContextError, AgentEvidenceError, SourceRecord, ToolObservation


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
