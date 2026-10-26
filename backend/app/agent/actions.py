"""Deterministic follow-ups over a trusted previous run; never calls an LLM.

The caller must supply the original core result. Structural checks are not a
signature: a caller able to forge the entire bundle is outside this boundary.
"""

import json
import math
import re
from types import SimpleNamespace

from .action_models import (
    ActionImpact, ActionProposal, ActionSourceRef, FinancialInputs, LoanSensitivityInput,
    ManagementActionRequest, ManagementActionResult,
)
from .context import json_text
from .financial import financial_calculations
from .models import AgentAnalysisResult, DISCLAIMER, Evidence
from .verification import project_insight, verify_draft


TOPICS = {
    "financing": r"ставк|кредит|процент|финансир|ликвид|за[её]м",
    "investment": r"инвест|бюджет|капитал|оборуд|модернизац|capex",
    "fx": r"валют|курс|импорт|экспорт|юан|доллар|евро",
    "logistics": r"логист|постав|перевоз|маршрут|транспорт",
    "inputs": r"сырь|себестоим|энерг|металл|комплект",
    "regulation": r"налог|закон|пошлин|санкци|регулир",
    "demand": r"спрос|продаж|заказ|потреблен",
}
CONTRACT = re.compile(
    r"долг|задолж|договор|погаш|плавающ|фиксирован|остаток|"
    r"(?:наш|компани|предприяти|у нас)[^?]{0,45}(?:кредит|ставк|процент)|"
    r"(?:ставк|процент|сумм)[^?]{0,45}(?:компани|предприяти|у нас|кредит)|"
    r"(?:получ|есть|имеется)[^?]{0,30}(?:кредит|за[её]м)|"
    r"(?:кредит|за[её]м)[^?]{0,40}(?:получен|получил|взят|выдан)", re.I,
)
FEATURES = {"revenue_company": r"выруч", "region": r"регион", "city": r"город",
            "mainokved_code": r"оквэд", "mainokved_name": r"отрасл",
            "company_age_years":r"возраст|давно.*(?:создан|работает)",
            "amount_workers_fns":r"работник|сотрудник|численност|штат|фнс",
            "amount_workers_spark":r"работник|сотрудник|численност|штат|спарк",
            "org_type":r"форм.*организац|организационн", "okfs_name":r"форм.*собственност",
            "cl_type":r"тип.*клиент|класс.*клиент"}
LIMITATIONS = [
    "Флаг продукта не подтверждает получение кредита, размер долга и договорные условия.",
    "Ответ использует сохранённый проверенный анализ; документы повторно не загружаются.",
    "Цитаты передают сообщения источников; условные механизмы AI требуют экспертной оценки.",
    "Предложения требуют решения человека; операции и изменения плана не выполняются.",
]


class ManagementActionEngine:
    def __init__(self, core):
        self.core = core

    @staticmethod
    def _integrity(analysis, company):
        if not analysis.verification.passed or analysis.verification.issues:
            return False
        if analysis.report.get("company_synthetic_u") != company.synthetic_u:
            return False
        if analysis.report.get("event_id") != analysis.event_id:
            return False
        sources = {item.source_id: item for item in analysis.sources}
        evidence = {item.id: item for item in analysis.evidence}
        if len(sources) != len(analysis.sources) or len(evidence) != len(analysis.evidence):
            return False
        current = company.model_dump(mode="python")
        if any(key not in current or current[key] != value
               for key, value in analysis.business_context.known_features.items()):
            return False
        for item in evidence.values():
            source = sources.get(item.source_id)
            if source is None or any(getattr(item, key) != getattr(source, key) for key in (
                "source_title", "source_url", "published_at", "provenance_status",
                "source_document_id", "source_page", "source_hash",
                "text_truncated",
            )):
                return False
            if item.kind == "company" and (item.source_id != f"company:{company.id}"
                    or item.company_feature not in analysis.business_context.known_features
                    or item.quote != json_text({item.company_feature: current[item.company_feature]})):
                return False
            if item.kind == "payment" and item.source_id != f"payments:{company.id}":
                return False
            if item.kind == "calculation" and not item.source_id.startswith(f"calculation:{company.id}:"):
                return False
            if item.kind == "news" and item.source_id != f"news:{analysis.event_id}":
                return False
            if item.kind == "bank" and not source.is_bank_analytics:
                return False
            if not any(ref.claim == item.quote and ref.source_title == item.source_title
                       and ref.source_url == item.source_url for ref in analysis.insight.evidences):
                return False
        if "news:event" not in evidence:
            return False
        # Recompute explicit arithmetic and the complete public projection. A
        # retained passed flag is not sufficient after a bundle was mutated.
        try:
            for calculation in analysis.calculations:
                kind = calculation.get("kind")
                if kind not in ("simple_interest_cost_delta", "cash_after_planned_capex"):
                    return False
                model = LoanSensitivityInput if kind == "simple_interest_cost_delta" else FinancialInputs
                kwargs = {"loan_sensitivity" if model is LoanSensitivityInput else "financial_inputs":
                          {key: calculation[key] for key in model.model_fields}}
                if financial_calculations(company.id, **kwargs) != [calculation]:
                    return False
                source = sources.get(f"calculation:{company.id}:{kind}")
                if source is None or source.is_bank_analytics or source.provenance_status != "unverified":
                    return False
                entry = evidence.get("calculation:" + kind)
                if entry is not None and entry.quote != json_text(calculation):
                    return False
            context = SimpleNamespace(evidence=evidence, warnings=[], business=analysis.business_context)
            verification = verify_draft(analysis.analysis, context)
            if not verification.passed:
                return False
            projected = project_insight(analysis.analysis, context, verification, analysis.calculations)
            return projected.model_dump(mode="json") == analysis.insight.model_dump(mode="json")
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _refs(evidence):
        return [ActionSourceRef(
            fact_id=item.id, source_id=item.source_id, source_title=item.source_title,
            source_url=item.source_url, quote=item.quote, provenance_status=item.provenance_status,
            published_at=item.published_at,source_document_id=item.source_document_id,
            source_page=item.source_page,source_hash=item.source_hash,text_truncated=item.text_truncated,
            attribution="verified_bank_statement" if item.kind == "bank"
            and item.provenance_status == "verified" and item.source_document_id else "reported_source",
        ) for item in evidence]

    @staticmethod
    def _result(request, analysis, **fields):
        return ManagementActionResult(
            company_id=request.company_id, event_id=analysis.event_id, action_kind=request.kind,
            disclaimer=DISCLAIMER, limitations=list(dict.fromkeys([*LIMITATIONS, *analysis.business_context.limitations])),
            report={"llm_calls": 0, "provider_attempts": 0, "mode": "deterministic_saved_analysis",
                    "company_synthetic_u": analysis.report.get("company_synthetic_u"),
                    "event_id": analysis.event_id}, **fields,
        )

    async def analyze_action(self, request: ManagementActionRequest, *, analysis: AgentAnalysisResult,
                             company, tracer) -> ManagementActionResult:
        """Preserve the existing API; never implicitly generate another paid analysis."""
        request = ManagementActionRequest.model_validate(request.model_dump(mode="python"))
        analysis = AgentAnalysisResult.model_validate(analysis.model_dump(mode="python"))
        if request.company_id != company.id or analysis.company_id != company.id:
            raise ValueError("ACTION_COMPANY_MISMATCH")
        if not self._integrity(analysis, company):
            return self._result(request, analysis, status="insufficient_evidence",
                                summary="Происхождение или целостность сохранённого анализа не подтверждены.")
        if request.kind == "question":
            return self._answer(request, analysis, company)
        return self._management(request, analysis)

    async def answer_question(self, question: str, *, analysis: AgentAnalysisResult, company, tracer):
        return await self.analyze_action(
            ManagementActionRequest(kind="question", company_id=company.id, question=question),
            analysis=analysis, company=company, tracer=tracer,
        )

    @staticmethod
    def _derived_evidence(analysis, identifier, source_id, quote, kind, feature=None):
        """Project a checked saved input; never take URLs or facts from the question."""
        source = next((item for item in analysis.sources if item.source_id == source_id), None)
        if source is None or source.is_bank_analytics:
            return None
        return Evidence(id=identifier, source_id=source.source_id, source_title=source.source_title,
                        quote=quote, kind=kind, company_feature=feature,
                        **source.model_dump(mode="python", exclude={"source_id", "source_title", "is_bank_analytics"}))

    def _calculation_answer(self, request, analysis):
        question = request.question
        budget_question = bool(re.search(r"бюджет|capex|капитал|средств|денежн", question, re.I))
        loan_question = bool(re.search(r"кредит|ставк|процент|долг|чувствительност", question, re.I))
        calculations = [item for item in analysis.calculations if
                        not (budget_question != loan_question) or item["kind"] ==
                        ("cash_after_planned_capex" if budget_question else "simple_interest_cost_delta")]
        selected = []
        for item in calculations:
            entry = self._derived_evidence(analysis, "calculation:" + item["kind"],
                f"calculation:{analysis.company_id}:{item['kind']}", json_text(item), "calculation")
            if entry is not None:
                selected.append(entry)
        if not selected:
            return None
        return self._result(request, analysis, status="complete", analysis_kind="quantitative_sensitivity",
            summary="\n".join([
                "Сохранённый условный расчёт: параметры заявлены пользователем, независимо не проверены. "
                "Метка actual означает заявление пользователя; это не подтверждение реального долга.",
                *(item["description"] for item in calculations),
            ]), source_refs=self._refs(selected), calculations=calculations)

    @staticmethod
    def _linked_evidence(analysis, identifiers):
        wanted = set(identifiers)
        return [item for item in analysis.evidence if item.id in wanted]

    def _scenario_answer(self, request, analysis):
        scenarios = analysis.analysis.scenarios
        if not scenarios:
            return self._result(request, analysis, status="needs_data",
                summary="В сохранённом анализе нет качественных сценариев по этому событию.",
                missing_data=["Сохранённые условия и исходы сценариев."])
        references = self._linked_evidence(analysis, [key for item in scenarios for key in item.evidence_ids])
        return self._result(request, analysis, status="complete", scenarios=scenarios,
            summary="Условные сценарии AI, а не прогноз или позиция банка:\n" + "\n".join(
                f"{item.label}: {item.condition}. {item.outcome}" for item in scenarios),
            source_refs=self._refs(references))

    def _requirements_answer(self, request, analysis):
        topics = {key for key, pattern in TOPICS.items() if re.search(pattern, request.question, re.I)}
        impacts = [item for item in analysis.analysis.impacts if not topics or item.channel in topics]
        ids = {item.id for item in impacts}
        actions = [item for item in analysis.analysis.actions if ids & set(item.impact_ids)]
        requirements = list(dict.fromkeys([
            *(text for item in impacts for text in item.data_needs),
            *(text for item in actions for text in item.required_data), *analysis.analysis.missing_data,
        ]))
        if not requirements:
            return self._result(request, analysis, status="needs_data",
                summary="В сохранённом анализе нет перечня данных для указанной проверки.",
                missing_data=["Уточнение проверяемого механизма и необходимых исходных данных."])
        references = self._linked_evidence(analysis, ["news:event", *(key for item in impacts for key in item.evidence_ids)])
        return self._result(request, analysis, status="complete", missing_data=requirements,
            summary="Для проверки условных выводов запрошены данные:\n" + "\n".join("• " + text for text in requirements),
            source_refs=self._refs(references))

    def _payment_answer(self, request, analysis, company):
        """Use selected saved payment snippets only, never reconstruct an omitted network."""
        incoming = bool(re.search(r"входящ|плательщик|поступлен", request.question, re.I))
        outgoing = bool(re.search(r"исходящ|получател|перечислен", request.question, re.I))
        direction = "incoming" if incoming and not outgoing else "outgoing" if outgoing and not incoming else None
        largest = bool(re.search(r"крупнейш|основн|максимальн|наибольш|концентрац|дол[яию]", request.question, re.I))
        role = bool(re.search(r"покупател|поставщик", request.question, re.I))
        saved = [(item, json.loads(item.quote)) for item in analysis.evidence if item.kind == "payment"]
        caveat = ("Только переданная выборка: период, валюта и полнота неизвестны. "
                  "Собственные переводы не являются внешними платежами. "
                  "Плательщик не установлен как покупатель, а получатель — как поставщик.")
        if not saved or role or (largest and direction is None):
            return self._result(request, analysis, status="needs_data", summary=caveat,
                missing_data=["Сохранённые платежи нужного направления; уточните входящие или исходящие платежи."
                              if not role else "Подтверждённые деловые роли участников; направление платежа их не доказывает."])
        summaries = [(item, data) for item, data in saved if "directions" in data]
        peers = [(item, data) for item, data in saved if "share_of_passed_direction_amount" in data
                 and data.get("direction") in ("incoming", "outgoing", "unknown")
                 and data.get("synthetic_u") != company.synthetic_u
                 and (direction is None or data["direction"] == direction)]
        labels = {"incoming": "Входящие внешние платежи", "outgoing": "Исходящие внешние платежи",
                  "unknown": "Платежи неизвестного направления", "self_transfer": "Собственные переводы отдельно"}
        parts, references = [], []
        if largest:
            if not peers:
                return self._result(request, analysis, status="needs_data", summary=caveat,
                    missing_data=["Выбранные сведения об участниках нужного направления и их долях."])
            peers.sort(key=lambda pair: (-pair[1]["total_amount"], pair[1]["synthetic_u"]))
            winner, data = peers[0]
            shares = [peer["share_of_passed_direction_amount"] for _, peer in peers]
            # A selected row need not be the first row of the original top.
            # Call it largest only when saved shares bound every omitted participant.
            if (any(type(share) not in (int, float) or not math.isfinite(share) or not 0 <= share <= 1 for share in shares)
                    or math.fsum(shares) > 1 + 1e-12 or shares[0] + 1e-12 < max(0, 1 - math.fsum(shares))):
                return self._result(request, analysis, status="needs_data", summary=caveat,
                    source_refs=self._refs([item for item, _ in peers]),
                    missing_data=["Сохранённая выборка участников недостаточна, чтобы установить крупнейшего во всём переданном направлении."])
            name = data.get("name") or data["synthetic_u"]
            parts.append(f"{labels[direction]}: крупнейший участник по сохранённым долям — {name}; "
                         f"сумма {data['total_amount']}, доля переданной выборки направления {shares[0]:.1%}.")
            references = [item for item, _ in peers]
        else:
            for item, data in summaries:
                references.append(item)
                for key, totals in data["directions"].items():
                    if direction is None or key == direction:
                        parts.append(f"{labels.get(key, key)}: сумма {totals['total_amount']}; "
                                     f"число операций {totals['transaction_count']}.")
            for item, data in peers:
                references.append(item)
                share = data["share_of_passed_direction_amount"]
                parts.append(f"{labels[data['direction']]}: участник {data.get('name') or data['synthetic_u']}; "
                             f"сумма {data['total_amount']}; доля " +
                             (f"{share:.1%}" if share is not None else "не определена") + ".")
        if not parts:
            return self._result(request, analysis, status="needs_data", summary=caveat,
                missing_data=["Сохранённые агрегаты запрошенного направления."])
        return self._result(request, analysis, status="complete", summary="\n".join([*parts, caveat]),
                            source_refs=self._refs(references))

    def _answer(self, request, analysis, company):
        question = request.question
        bank = bool(re.search(r"альфа|банковск[^?]{0,40}(?:аналитик|прогноз|обзор|материал)|(?:позици|мнени|оценк)[^?]{0,40}банк|"
                              r"банк[^?]{0,40}(?:счита|прогноз|рекоменду)|(?:пишет|говорит)[^?]{0,20}банк", question, re.I))
        calculation_question = bool(re.search(
            r"расч[её]т|рассчитан|сценари|чувствительност|формул|"
            r"(?:входн|переданн|заданн|использованн)[^?]{0,30}(?:параметр|данн|сумм|ставк)", question, re.I))
        actual_contract = bool(re.search(
            r"(?:реальн|фактическ|действующ|подтвержд[её]нн|сейчас|сегодня)[^?]{0,35}(?:долг|кредит|плат[её]ж|договор)|"
            r"(?:долг|кредит|плат[её]ж|договор)[^?]{0,35}(?:реальн|фактическ|действующ|сейчас|сегодня)|"
            r"(?:кредит|за[её]м)[^?]{0,40}(?:получен|получил|взят|выдан)", question, re.I))
        scenario_question = bool(re.search(r"сценари", question, re.I))
        explicit_calculation = bool(re.search(r"расч[её]т|рассчитан|чувствительност|формул|параметр", question, re.I))
        requirements_question = bool(re.search(
            r"(?:нужн|необходим|недоста|уточн|собрат|провер)[^?]{0,50}(?:данн|сведен|документ|допущен)|"
            r"(?:данн|документ|сведен)[^?]{0,40}(?:нужн|необходим|недоста|уточн|собрат|провер)", question, re.I))
        payment_question = bool(re.search(r"плат[её]ж|плательщик|получател|контрагент|самоперевод|концентрац|покупател|поставщик", question, re.I))
        quantitative_effect = bool(re.search(r"(?:на сколько|насколько|сколько)[^?]{0,100}(?:расход|затрат|плат[её]ж|увелич|выраст|измен|сниз|сократ|сэконом)", question, re.I))
        if not bank and not actual_contract:
            if requirements_question:
                return self._requirements_answer(request, analysis)
            if scenario_question and not explicit_calculation:
                return self._scenario_answer(request, analysis)
            if payment_question and not calculation_question and not quantitative_effect and not CONTRACT.search(question):
                return self._payment_answer(request, analysis, company)
        if calculation_question and not actual_contract and not bank:
            answer = self._calculation_answer(request, analysis)
            if answer is not None:
                return answer
        if CONTRACT.search(question):
            return self._result(request, analysis, status="needs_data",
                                summary="В сохранённом анализе нет независимо подтверждённых договорных параметров компании.",
                                missing_data=["Договоры, остатки долга, ставки и условия их пересмотра."])
        if quantitative_effect:
            return self._result(request, analysis, status="needs_data",
                                summary="Числовой эффект нельзя вывести из новости и продуктовых признаков.",
                                missing_data=["Явные финансовые параметры, применимая формула и период расчёта."])
        topics = {key for key, pattern in TOPICS.items() if re.search(pattern, question, re.I)}
        general = bool(re.search(r"событи|новост|что произош|что решил|источник|итог|что делать|рекоменд|действи", question, re.I))
        feature_keys = {key for key, pattern in FEATURES.items() if re.search(pattern, question, re.I)}
        impacts = [item for item in analysis.analysis.impacts if (general or item.channel in topics)
                   and not bank and not feature_keys]
        linked = {key for item in impacts for key in item.evidence_ids}
        available = list(analysis.evidence)
        for key in sorted(feature_keys):
            if key not in analysis.business_context.known_features or any(item.company_feature == key for item in available):
                continue
            # _integrity already compared every known value with the current authorized company.
            value = company.model_dump(mode="python")[key]
            entry = self._derived_evidence(analysis, "company:" + key, f"company:{company.id}",
                                          json_text({key: value}), "company", key)
            if entry is not None:
                available.append(entry)
        selected = [item for item in available if
                    (bank and item.kind == "bank") or
                    (not bank and feature_keys and item.company_feature in feature_keys) or
                    (not bank and not feature_keys and (item.id in linked or general or
                     any(re.search(TOPICS[topic], item.quote, re.I) for topic in topics)))]
        if not selected:
            return self._result(request, analysis, status="needs_data", summary="В сохранённых источниках нет ответа на этот вопрос.",
                                missing_data=["Источник по запрошенному факту или уточнение предмета вопроса."])
        parts = [f"Источник «{item.source_title}»"+
                 (f" ({item.published_at.date().isoformat()})" if item.published_at else "")+
                 f" [{item.provenance_status}"+
                 ("; частичная выдержка" if item.text_truncated else "")+f"]: «{item.quote}»" for item in selected]
        parts += [f"Условная гипотеза AI: {item.condition}. {item.mechanism}" for item in impacts]
        actions = []
        if re.search(r"что делать|рекоменд|действи", question, re.I):
            wanted = {item.id for item in impacts}
            actions = [ActionProposal(title=item.title, rationale=item.rationale, owner_role=item.owner_role,
                                      fact_ids=list(dict.fromkeys(key for impact in impacts
                                                if impact.id in item.impact_ids for key in impact.evidence_ids)))
                       for item in analysis.analysis.actions if set(item.impact_ids) & wanted]
        return self._result(request, analysis, status="complete", summary="\n".join(parts),
                            source_refs=self._refs(selected), proposed_actions=actions,
                            impacts=[ActionImpact(description=item.mechanism, fact_ids=item.evidence_ids,
                                                  assumptions=[item.condition]) for item in impacts])

    def _management(self, request, analysis):
        if request.kind == 'review_plan':
            channels={item.channel for item in analysis.analysis.impacts}
            items=[item for item in request.current_plan if set(item.related_channels) & channels]
            if not items:
                return self._result(request,analysis,status='needs_data',summary='Нужны относящиеся к анализу пункты плана с происхождением.',
                                    missing_data=['Пункты плана и их связь с каналами влияния.'])
            hypothetical=any(item.provenance.data_mode=='hypothetical' for item in items)
            related=[impact for impact in analysis.analysis.impacts if any(impact.channel in item.related_channels for item in items)]
            references=self._linked_evidence(analysis,[key for impact in related for key in impact.evidence_ids])
            return self._result(request,analysis,status='complete',
                summary=('Предложения включают гипотетические пункты плана; фактическое выполнение не предполагается.' if hypothetical else 'Предложения к обсуждению действующего плана; изменения не выполняются.')+
                    '\nПроисхождение и режим данных объявлены пользователем, независимо не проверены.\n'+
                    '\n'.join(f'«{item.title}»: {item.provenance.data_mode}; источник: {item.provenance.reference}.' for item in items),
                provided_plan=items,source_refs=self._refs(references),
                impacts=[ActionImpact(description=impact.mechanism,fact_ids=impact.evidence_ids,
                                      assumptions=[impact.condition]) for impact in related],
                proposed_actions=[ActionProposal(title='Проверить варианты исполнения: '+item.title,
                    rationale='\n'.join(impact.condition+'. '+impact.mechanism for impact in related if impact.channel in item.related_channels),
                    fact_ids=list(dict.fromkeys(key for impact in related if impact.channel in item.related_channels for key in impact.evidence_ids)),
                    plan_item_ids=[item.id],owner_role='CFO') for item in items])
        try:
            calculations = financial_calculations(request.company_id, financial_inputs=request.financial_inputs,
                                                   loan_sensitivity=request.loan_sensitivity)
        except ValueError:
            return self._result(request, analysis, status="needs_data", summary="Для расчёта нужны допустимые явные договорные параметры.",
                                missing_data=["Условия договорного пересмотра и применимая разница ставки."])
        if request.kind == "rate_scenario":
            text = {"up": "Если договорная стоимость заимствований повысится, процентные расходы могут увеличиться.",
                    "down": "Если договорная стоимость заимствований снизится, процентные расходы могут уменьшиться.",
                    "unchanged": "Если договорные ставки и условия сохраняются, изменение ключевой ставки само по себе не меняет платежи."}[request.rate_direction]
            if not any(item["kind"] == "simple_interest_cost_delta" for item in calculations):
                return self._result(request, analysis, status="needs_data", summary=text,
                                    missing_data=["Остаток долга, договорная разница ставки, период её применения и база дней."])
        else:
            if not any(item["kind"] == "cash_after_planned_capex" for item in calculations) or not request.current_plan or any(
                item.provenance.data_mode != "actual" for item in request.current_plan
            ):
                return self._result(request, analysis, status="needs_data", summary="Для обсуждения бюджета нужны фактические параметры и действующий план.",
                                    missing_data=["Свободные средства, планируемый CAPEX, валюта и план с происхождением."])
        title = "Проверить применимость расчёта и варианты этапности расходов."
        return self._result(request, analysis, status="complete", analysis_kind="quantitative_sensitivity",
                            summary="\n".join(item["description"] for item in calculations), calculations=calculations,
                            provided_financial_inputs=request.financial_inputs,
                            proposed_actions=[ActionProposal(title=title,
                                rationale="Расчёт использует только объявленные параметры и требует проверки человеком.",
                                plan_item_ids=[item.id for item in request.current_plan], owner_role="CFO")])
