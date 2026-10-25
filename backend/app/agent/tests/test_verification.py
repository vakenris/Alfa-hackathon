"""Offline acceptance/refusal checks using explicitly synthetic documents only."""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from backend.app.agent.config import AgentConfig
from backend.app.agent.context import GroundedRunContext
from backend.app.agent.models import (
    AnalyticsDocument, BANK_VIEW_UNAVAILABLE, DISCLAIMER, DraftInsight, ToolObservation,
)
from backend.app.agent.verification import project_insight, verify_draft
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse


class VerificationTests(unittest.TestCase):
    """Fixtures simulate adapter metadata; they make no claim about real bank materials."""

    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.analysis_date = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        self.company = CompanyResponse(
            id=1, synthetic_u="SYNTHETIC_TEST_COMPANY", name="Синтетический завод",
            mainokved_code="24.10", mainokved_name="Синтетическое производство",
            cl_product_loa_mark=True,
        )
        self.quote = "В синтетическом сценарии ключевая ставка повышена."
        self.event = NewsArticleResponse(
            id=10, title="Синтетическое сообщение о ставке", source="Синтетический источник",
            published_at=self.analysis_date, content=self.quote, category="Тест",
        )
        self.context = self.make_context()

    def make_context(self, *, event=None, config=None):
        return GroundedRunContext(self.company, event or self.event, [], config or AgentConfig())

    def payload(self):
        return {
            "applicability": "conditional",
            "facts": [{"id": "fact:event", "source_id": self.context.event_source_id, "quote": self.quote}],
            "impacts": [{
                "id": "impact:financing", "channel": "financing", "direction": "risk",
                "mechanism": "При условии пересмотра условий новых займов стоимость финансирования может вырасти.",
                "fact_ids": ["fact:event"], "company_features": ["cl_product_loa_mark"],
                "assumptions": ["Компания планирует привлекать новое финансирование."],
                "data_needs": ["Уточнить остатки долга и условия кредитных договоров."],
            }],
            "actions": [{
                "title": "Проверить условия финансирования перед обновлением бюджета.",
                "impact_ids": ["impact:financing"], "owner_role": "CFO",
                "required_data": ["Собрать условия действующих договоров."],
            }],
        }

    def verify(self, payload=None, *, context=None):
        return verify_draft(DraftInsight.model_validate(payload or self.payload()), context or self.context)

    def assert_refused(self, payload, expected, *, context=None):
        report = self.verify(payload, context=context)
        self.assertFalse(report.passed, report.model_dump())
        self.assertIn(expected, {issue.code for issue in report.issues}, report.model_dump())
        with self.assertRaises(ValueError):
            project_insight(DraftInsight.model_validate(payload), context or self.context, report)
        return report

    def add_document(self, *, context=None, content=None, published_at=None,
                     provenance_status="verified", bank=True, document_id="synthetic:document:20",
                     title="Синтетический банковский обзор", summary=None, article_id=20):
        context = context or self.context
        document = AnalyticsDocument(
            article=self.event.model_copy(update={
                "id": article_id, "title": title, "summary": summary,
                "content": content or "В синтетическом обзоре описана стоимость новых займов.",
                "published_at": published_at or self.analysis_date - timedelta(days=1),
                "is_bank_analytics": bank,
            }),
            source_url="https://synthetic.example.test/review",
            provenance_status=provenance_status, source_document_id=document_id,
        )
        previous = set(context.sources)
        observation = context.add_observation(ToolObservation(
            tool="get_bank_analytics", arguments={}, status="ok",
            data={"documents": [document.model_dump(mode="json")]},
        ))
        self.assertEqual(observation.status, "ok")
        added = set(context.sources) - previous
        self.assertEqual(len(added), 1)
        return added.pop(), document.article.content

    def with_bank_fact(self, **kwargs):
        source_id, quote = self.add_document(**kwargs)
        payload = self.payload()
        payload["facts"].append({
            "id": "fact:bank", "source_id": source_id, "quote": quote, "kind": "bank_statement",
        })
        return payload

    def test_conditional_example_projects_quotes_features_and_limits(self):
        draft = DraftInsight.model_validate(self.payload())
        report = verify_draft(draft, self.context)
        self.assertTrue(report.passed, report.model_dump())
        self.assertEqual((report.checked_facts, report.checked_impacts), (1, 1))
        self.assertIn("SEMANTIC_EXPERT_REVIEW_REQUIRED", report.warnings)
        result = project_insight(draft, self.context, report)
        self.assertIn(self.quote, result.what_happened)
        self.assertIn("Гипотеза AI", result.consequences)
        self.assertIn("Допущения:", result.consequences)
        self.assertEqual(result.bank_view, BANK_VIEW_UNAVAILABLE)
        self.assertIsNone(result.relevant_bank_product)
        self.assertIn(DISCLAIMER, result.recommended_action)
        self.assertTrue(any(e.company_feature == "cl_product_loa_mark=true" for e in result.evidences))
        self.assertIn("не доказывают истинность причинных связей", result.ai_view)

    def test_real_quote_does_not_justify_an_unconditional_causal_claim(self):
        payload = self.payload()
        self.assertIn(payload["facts"][0]["quote"], self.event.content)
        payload["impacts"][0]["mechanism"] = "Повышение ставки увеличивает прибыль синтетического завода."
        self.assert_refused(payload, "CAUSAL_LINK_NOT_CONDITIONAL")

    def test_conditional_word_does_not_justify_an_obviously_unrelated_mechanism(self):
        payload = self.payload()
        payload["impacts"][0]["mechanism"] = "Событие может улучшить урожайность."
        self.assert_refused(payload, "UNSUPPORTED_CAUSAL_CHANNEL")

    def test_unchanged_rate_does_not_automatically_change_existing_payments(self):
        quote = "В синтетическом сценарии ключевая ставка сохранена без изменений."
        context = self.make_context(event=self.event.model_copy(update={"content": quote}))
        payload = self.payload()
        payload["facts"][0]["quote"] = quote
        payload["impacts"][0]["mechanism"] = "Стоимость финансирования может измениться: платежи по кредитам вырастут."
        self.assert_refused(payload, "UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE", context=context)

    def test_false_certainty_is_refused_even_with_conditional_word(self):
        for text in ("При условии новых займов расходы неизбежно вырастут.",
                     "Стоимость финансирования может вырасти и гарантированно повысит прибыль."):
            with self.subTest(text=text):
                payload = self.payload()
                payload["impacts"][0]["mechanism"] = text
                self.assert_refused(payload, "FALSE_CERTAINTY")

    def test_certainty_schema_does_not_accept_a_factual_conclusion(self):
        payload = self.payload()
        payload["impacts"][0]["certainty"] = "certain"
        with self.assertRaises(ValidationError):
            DraftInsight.model_validate(payload)

    def test_bank_position_cannot_be_invented_in_ai_or_action_prose(self):
        for section in ("mechanism", "assumptions", "action", "clarification"):
            with self.subTest(section=section):
                payload = self.payload()
                invented = "Альфа-Банк рекомендует немедленно менять условия финансирования."
                if section == "mechanism":
                    payload["impacts"][0]["mechanism"] = "Возможен пересмотр условий. " + invented
                elif section == "assumptions":
                    payload["impacts"][0]["assumptions"] = [invented]
                elif section == "action":
                    payload["actions"][0]["title"] = invented
                else:
                    payload["clarification_questions"] = [invented]
                self.assert_refused(payload, "UNSUPPORTED_BANK_ATTRIBUTION")

    def test_loan_product_flag_does_not_support_contract_terms(self):
        self.assertTrue(self.context.business.known_features["cl_product_loa_mark"])
        for terms in ("У компании плавающая ставка по кредитному договору.",
                      "Компания имеет фиксированный остаток долга."):
            with self.subTest(terms=terms):
                payload = self.payload()
                payload["impacts"][0]["mechanism"] = "Возможно изменение расходов. " + terms
                self.assert_refused(payload, "UNSUPPORTED_COMPANY_CONDITION")

    def test_numbers_in_inferred_mechanisms_assumptions_and_actions_are_refused(self):
        for section in ("mechanism", "assumptions", "action"):
            with self.subTest(section=section):
                payload = self.payload()
                text = "Экономия может составить 20 процентов."
                if section == "mechanism":
                    payload["impacts"][0]["mechanism"] = text
                elif section == "assumptions":
                    payload["impacts"][0]["assumptions"] = [text]
                else:
                    payload["actions"][0]["title"] = text
                self.assert_refused(payload, "UNSUPPORTED_INFERENCE_NUMBER")

    def test_financial_channel_requires_missing_contract_data_to_be_declared(self):
        payload = self.payload()
        payload["impacts"][0]["data_needs"] = []
        self.assert_refused(payload, "FINANCIAL_TERMS_UNKNOWN")

    def test_personalization_must_reference_a_known_feature(self):
        for feature in ("loan_interest_rate", "city", "company_name"):
            with self.subTest(feature=feature):
                payload = self.payload()
                payload["impacts"][0]["company_features"] = [feature]
                self.assert_refused(payload, "UNKNOWN_COMPANY_FEATURE")

    def test_quotes_outside_visible_truncated_text_are_refused(self):
        hidden_quote = "Скрытое синтетическое условие за пределами видимого фрагмента."
        event = self.event.model_copy(update={"content": "Видимый текст. " * 20 + hidden_quote})
        context = self.make_context(event=event, config=AgentConfig(max_article_chars=64))
        self.assertIn(hidden_quote, event.content)
        self.assertNotIn(hidden_quote, context.documents[context.event_source_id]["content"])
        payload = self.payload()
        payload["facts"][0]["quote"] = hidden_quote
        report = self.assert_refused(payload, "QUOTE_NOT_VISIBLE", context=context)
        self.assertIn("SOURCE_TEXT_TRUNCATED", report.warnings)

    def test_unknown_source_is_refused_even_if_quote_exists_elsewhere(self):
        payload = self.payload()
        payload["facts"].append({"id": "fact:unknown", "source_id": "news:missing", "quote": self.quote})
        self.assert_refused(payload, "UNKNOWN_SOURCE")

    def test_bank_statement_requires_verified_bank_source_and_document_identity(self):
        cases = [
            {"provenance_status": "unverified"}, {"provenance_status": "synthetic"},
            {"bank": False}, {"document_id": None},
        ]
        for parameters in cases:
            with self.subTest(parameters=parameters):
                self.context = self.make_context()
                payload = self.with_bank_fact(**parameters)
                self.assert_refused(payload, "BANK_SOURCE_NOT_VERIFIED")

    def test_bank_statement_must_be_an_exact_visible_content_quote(self):
        payload = self.with_bank_fact()
        payload["facts"][-1]["quote"] = "Банк утверждает совершенно другую позицию."
        self.assert_refused(payload, "QUOTE_NOT_VISIBLE")

    def test_bank_title_or_summary_cannot_become_official_position(self):
        for field in ("title", "summary"):
            with self.subTest(field=field):
                self.context = self.make_context()
                payload = self.with_bank_fact(summary="Синтетическая краткая аннотация.")
                fact = payload["facts"][-1]
                fact["field"] = field
                fact["quote"] = self.context.documents[fact["source_id"]][field]
                self.assert_refused(payload, "BANK_STATEMENT_REQUIRES_CONTENT")

    def test_verified_bank_quote_is_projected_verbatim_without_added_bank_position(self):
        payload = self.with_bank_fact()
        draft = DraftInsight.model_validate(payload)
        report = verify_draft(draft, self.context)
        self.assertTrue(report.passed, report.model_dump())
        result = project_insight(draft, self.context, report)
        self.assertIn(payload["facts"][-1]["quote"], result.bank_view)
        self.assertNotIn(payload["impacts"][0]["mechanism"], result.bank_view)

    def numeric_fact(self, *, fact_id, source_id, value, period="2026", unit="процент", entity="Тестовый сектор"):
        quote = f"{entity}: ставка в период {period}, единица {unit}, значение {value}."
        return {
            "id": fact_id, "source_id": source_id, "quote": quote,
            "metric": "ставка", "entity": entity, "period": period, "unit": unit, "value": value,
        }

    def conflicting_payload(self):
        payload = self.payload()
        for index, value in enumerate((14, 13)):
            fact = self.numeric_fact(fact_id=f"fact:number{index}", source_id="temporary", value=value)
            source_id, _ = self.add_document(content=fact["quote"], bank=False, article_id=30 + index)
            fact["source_id"] = source_id
            payload["facts"].append(fact)
        return payload

    def test_same_entity_metric_unit_and_period_conflict_must_be_acknowledged(self):
        payload = self.conflicting_payload()
        report = self.assert_refused(payload, "UNACKNOWLEDGED_CONTRADICTION")
        self.assertIn("CONTRADICTORY_SOURCES", report.warnings)

    def test_acknowledged_conflict_cannot_be_used_as_causal_premise(self):
        payload = self.conflicting_payload()
        payload["acknowledged_conflicts"] = [["fact:number0", "fact:number1"]]
        payload["impacts"][0]["fact_ids"].append("fact:number0")
        self.assert_refused(payload, "CONTRADICTION_USED_AS_PREMISE")

    def test_acknowledged_conflict_can_be_reported_without_driving_the_conclusion(self):
        payload = self.conflicting_payload()
        payload["acknowledged_conflicts"] = [["fact:number0", "fact:number1"]]
        report = self.verify(payload)
        self.assertTrue(report.passed, report.model_dump())
        result = project_insight(DraftInsight.model_validate(payload), self.context, report)
        self.assertIn("Источники противоречат друг другу", result.ai_view)

    def test_different_period_or_entity_or_unit_does_not_create_a_false_conflict(self):
        for changed in ({"period": "2025"}, {"entity": "Другой тестовый сектор"}, {"unit": "пункт"}):
            with self.subTest(changed=changed):
                self.context = self.make_context()
                payload = self.payload()
                for index, value in enumerate((14, 13)):
                    fact = self.numeric_fact(fact_id=f"fact:number{index}", source_id="temporary", value=value,
                                             **(changed if index else {}))
                    source_id, _ = self.add_document(content=fact["quote"], bank=False, article_id=40 + index)
                    fact["source_id"] = source_id
                    payload["facts"].append(fact)
                report = self.verify(payload)
                self.assertTrue(report.passed, report.model_dump())
                self.assertNotIn("CONTRADICTORY_SOURCES", report.warnings)

    def test_numeric_value_and_scope_must_be_present_in_the_actual_quote(self):
        for mutate, code in (
            ({"value": 99}, "NUMBER_NOT_ANCHORED"),
            ({"entity": "Несуществующая организация"}, "NUMERIC_SCOPE_NOT_ANCHORED"),
            ({"period": None}, "NUMERIC_SCOPE_REQUIRED"),
        ):
            with self.subTest(mutate=mutate):
                self.context = self.make_context()
                fact = self.numeric_fact(fact_id="fact:number", source_id="temporary", value=14)
                source_id, _ = self.add_document(content=fact["quote"], bank=False)
                fact["source_id"] = source_id
                fact.update(mutate)
                payload = self.payload()
                payload["facts"].append(fact)
                self.assert_refused(payload, code)

    def test_numeric_scope_without_a_value_cannot_bypass_conflict_checks(self):
        fact = self.numeric_fact(fact_id="fact:number", source_id="temporary", value=14)
        source_id, _ = self.add_document(content=fact["quote"], bank=False)
        fact.update(source_id=source_id, value=None)
        payload = self.payload()
        payload["facts"].append(fact)
        self.assert_refused(payload, "NUMERIC_SCOPE_REQUIRED")

    def test_numeric_anchor_distinguishes_a_decimal_or_larger_number(self):
        for quoted_value in (14.5, 114):
            with self.subTest(quoted_value=quoted_value):
                self.context = self.make_context()
                fact = self.numeric_fact(fact_id="fact:number", source_id="temporary", value=quoted_value)
                source_id, _ = self.add_document(content=fact["quote"], bank=False)
                fact.update(source_id=source_id, value=14)
                payload = self.payload()
                payload["facts"].append(fact)
                self.assert_refused(payload, "NUMBER_NOT_ANCHORED")

    def test_numeric_anchor_accepts_sentence_punctuation_after_the_exact_number(self):
        fact = self.numeric_fact(fact_id="fact:number", source_id="temporary", value=14)
        self.assertTrue(fact["quote"].endswith("14."))
        source_id, _ = self.add_document(content=fact["quote"], bank=False)
        fact["source_id"] = source_id
        payload = self.payload()
        payload["facts"].append(fact)
        report = self.verify(payload)
        self.assertTrue(report.passed, report.model_dump())

    def test_material_published_after_the_analysis_event_is_refused(self):
        payload = self.with_bank_fact(published_at=self.analysis_date + timedelta(seconds=1))
        self.assert_refused(payload, "SOURCE_AFTER_ANALYSIS_DATE")

    def test_equivalent_publication_times_in_different_timezones_are_not_future(self):
        local_time = self.analysis_date.astimezone(timezone(timedelta(hours=3)))
        payload = self.with_bank_fact(published_at=local_time)
        self.assertTrue(self.verify(payload).passed)

    def test_historical_material_is_labelled_instead_of_becoming_current_bank_position(self):
        payload = self.with_bank_fact(published_at=self.analysis_date - timedelta(days=120))
        report = self.verify(payload)
        self.assertTrue(report.passed, report.model_dump())
        self.assertIn("HISTORICAL_SOURCE_CONTEXT", report.warnings)
        result = project_insight(DraftInsight.model_validate(payload), self.context, report)
        self.assertIn("Исторический контекст", result.bank_view)
        self.assertIn("не подтверждает текущие условия", result.bank_view)

    def test_insufficient_data_can_ask_for_clarification_but_cannot_keep_conclusions(self):
        payload = self.payload()
        payload.update(applicability="insufficient", impacts=[], actions=[],
                       missing_data=["Уточнить условия финансирования."],
                       clarification_questions=["Планируется ли новое финансирование?"])
        report = self.verify(payload)
        self.assertTrue(report.passed, report.model_dump())
        rejected = copy.deepcopy(payload)
        rejected["impacts"] = self.payload()["impacts"]
        self.assert_refused(rejected, "INSUFFICIENT_CONTEXT_HAS_CONCLUSIONS")
        payload["missing_data"] = []
        self.assert_refused(payload, "MISSING_CLARIFICATION")


if __name__ == "__main__":
    unittest.main()
