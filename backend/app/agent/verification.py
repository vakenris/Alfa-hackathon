"""Conservative executable checks and deterministic projection, not semantic truth certification."""

import re
from decimal import Decimal
from datetime import timezone
from itertools import combinations

from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.insight import EvidenceItem

from .context import GroundedRunContext, json_text
from .applicability import event_qualitative_channels
from .models import BANK_VIEW_UNAVAILABLE, DISCLAIMER, CheckIssue, DraftInsight, VerificationReport


ASSERTIVE = re.compile(
    r"гарантир|неизбеж|обязательн|безусловн|однозначн|несомнен|точно\b|"
    r"доказан|привед[её]т|непременно|will\b|guarantee|certainly", re.I,
)
CONDITIONAL = re.compile(r"может|могут|возмож|при услов|если|зависит|could\b|may\b", re.I)
BANK_ATTRIBUTION = re.compile(
    r"(?:банк|альфа)[^.!?]{0,50}(?:счита|рекоменду|прогноз|гарантир|подтвержда|утвержда)|"
    r"позици[яию] банка|(?:по (?:мнению|оценке|прогнозу|данным)|согласно)[^.!?]{0,45}(?:банк|альфа)|"
    r"альфа|alfa|bank (?:says|expects|recommends)", re.I,
)
UNSUPPORTED_COMPANY = re.compile(
    r"(?:у компании|компания имеет|компании принадлежат)[^.!?]{0,70}"
    r"(?:плавающ|фиксированн|остаток долга|кредитный договор)|"
    r"(?:прибыль|выручка|плат[её]ж[аи]|процентные расходы) компании (?:выраст|увелич|сниз|сократ)", re.I,
)
FEATURE_LABELS = {
    "mainokved_code": "ОКВЭД", "mainokved_name": "Отрасль", "region": "Регион",
    "city": "Город", "group_id": "Идентификатор группы", "fts_country_name": "Страны ФТС",
    "revenue_company": "Выручка за 2025 год, RUB", "cl_product_loa_mark": "Признак кредитных продуктов",
    "cl_product_lea_mark": "Признак лизинга", "cl_product_dep_mark": "Признак депозитов",
    "cl_product_gar_mark": "Признак банковских гарантий", "cl_product_rko_mark": "Расчётное обслуживание",
    "cl_product_zpp_mark": "Признак зарплатного проекта", "factoring_mark": "Признак факторинга",
    "fts_flag": "Наличие сведений ФТС",
}

# Auditable minimum applicability checks, intentionally conservative. They rule
# out obvious unrelated mechanisms; lexical overlap is not causal entailment.
CHANNEL_RULES = {
    "financing": (r"ставк|кредит|финансир|инфляц|фондирован|за[её]м", r"ставк|кредит|финансир|фондирован|за[её]м|ликвидност"),
    "investment": (r"ставк|инвест|оборудован|модернизац|строител", r"инвест|проект|бюджет|капитал|модернизац"),
    "fx": (r"валют|курс|доллар|юан|евро", r"валют|курс|расч[её]т|импорт|экспорт"),
    "logistics": (r"логист|транспорт|фрахт|перевоз|маршрут|постав", r"логист|транспорт|достав|перевоз|постав|себестоим"),
    "inputs": (r"сырь|оборудован|нефт|газ|энерг|металл|комплект", r"сырь|себестоим|производ|оборудован|затрат|снабжен"),
    "regulation": (r"закон|регулир|налог|пошлин|ограничен|санкци|требован", r"закон|налог|соответств|пошлин|ограничен|затрат|документ"),
    "demand": (r"спрос|потреблен|продаж|заказ|доход", r"спрос|продаж|выручк|заказ|потреблен"),
}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def complete_quote(quote: str, visible: str, *, title: bool = False) -> bool:
    """Do not let a substring drop negation or the subject of a source statement."""
    if quote != quote.strip():
        return False
    if title:
        return quote == visible.strip()
    start = visible.find(quote)
    while start >= 0:
        before = visible[:start]
        after = visible[start + len(quote):]
        left_boundary = not before.strip() or before.endswith("\n") or (
            before[-1:].isspace() and before.rstrip()[-1:] in ".!?"
        )
        right_boundary = not after.strip() or after.startswith("\n") or (
            quote[-1:] in ".!?" and (after[:1].isspace() or after[:1] in '\"»”')
        )
        if left_boundary and right_boundary:
            return True
        start = visible.find(quote, start + 1)
    return False


def free_text_issues(text: str, *, conditional: bool = False) -> list[str]:
    """A narrow refusal policy: no inferred figures or unqualified causal guarantees."""
    codes = []
    if BANK_ATTRIBUTION.search(text):
        codes.append("UNSUPPORTED_BANK_ATTRIBUTION")
    if ASSERTIVE.search(text):
        codes.append("FALSE_CERTAINTY")
    if re.search(r"\d", text):
        codes.append("UNSUPPORTED_INFERENCE_NUMBER")
    if UNSUPPORTED_COMPANY.search(text):
        codes.append("UNSUPPORTED_COMPANY_CONDITION")
    if conditional and not CONDITIONAL.search(text):
        codes.append("CAUSAL_LINK_NOT_CONDITIONAL")
    return codes


def verify_draft(draft: DraftInsight, context: GroundedRunContext) -> VerificationReport:
    issues: list[CheckIssue] = []
    warnings = {"SEMANTIC_EXPERT_REVIEW_REQUIRED"}
    def fail(code, claim_id=None):
        issue = CheckIssue(code=code, claim_id=claim_id)
        if issue not in issues:
            issues.append(issue)

    facts = {item.id: item for item in draft.facts}
    impacts = {item.id: item for item in draft.impacts}
    if len(facts) != len(draft.facts) or len(impacts) != len(draft.impacts) or set(facts) & set(impacts):
        fail("DUPLICATE_CLAIM_ID")
    if not any(item.source_id == context.event_source_id for item in draft.facts):
        fail("MISSING_EVENT_ANCHOR")
    event_date = utc(context.sources[context.event_source_id].published_at)
    quantitative_facts = []
    for fact in draft.facts:
        source = context.sources.get(fact.source_id)
        document = context.documents.get(fact.source_id)
        if source is None or document is None:
            fail("UNKNOWN_SOURCE", fact.id)
            continue
        visible = document.get(fact.field)
        if not isinstance(visible, str) or not fact.quote.strip() or "[сокращено]" in fact.quote or fact.quote not in visible:
            fail("QUOTE_NOT_VISIBLE", fact.id)
        elif not complete_quote(fact.quote, visible, title=fact.field == "title"):
            fail("QUOTE_CONTEXT_INCOMPLETE", fact.id)
        if fact.kind == "bank_statement":
            if not (source.is_bank_analytics and source.provenance_status == "verified" and source.source_document_id):
                fail("BANK_SOURCE_NOT_VERIFIED", fact.id)
            if fact.field != "content":
                fail("BANK_STATEMENT_REQUIRES_CONTENT", fact.id)
        elif source.is_bank_analytics:
            warnings.add("BANK_MATERIAL_NOT_AN_OFFICIAL_POSITION")
        if utc(source.published_at) > event_date:
            fail("SOURCE_AFTER_ANALYSIS_DATE", fact.id)
        if (event_date - utc(source.published_at)).days > 90:
            warnings.add("HISTORICAL_SOURCE_CONTEXT")
        if source.text_truncated:
            warnings.add("SOURCE_TEXT_TRUNCATED")
        quantitative = re.search(
            r"\d+(?:[.,]\d+)?\s*(?:%|процент|руб|млн|млрд|RUB)|(?:ставк\w*|значени\w*)[^.!?]{0,45}\d", fact.quote, re.I,
        )
        if quantitative:
            quantitative_facts.append(fact)
        if quantitative and fact.value is None:
            # An exact, attributed report may contain a number without enough
            # scope to compute with it. Never certify its numeric interpretation.
            warnings.add("NUMERIC_SCOPE_UNRESOLVED")
        if fact.value is None and any((fact.metric, fact.entity, fact.period, fact.unit)):
            fail("NUMERIC_SCOPE_REQUIRED", fact.id)
        if fact.value is not None:
            if not all((fact.metric, fact.entity, fact.unit, fact.period)):
                fail("NUMERIC_SCOPE_REQUIRED", fact.id)
            else:
                tokens = re.findall(r"(?<![\w.])[+-]?\d+(?:[.,]\d+)?(?!\w)", fact.quote)
                if Decimal(str(fact.value)) not in {Decimal(token.replace(",", ".")) for token in tokens}:
                    fail("NUMBER_NOT_ANCHORED", fact.id)
                if not all(part.casefold() in fact.quote.casefold() for part in (fact.metric, fact.entity, fact.period, fact.unit)):
                    fail("NUMERIC_SCOPE_NOT_ANCHORED", fact.id)

    if len(quantitative_facts) > 1 and any(fact.value is None for fact in quantitative_facts):
        # Dropping every optional metadata field cannot silently disable the
        # conflict check when synthesizing multiple quantitative source claims.
        fail("UNNORMALIZED_NUMERIC_CLAIM")

    known = context.business.known_features
    if draft.applicability == "insufficient":
        if draft.impacts or draft.actions:
            fail("INSUFFICIENT_CONTEXT_HAS_CONCLUSIONS")
        if not draft.missing_data:
            fail("MISSING_CLARIFICATION")
        visible_event = context.documents[context.event_source_id]
        if event_qualitative_channels(known, visible_event):
            # Missing contract data blocks quantification, not a conditional
            # discussion of new financing / prospective investment decisions.
            fail("QUALITATIVE_ANALYSIS_MISSING")
    elif not draft.impacts or not draft.actions:
        fail("INCOMPLETE_INTERPRETATION")
    for impact in draft.impacts:
        if not set(impact.fact_ids) <= set(facts):
            fail("UNKNOWN_FACT_REFERENCE", impact.id)
        if not set(impact.company_features) <= set(known):
            fail("UNKNOWN_COMPANY_FEATURE", impact.id)
        # Industry alone can support a conditional channel; a name or ID cannot.
        if not impact.company_features:
            fail("MISSING_PERSONALIZATION", impact.id)
        for code in free_text_issues(impact.mechanism, conditional=True):
            fail(code, impact.id)
        # Contract terms may be explicit assumptions, not hidden factual clauses
        # in a sentence that happens to contain a conditional word elsewhere.
        if re.search(r"плавающ|фиксированн|остаток долга|floating|fixed.rate", impact.mechanism, re.I):
            fail("UNSUPPORTED_COMPANY_CONDITION", impact.id)
        event_quotes = " ".join(facts[key].quote for key in impact.fact_ids
                                if key in facts and facts[key].source_id == context.event_source_id)
        rule = CHANNEL_RULES.get(impact.channel)
        if rule is None or not re.search(rule[0], event_quotes, re.I) or not re.search(rule[1], impact.mechanism, re.I):
            fail("UNSUPPORTED_CAUSAL_CHANNEL", impact.id)
        if (re.search(r"сохран|без изменен|не измен", event_quotes, re.I)
                and re.search(r"(?:плат[её]ж|обслуживан.*долг)[^.!?]{0,50}(?:выраст|увелич|сниз|сократ)", impact.mechanism, re.I)):
            fail("UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE", impact.id)
        for text in [*impact.assumptions, *impact.data_needs]:
            for code in free_text_issues(text):
                fail(code, impact.id)
        if impact.channel in ("financing", "investment") and not impact.data_needs:
            fail("FINANCIAL_TERMS_UNKNOWN", impact.id)
        if not any(facts[key].source_id == context.event_source_id for key in impact.fact_ids if key in facts):
            fail("IMPACT_WITHOUT_EVENT", impact.id)
    for action in draft.actions:
        if not set(action.impact_ids) <= set(impacts):
            fail("UNKNOWN_IMPACT_REFERENCE")
        for text in [action.title, *action.required_data]:
            for code in free_text_issues(text):
                fail(code)
    for text in [*draft.missing_data, *draft.clarification_questions]:
        for code in free_text_issues(text):
            fail(code)

    conflicts = []
    for first, second in combinations(draft.facts, 2):
        key = (first.entity, first.metric, first.period, first.unit)
        if (all(key) and key == (second.entity, second.metric, second.period, second.unit)
                and first.value is not None and second.value is not None and first.value != second.value):
            conflicts.append({first.id, second.id})
    for pair in draft.acknowledged_conflicts:
        if len(pair) != 2 or len(set(pair)) != 2 or set(pair) not in conflicts:
            fail("INVALID_CONFLICT_REFERENCE")
    for pair in conflicts:
        if not any(set(ack) == pair for ack in draft.acknowledged_conflicts):
            fail("UNACKNOWLEDGED_CONTRADICTION")
    if conflicts:
        warnings.add("CONTRADICTORY_SOURCES")
        # Until a contradiction is resolved, claims cannot become causal premises.
        conflicted = set().union(*conflicts)
        if any(set(item.fact_ids) & conflicted for item in draft.impacts):
            fail("CONTRADICTION_USED_AS_PREMISE")
    if any(item["status"] == "error" for item in context.payload["observations"]):
        warnings.add("INCOMPLETE_TOOL_DATA")
    return VerificationReport(
        passed=not issues, issues=issues, warnings=sorted(warnings),
        checked_facts=len(draft.facts), checked_impacts=len(draft.impacts),
    )


def project_insight(draft: DraftInsight, context: GroundedRunContext, report: VerificationReport) -> MLInsightResult:
    """Only verified quotations are attributed to sources. Causal prose is labelled hypothesis."""
    if not report.passed:
        raise ValueError("Projection requires a passing verification report")
    event_facts = [fact for fact in draft.facts if fact.source_id == context.event_source_id]
    what = "\n".join(f"Источник сообщает: «{fact.quote}»" for fact in event_facts)
    used_features = list(dict.fromkeys(key for impact in draft.impacts for key in impact.company_features))
    why = "; ".join(
        f"{FEATURE_LABELS.get(key, key)}: {'отмечен в профиле' if context.business.known_features[key] is True else str(context.business.known_features[key])}" for key in used_features
    ) or "Связь события с компанией по доступным признакам не установлена."
    if draft.impacts:
        why += ". Связь с событием требует проверки: " + " ".join(item.mechanism for item in draft.impacts)
    impact_text = []
    for impact in draft.impacts:
        impact_text.append(
            f"Гипотеза AI ({ {'risk': 'риск', 'opportunity': 'возможность', 'mixed': 'риск и возможность'}[impact.direction]}; "
            f"горизонт: { {'short': 'краткосрочный', 'medium': 'среднесрочный', 'long': 'долгосрочный', 'unknown': 'не определён'}[impact.horizon]}): {impact.mechanism}\n"
            f"Допущения: {'; '.join(impact.assumptions)}\n"
            f"Для проверки: {'; '.join(impact.data_needs) or 'Проверить применимость механизма к компании.'}"
        )
    bank_facts = [fact for fact in draft.facts if fact.kind == "bank_statement"]
    bank_view = "\n".join(
        f"Материал «{context.sources[fact.source_id].source_title}» от "
        f"{context.sources[fact.source_id].published_at.date().isoformat()}: «{fact.quote}»"
        for fact in bank_facts
    ) or BANK_VIEW_UNAVAILABLE
    if bank_facts and "HISTORICAL_SOURCE_CONTEXT" in report.warnings:
        bank_view += "\nИсторический контекст; не подтверждает текущие условия или актуальность прогноза."
    gaps = list(dict.fromkeys([*draft.missing_data, *(need for impact in draft.impacts for need in impact.data_needs)]))
    ai_view = "Причинные выводы представлены как условные гипотезы AI, а не позиция банка."
    if gaps:
        ai_view += "\nНедостающие данные: " + "; ".join(gaps)
    if draft.acknowledged_conflicts:
        ai_view += "\nИсточники противоречат друг другу; спорные сведения не используются как установленная предпосылка."
    ai_view += "\nОграничения: " + " ".join(context.business.limitations)
    if report.warnings:
        ai_view += "\nПрограммные проверки не доказывают истинность причинных связей; требуется экспертная оценка."
    actions = "\n".join(
        f"{item.owner_role + ': ' if item.owner_role else ''}{item.title}"
        + (f" Нужны: {'; '.join(item.required_data)}" if item.required_data else "")
        for item in draft.actions
    ) or "Уточнить данные: " + "; ".join(draft.missing_data)
    evidences = [EvidenceItem(
        claim=fact.quote, source_title=context.sources[fact.source_id].source_title,
        source_url=context.sources[fact.source_id].source_url,
    ) for fact in draft.facts]
    for key in used_features:
        feature = f"{key}={json_text(context.business.known_features[key])}"
        # Legacy DB column company_feature is only 255 chars; full feature remains in the companion DTO.
        evidences.append(EvidenceItem(claim=f"Признак профиля: {key}", source_title="Профиль компании",
                                      company_feature=feature if len(feature) <= 255 else None))
    return MLInsightResult(
        what_happened=what, why_important=why,
        consequences="\n\n".join(impact_text) or "Данных для обоснованного вывода о последствиях недостаточно.",
        bank_view=bank_view, ai_view=ai_view, recommended_action=actions + "\n" + DISCLAIMER,
        relevant_bank_product=None, evidences=evidences,
    )
