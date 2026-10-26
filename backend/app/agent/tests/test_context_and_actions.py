"""Synthetic offline checks for context boundaries and deterministic follow-ups."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from pydantic import ValidationError

from backend.app.agent.action_models import ManagementActionRequest
from backend.app.agent.actions import ManagementActionEngine
from backend.app.agent.config import AgentConfig
from backend.app.agent.context import json_text, prepare_context
from backend.app.agent.financial import add_calculations, financial_calculations
from backend.app.agent.llm import FakeLLMClient
from backend.app.agent.models import AgentAnalysisResult, AgentContextError, AnalyticsDocument
from backend.app.agent.orchestrator import AgentInsightEngine
from backend.app.agent.runtime import RuntimeAgentEngine
from backend.app.agent.tools import MockAnalyticsSource
from backend.app.agent.tracer import RecordingTracer
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


def company():
    return CompanyResponse(id=1, synthetic_u="SYNTHETIC_TEST_A", name="Тестовая строительная компания",
                           mainokved_code="41.20", mainokved_name="Строительство зданий",
                           cl_product_loa_mark=True)


def event():
    return NewsArticleResponse(id=10, title="Ключевая ставка сохранена", source="Синтетическая новость",
                               published_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
                               content="Ключевая ставка сохранена на уровне 14%.", category="Экономика")


def row(uid="PEER_A", direction="PAYER", amount=10, count=1, name=None):
    return CounterpartyItem(synthetic_u=uid, name=name, direction=direction,
                            total_amount=amount, transaction_count=count)


def bank_document(identifier=100, *, days=-1, content="Банковская выдержка для локальной проверки.", **metadata):
    article = event().model_copy(update={"id": identifier, "title": "Тестовый банковский материал",
        "content": content, "is_bank_analytics": True, "published_at": event().published_at + timedelta(days=days)})
    values = {"source_document_id": "synthetic_pdf", "source_page": 3, "source_hash": "a" * 64,
              "source_url": "https://example.invalid/synthetic.pdf", "provenance_status": "verified"}
    return AnalyticsDocument(article=article, **{**values, **metadata})


def draft(*extra_ids):
    return {"applicability": "conditional", "impacts": [{
        "id": "financing", "channel": "financing", "direction": "mixed",
        "condition": "Если компания рассматривает новое финансирование",
        "mechanism": "Стоимость нового финансирования может влиять на инвестиционные решения.",
        "evidence_ids": ["news:event", "company:mainokved_code", *extra_ids],
        "data_needs": ["Условия финансирования и планы компании."],
    }], "actions": [{"title": "Проверить варианты финансирования.",
        "rationale": "Сравнение может уточнить условия и необходимые данные.", "impact_ids": ["financing"]}]}


def loan(**changes):
    return {"company_id": 1, "principal": "1000000", "currency": "RUB", "rate_type": "floating",
            "delta_contract_rate": "0.01", "days": 90, "day_count_basis": 360,
            "reset_within_horizon": True,
            "provenance": {"data_mode": "hypothetical", "reference": "Явные синтетические условия"}, **changes}


def budget(**changes):
    return {"company_id": 1, "available_cash": "500", "planned_capex": "1000", "currency": "RUB",
            "provenance": {"data_mode": "actual", "reference": "Синтетический бюджет"}, **changes}


class NetworkBlockedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.company, self.event = company(), event()


class ContextTests(NetworkBlockedTests):
    async def prepare(self, rows=(), source=None, **config):
        return await prepare_context(self.company, self.event, list(rows), source, AgentConfig(**config))

    async def test_payment_aliases_merge_and_own_transfers_do_not_dilute_external_share(self):
        context = await self.prepare([row(amount=30), row(direction="CUSTOMER", amount=20, count=2),
            row("PEER_B", "BENEFICIARY", 40), row(self.company.synthetic_u, "SUPPLIER", 100),
            row("PEER_C", "unexpected", 5)])
        summary = json.loads(context.evidence["payments:summary"].quote)
        self.assertEqual(summary["directions"]["incoming"]["total_amount"], 50)
        self.assertEqual(summary["directions"]["incoming"]["transaction_count"], 3)
        self.assertEqual(summary["directions"]["self_transfer"]["total_amount"], 100)
        self.assertEqual(summary["directions"]["unknown"]["total_amount"], 5)
        incoming = [item for key, item in context.evidence.items() if key.startswith("payments:incoming:")]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(json.loads(incoming[0].quote)["share_of_passed_direction_amount"], 1)
        self.assertEqual(context.business.self_transfer_rows, 1)

    async def test_top_counterparties_are_bounded_but_summary_uses_all_passed_rows(self):
        context = await self.prepare([row(f"PEER_{index}", amount=index) for index in range(50)])
        peers = [item for key, item in context.evidence.items() if key.startswith("payments:incoming:")]
        self.assertEqual(len(peers), 3)
        self.assertEqual(sorted(json.loads(item.quote)["total_amount"] for item in peers), [47, 48, 49])
        summary = json.loads(context.evidence["payments:summary"].quote)
        self.assertEqual(summary["directions"]["incoming"]["total_amount"], 1225)
        self.assertIn("Только переданная выборка", summary["scope"])
        self.assertNotIn("counterparties", context.payload)

    async def test_ambiguous_counterparty_names_are_not_arbitrarily_selected(self):
        context = await self.prepare([row(name="Первое имя"), row(name="Другое имя")])
        peer = next(item for key, item in context.evidence.items() if key.startswith("payments:incoming:"))
        self.assertIsNone(json.loads(peer.quote)["name"])
        self.assertTrue(json.loads(peer.quote)["name_conflict"])

    async def test_invalid_payment_values_are_rejected(self):
        invalid = [row(amount=float("nan")), row(amount=float("inf")), row(amount=-1), row(count=-1),
                   row().model_copy(update={"transaction_count": True})]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(AgentContextError):
                await self.prepare([item])

    async def test_oversized_inputs_and_aggregate_overflow_fail_safely(self):
        with self.assertRaises(AgentContextError):
            await self.prepare([row(), row()], max_input_counterparties=1)
        with self.assertRaises(AgentContextError):
            await self.prepare([row(amount=1e308), row(amount=1e308)])

    async def test_product_flags_do_not_create_contract_amounts_or_revenue_periods(self):
        self.company.revenue_company = 1000  # Dataset placeholder is not a usable revenue figure.
        context = await self.prepare()
        self.assertIs(context.business.known_features["cl_product_loa_mark"], True)
        self.assertIn("cl_product_lea_mark", context.business.unknown_features)
        self.assertNotIn("revenue_company", context.business.known_features)
        self.assertIsNone(context.business.revenue_year)
        self.assertIsNone(context.business.revenue_currency)
        self.assertIn("не доказывает получение кредита", " ".join(context.business.limitations))

    async def test_bank_search_is_bounded_and_preserves_metadata_while_excluding_future(self):
        class Source:
            calls = []
            async def search(source, query, *, company, event, limit):
                source.calls.append((query, company.id, event.id, limit))
                return [bank_document(), bank_document(101, days=1)]
        source = Source()
        context = await self.prepare(source=source)
        self.assertEqual(source.calls, [(self.event.title, 1, 10, 3)])
        evidence = context.evidence["bank:100:excerpt"]
        self.assertEqual((evidence.source_page, evidence.source_hash), (3, "a" * 64))
        self.assertEqual(evidence.provenance_status, "verified")
        self.assertEqual(evidence.published_at, bank_document().article.published_at)
        self.assertNotIn("bank:101:excerpt", context.evidence)
        self.assertIn("FUTURE_BANK_SOURCE_EXCLUDED", context.warnings)

    async def test_conflicting_document_identity_or_hash_cannot_supply_bank_quotes(self):
        cases = [([bank_document(), bank_document(content="Иная выдержка.")], "BANK_SOURCE_ID_CONFLICT"),
                 ([bank_document(), bank_document(101, source_hash="b" * 64)], "BANK_DOCUMENT_HASH_CONFLICT")]
        for documents, code in cases:
            with self.subTest(code=code):
                context = await self.prepare(source=MockAnalyticsSource(documents))
                self.assertIn(code, context.warnings)
                self.assertFalse(any(item.kind == "bank" for item in context.evidence.values()))

    async def test_source_failure_timeout_and_disabled_search_create_no_invented_sources(self):
        class FailingSource:
            async def search(self, *args, **kwargs):
                raise RuntimeError("Synthetic source failure")
        class SlowSource:
            async def search(self, *args, **kwargs):
                await asyncio.sleep(1)
        for source, options, code in [(FailingSource(), {}, "BANK_SOURCE_UNAVAILABLE_OR_INVALID"),
                                     (SlowSource(), {"source_timeout_seconds": .001}, "BANK_SOURCE_TIMEOUT")]:
            with self.subTest(code=code):
                context = await self.prepare(source=source, **options)
                self.assertIn(code, context.warnings)
                self.assertFalse(any(item.is_bank_analytics for item in context.sources))
        context = await self.prepare(source=FailingSource(), max_bank_documents=0)
        self.assertFalse(context.warnings)

    async def test_excerpt_and_budget_keep_only_exact_visible_evidence(self):
        text = "Законченная фраза о ставке. " * 80
        documents = [bank_document(index, content=text) for index in range(100, 103)]
        context = await self.prepare([row(f"PEER_{index}", amount=index) for index in range(50)],
                                    MockAnalyticsSource(documents), max_article_chars=200, max_context_chars=4500)
        self.assertLessEqual(len(json_text(context.payload)), 4500)
        self.assertEqual({item["id"] for item in context.payload["evidence"]}, set(context.evidence))
        self.assertEqual({item.source_id for item in context.evidence.values()},
                         {item.source_id for item in context.sources})
        for item in context.evidence.values():
            if item.kind == "bank":
                self.assertTrue(text.startswith(item.quote))
                self.assertTrue(item.quote.endswith("."))
                self.assertLessEqual(len(item.quote), 200)

    async def test_mock_documents_are_copied_on_input_and_output(self):
        document = bank_document()
        source = MockAnalyticsSource([document])
        document.article.content = "Изменён входной объект."
        first = (await source.search("", company=self.company, event=self.event, limit=1))[0]
        first.article.content = "Изменён возвращённый объект."
        second = (await source.search("", company=self.company, event=self.event, limit=1))[0]
        self.assertEqual(second.article.content, bank_document().article.content)


class FollowupTests(NetworkBlockedTests):
    async def asyncSetUp(self):
        self.client = FakeLLMClient(json.dumps(draft(), ensure_ascii=False))
        self.core = AgentInsightEngine(self.client)
        self.analysis = await self.core.generate_analysis(self.company, self.event, [], RecordingTracer())
        self.actions = ManagementActionEngine(self.core)

    async def ask(self, question, *, analysis=None, company=None):
        return await self.actions.answer_question(question, analysis=analysis or self.analysis,
                                                  company=company or self.company, tracer=RecordingTracer())

    async def test_qa_reuses_exact_sources_and_conditionals_without_llm_calls(self):
        before = len(self.client.prompts)
        answer = await self.ask("Что произошло и что делать?")
        self.assertEqual(answer.status, "complete")
        self.assertEqual(len(self.client.prompts), before)
        self.assertEqual(answer.report["llm_calls"], 0)
        self.assertEqual(answer.source_refs[0].quote, self.event.content)
        self.assertTrue(answer.proposed_actions)
        self.assertIn(self.analysis.analysis.impacts[0].condition, answer.summary)
        self.assertTrue(all(ref.source_url is None for ref in answer.source_refs))

    async def test_unknown_debt_contract_rates_and_numeric_effect_need_data_even_with_product_flag(self):
        for question in ("Какой долг у компании?", "Какая наша кредитная ставка?",
                         "На сколько увеличатся процентные расходы?", "Какой срок погашения?"):
            with self.subTest(question=question):
                answer = await self.ask(question)
                self.assertEqual(answer.status, "needs_data")
                self.assertFalse(answer.source_refs)
                self.assertTrue(answer.missing_data)

    async def test_unrelated_question_and_absent_bank_position_do_not_invent_answers(self):
        for question in ("Какова позиция Альфа-Банка?", "Кто выиграл футбольный матч?"):
            with self.subTest(question=question):
                answer = await self.ask(question)
                self.assertEqual(answer.status, "needs_data")
                self.assertFalse(answer.source_refs)

    async def test_profile_question_uses_known_uncited_value_after_json_roundtrip(self):
        self.company.region = "Москва"
        self.company.city = "Москва"
        analysis = await AgentInsightEngine(FakeLLMClient(json.dumps(draft(), ensure_ascii=False))).generate_analysis(
            self.company, self.event, [], RecordingTracer())
        restored = AgentAnalysisResult.model_validate_json(analysis.model_dump_json())
        self.assertFalse(any(item.company_feature == "region" for item in restored.evidence))
        before = restored.model_dump_json()
        answer = await self.ask("В каком регионе находится компания?", analysis=restored)
        self.assertEqual(answer.status, "complete")
        self.assertEqual(json.loads(answer.source_refs[0].quote), {"region": "Москва"})
        self.assertEqual(answer.source_refs[0].source_id, "company:1")
        self.assertIsNone(answer.source_refs[0].source_url)
        self.assertEqual(restored.model_dump_json(), before)
        changed = self.company.model_copy(update={"region": "Чужой регион"})
        self.assertEqual((await self.ask("В каком регионе?", analysis=restored, company=changed)).status,
                         "insufficient_evidence")

    async def test_unknown_profile_value_still_needs_data(self):
        self.assertEqual((await self.ask("В каком регионе находится компания?")).status, "needs_data")

    async def test_qa_rejects_changed_truncation_metadata(self):
        for target in ("evidence", "sources"):
            with self.subTest(target=target):
                changed = self.analysis.model_copy(deep=True)
                getattr(changed, target)[0].text_truncated = True
                answer = await self.ask("Что произошло?", analysis=changed)
                self.assertEqual(answer.status, "insufficient_evidence")

    async def test_company_and_event_identity_must_match_saved_analysis(self):
        wrong_uid = self.company.model_copy(update={"synthetic_u": "ANOTHER_UID"})
        self.assertEqual((await self.ask("Что произошло?", company=wrong_uid)).status, "insufficient_evidence")
        for field, value in (("company_synthetic_u", "ANOTHER_UID"), ("event_id", 999)):
            with self.subTest(field=field):
                changed = self.analysis.model_copy(deep=True)
                changed.report[field] = value
                self.assertEqual((await self.ask("Что произошло?", analysis=changed)).status, "insufficient_evidence")
        with self.assertRaisesRegex(ValueError, "ACTION_COMPANY_MISMATCH"):
            await self.ask("Что произошло?", company=self.company.model_copy(update={"id": 2}))

    async def test_failed_verification_and_tampered_quote_or_conditionals_are_rejected(self):
        changes = (lambda item: setattr(item.verification, "passed", False),
                   lambda item: setattr(item.evidence[0], "quote", "Выдуманная цитата."),
                   lambda item: setattr(item.analysis.impacts[0], "mechanism", "Компания получила кредит."),
                   lambda item: setattr(item.analysis.impacts[0], "condition", "Если компания меняет ранее проверенные планы"),
                   lambda item: setattr(item.analysis.actions[0], "title", "Проверить иной непроверенный план."))
        for change in changes:
            with self.subTest(change=change):
                changed = self.analysis.model_copy(deep=True)
                change(changed)
                answer = await self.ask("Что произошло и что делать?", analysis=changed)
                self.assertEqual(answer.status, "insufficient_evidence")
                self.assertFalse(answer.source_refs)

    async def test_unverified_bank_quote_is_explicitly_reported_without_official_attribution(self):
        source = MockAnalyticsSource([bank_document(provenance_status="unverified")])
        core = AgentInsightEngine(FakeLLMClient(json.dumps(draft("bank:100:excerpt"), ensure_ascii=False)),
                                  analytics_source=source)
        analysis = await core.generate_analysis(self.company, self.event, [], RecordingTracer())
        answer = await self.ask("Что говорит Альфа-Банк?", analysis=analysis)
        self.assertEqual(answer.status, "complete")
        self.assertEqual(answer.source_refs[0].quote, bank_document().article.content)
        self.assertEqual(answer.source_refs[0].attribution, "reported_source")
        self.assertIn("unverified", answer.summary)

    async def test_rate_direction_does_not_replace_explicit_contract_delta_or_use_budget_as_loan(self):
        request = ManagementActionRequest(kind="rate_scenario", company_id=1, rate_direction="down", loan_sensitivity=loan())
        result = await self.actions.analyze_action(request, analysis=self.analysis, company=self.company, tracer=RecordingTracer())
        self.assertEqual(Decimal(result.calculations[0]["amount"]), Decimal("2500"))
        request = ManagementActionRequest(kind="rate_scenario", company_id=1, rate_direction="up", financial_inputs=budget())
        result = await self.actions.analyze_action(request, analysis=self.analysis, company=self.company, tracer=RecordingTracer())
        self.assertEqual(result.status, "needs_data")
        self.assertFalse(result.calculations)


class FinancialTests(NetworkBlockedTests):
    async def test_runtime_facade_forwards_financial_inputs_and_retains_companion(self):
        clients = []
        def client_factory():
            client = FakeLLMClient(json.dumps(draft("calculation:simple_interest_cost_delta"), ensure_ascii=False))
            clients.append(client)
            return client
        runtime = RuntimeAgentEngine(client_factory=client_factory, source_factory=lambda: MockAnalyticsSource(),
                                     allow_test_dependencies=True)
        analysis = await runtime.generate_analysis(self.company, self.event, [], RecordingTracer(),
                                                   loan_sensitivity=loan(), financial_inputs=budget())
        restored = AgentAnalysisResult.model_validate_json(analysis.model_dump_json())
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(clients[0].prompts), 1)
        self.assertEqual({item["kind"] for item in restored.calculations},
                         {"simple_interest_cost_delta", "cash_after_planned_capex"})
        self.assertTrue(restored.verification.passed)
        self.assertEqual(restored.report["company_synthetic_u"], self.company.synthetic_u)

    async def test_saved_calculation_questions_show_exact_inputs_and_arithmetic_for_both_modes(self):
        for mode in ("actual", "hypothetical"):
            with self.subTest(mode=mode):
                inputs = loan(provenance={"data_mode": mode, "reference": "Явные параметры синтетического договора"})
                client = FakeLLMClient(json.dumps(draft("calculation:simple_interest_cost_delta"), ensure_ascii=False))
                core = AgentInsightEngine(client)
                analysis = await core.generate_analysis(self.company, self.event, [], RecordingTracer(), loan_sensitivity=inputs)
                restored = AgentAnalysisResult.model_validate_json(analysis.model_dump_json())
                for question in ("Какую сумму кредита вы использовали в расчёте?",
                                 "На сколько изменятся процентные расходы в рассчитанном сценарии?",
                                 "Какая формула и период использованы для чувствительности?"):
                    with self.subTest(question=question):
                        answer = await ManagementActionEngine(core).answer_question(
                            question, analysis=restored, company=self.company, tracer=RecordingTracer())
                        self.assertEqual(answer.status, "complete")
                        self.assertEqual(answer.calculations, restored.calculations)
                        quote = json.loads(answer.source_refs[0].quote)
                        self.assertEqual(quote["principal"], "1000000")
                        self.assertEqual(Decimal(quote["amount"]), Decimal("2500"))
                        self.assertEqual((quote["days"], quote["day_count_basis"], quote["currency"]), (90, 360, "RUB"))
                        self.assertEqual(quote["provenance"], inputs["provenance"])
                        self.assertIn("formula", quote)
                        self.assertIn("независимо не проверены", answer.summary)
                        self.assertEqual(answer.source_refs[0].provenance_status, "unverified")
                        self.assertEqual(answer.report["llm_calls"], 0)
                self.assertEqual(len(client.prompts), 1)

    async def test_calculation_not_selected_by_llm_is_still_explainable(self):
        core = AgentInsightEngine(FakeLLMClient(json.dumps(draft(), ensure_ascii=False)))
        analysis = await core.generate_analysis(self.company, self.event, [], RecordingTracer(), loan_sensitivity=loan())
        self.assertFalse(any(key.startswith("calculation:") for item in analysis.analysis.impacts
                             for key in item.evidence_ids))
        restored = AgentAnalysisResult.model_validate_json(analysis.model_dump_json())
        answer = await ManagementActionEngine(core).answer_question("Объясни расчёт чувствительности.",
            analysis=restored, company=self.company, tracer=RecordingTracer())
        self.assertEqual(answer.status, "complete")
        self.assertEqual(Decimal(answer.calculations[0]["amount"]), 2500)
        changed = restored.model_copy(deep=True)
        changed.calculations[0]["provenance"] = {"data_mode": "actual", "reference": "Подмена"}
        self.assertEqual((await ManagementActionEngine(core).answer_question("Объясни расчёт.",
            analysis=changed, company=self.company, tracer=RecordingTracer())).status, "insufficient_evidence")

    async def test_workforce_categories_remain_separate_and_available_after_roundtrip(self):
        company=self.company.model_copy(update={'amount_workers_fns':'251–500','amount_workers_spark':'101–250',
                                               'company_age_years':'Свыше десяти лет'})
        client=FakeLLMClient(json.dumps(draft(),ensure_ascii=False));core=AgentInsightEngine(client)
        analysis=await core.generate_analysis(company,self.event,[],RecordingTracer())
        restored=AgentAnalysisResult.model_validate_json(analysis.model_dump_json())
        helper=ManagementActionEngine(core)
        answer=await helper.answer_question('Какие диапазоны численности работников указаны в профиле?',
            analysis=restored,company=company,tracer=RecordingTracer())
        self.assertEqual(answer.status,'complete')
        self.assertEqual({ref.fact_id for ref in answer.source_refs},
                         {'company:amount_workers_fns','company:amount_workers_spark'})
        self.assertIn('251–500',answer.summary);self.assertIn('101–250',answer.summary)
        changed=company.model_copy(update={'amount_workers_fns':'501–1000'})
        invalid=await helper.answer_question('Какая численность работников?',analysis=restored,company=changed,tracer=RecordingTracer())
        self.assertEqual(invalid.status,'insufficient_evidence')
        self.assertEqual(len(client.prompts),1)

    async def test_calculation_does_not_establish_actual_debt_or_bank_position(self):
        core = AgentInsightEngine(FakeLLMClient(json.dumps(draft(), ensure_ascii=False)))
        analysis = await core.generate_analysis(self.company, self.event, [], RecordingTracer(), loan_sensitivity=loan())
        helper = ManagementActionEngine(core)
        for question in ("Какой реальный долг у компании в этом сценарии?", "Какой кредит действительно получен компанией?",
                         "Есть ли у нас заём?", "Какие параметры расчёта использовал Альфа-Банк?"):
            with self.subTest(question=question):
                answer = await helper.answer_question(question, analysis=analysis, company=self.company, tracer=RecordingTracer())
                self.assertEqual(answer.status, "needs_data")
                self.assertFalse(answer.source_refs)
        wrong_company = self.company.model_copy(update={"synthetic_u": "OTHER_COMPANY"})
        answer = await helper.answer_question("Объясни расчёт.", analysis=analysis, company=wrong_company, tracer=RecordingTracer())
        self.assertEqual(answer.status, "insufficient_evidence")

    async def test_budget_calculation_question_does_not_return_loan_sensitivity(self):
        core = AgentInsightEngine(FakeLLMClient(json.dumps(draft(), ensure_ascii=False)))
        analysis = await core.generate_analysis(self.company, self.event, [], RecordingTracer(),
                                                loan_sensitivity=loan(), financial_inputs=budget())
        answer = await ManagementActionEngine(core).answer_question("Объясни расчёт бюджета и CAPEX.",
            analysis=analysis, company=self.company, tracer=RecordingTracer())
        self.assertEqual(answer.status, "complete")
        self.assertEqual([item["kind"] for item in answer.calculations], ["cash_after_planned_capex"])
        self.assertEqual(Decimal(answer.calculations[0]["amount"]), -500)

    async def test_decimal_sensitivity_keeps_units_formula_and_declared_provenance(self):
        result = financial_calculations(1, loan_sensitivity=loan())[0]
        self.assertEqual(Decimal(result["amount"]), Decimal("2500"))
        self.assertEqual(result["currency"], "RUB")
        self.assertEqual(result["formula"], "principal * delta_contract_rate * days / day_count_basis")
        self.assertEqual(result["provenance"]["data_mode"], "hypothetical")
        self.assertFalse(result["is_financial_forecast"])
        self.assertIn("независимо не проверены", result["description"])

    async def test_fixed_zero_and_explicit_negative_delta_are_supported(self):
        fixed = financial_calculations(1, loan_sensitivity=loan(rate_type="fixed", delta_contract_rate="0", reset_within_horizon=False))[0]
        self.assertEqual(Decimal(fixed["amount"]), 0)
        lower = financial_calculations(1, loan_sensitivity=loan(delta_contract_rate="-0.01"))[0]
        self.assertEqual(Decimal(lower["amount"]), -2500)

    async def test_invalid_and_cross_company_parameters_are_rejected(self):
        cases = [loan(company_id=2), loan(rate_type="fixed"), loan(reset_within_horizon=False),
                 loan(principal=True), loan(principal="NaN"), loan(principal="Infinity"),
                 loan(day_count_basis=True), loan(days=0), loan(delta_contract_rate="2")]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                financial_calculations(1, loan_sensitivity=values)

    async def test_budget_comparison_requires_actual_declaration_without_claiming_total_deficit(self):
        result = financial_calculations(1, financial_inputs=budget())[0]
        self.assertEqual(Decimal(result["amount"]), -500)
        self.assertIn("не характеризует полный бюджет", result["description"])
        hypothetical = budget(provenance={"data_mode": "hypothetical", "reference": "Сценарий"})
        self.assertEqual(financial_calculations(1, financial_inputs=hypothetical), [])

    async def test_calculation_evidence_matches_visible_formula_and_ownership(self):
        context = await prepare_context(self.company, self.event, [], None, AgentConfig())
        calculations = add_calculations(context, loan_sensitivity=loan(), financial_inputs=budget())
        self.assertEqual(len(calculations), 2)
        for calculation in calculations:
            evidence = context.evidence["calculation:" + calculation["kind"]]
            self.assertEqual(json.loads(evidence.quote), calculation)
            self.assertEqual(evidence.provenance_status, "unverified")
            self.assertTrue(evidence.source_id.startswith("calculation:1:"))
        self.assertEqual({item["id"] for item in context.payload["evidence"]}, set(context.evidence))
        before = json_text(context.payload)
        with self.assertRaises(AgentContextError):
            add_calculations(context, loan_sensitivity=loan(company_id=2))
        self.assertEqual(json_text(context.payload), before)

    async def test_recomputed_arithmetic_and_provenance_tampering_are_not_accepted_by_qa(self):
        response = json.dumps(draft("calculation:simple_interest_cost_delta"), ensure_ascii=False)
        core = AgentInsightEngine(FakeLLMClient(response))
        analysis = await core.generate_analysis(self.company, self.event, [], RecordingTracer(), loan_sensitivity=loan())
        helper = ManagementActionEngine(core)
        self.assertEqual((await helper.answer_question("Что произошло?", analysis=analysis, company=self.company, tracer=RecordingTracer())).status, "complete")
        for key, value in (("amount", "999999"), ("company_id", 2), ("provenance", {"data_mode": "actual", "reference": "Подмена"})):
            with self.subTest(key=key):
                changed = analysis.model_copy(deep=True)
                changed.calculations[0][key] = value
                result = await helper.answer_question("Что произошло?", analysis=changed, company=self.company, tracer=RecordingTracer())
                self.assertEqual(result.status, "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
