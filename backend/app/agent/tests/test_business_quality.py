"""Specific business-quality regressions; no model, network, settings or DB."""
import copy
import json
import unittest
from unittest.mock import patch

from backend.app.agent import AgentInsightEngine, FakeLLMClient, ManagementActionEngine, RecordingTracer
from backend.app.agent.config import AgentConfig
from backend.app.agent.context import prepare_context
from backend.app.agent.financial import add_calculations
from backend.app.agent.models import AgentAnalysisResult, DraftInsight
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.agent.tests.test_context_and_actions import budget
from backend.app.agent.verification import project_insight, selected_evidence, verify_draft
from backend.app.schemas.company import CounterpartyItem


class BusinessQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        network=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        network.start();self.addCleanup(network.stop)
        self.company,self.event=fixtures()
        self.context=await prepare_context(self.company,self.event,[],None,AgentConfig())

    def verify(self,value):
        return verify_draft(DraftInsight.model_validate(value),self.context)

    def test_coordinated_unknown_conditions_stay_hypothetical(self):
        for condition in (
            'Если денежно-кредитные условия остаются жесткими, а у компании есть чувствительный к ставке долг',
            'Если ставка сохраняется, и если у компании есть потребность в рефинансировании',
        ):
            with self.subTest(condition=condition):
                value=valid_draft();value['impacts'][0]['condition']=condition
                self.assertTrue(self.verify(value).passed)

    def test_if_prefix_cannot_hide_a_completed_event_or_independent_assertion(self):
        for condition in (
            'Если ставка сохраняется, компания уже получила кредит',
            'Если ставка сохраняется, а компания уже получила кредит',
            'Если ставка сохраняется. У компании есть кредит',
            'Если ставка сохраняется; и у компании есть кредит',
            'Если ставка сохраняется, у компании есть кредит',
        ):
            with self.subTest(condition=condition):
                value=valid_draft();value['impacts'][0]['condition']=condition
                self.assertIn('UNSUPPORTED_FACTUAL_PREMISE',[issue.code for issue in self.verify(value).issues])

    def test_saved_B01_invented_expectations_are_rejected_in_every_inference(self):
        sentence='Стоимость обслуживания долга может остаться выше ожиданий снижения.'
        for field in ('mechanism','outcome','rationale'):
            value=valid_draft()
            if field=='mechanism': value['impacts'][0][field]=sentence
            elif field=='rationale': value['actions'][0][field]=sentence
            else:
                value['scenarios']=[{'label':'Сохранение условий','condition':'Если требуется новое финансирование',
                    'outcome':sentence,'evidence_ids':['news:event','company:mainokved_code']}]
            with self.subTest(field=field):
                self.assertIn('UNSUPPORTED_COMPARISON_BASELINE',[issue.code for issue in self.verify(value).issues])

    def test_unchanged_decision_cannot_become_a_new_cost_increase(self):
        for mechanism in (
            'Сохранение ключевой ставки может повысить стоимость нового финансирования.',
            'Сохранение ставки может привести к увеличению стоимости кредита.',
            'Сохранение ставки не означает снижения расходов, но может повысить стоимость нового финансирования.',
            'Сохранение ставки не влечёт изменения договоров и может увеличить стоимость кредита.',
        ):
            value=valid_draft();value['impacts'][0]['mechanism']=mechanism
            with self.subTest(mechanism=mechanism):
                self.assertIn('UNSUPPORTED_COMPARISON_BASELINE',[issue.code for issue in self.verify(value).issues])
        for mechanism in (
            'Сохранение ставки не означает увеличения стоимости кредита; стоимость финансирования может влиять на выбор вариантов.',
            'Стоимость нового финансирования может оставаться значимым условием инвестиционных решений.',
        ):
            value=valid_draft();value['impacts'][0]['mechanism']=mechanism
            with self.subTest(mechanism=mechanism): self.assertTrue(self.verify(value).passed)

    def test_explicitly_hypothetical_planning_expectation_is_a_comparison_base(self):
        value=valid_draft();value['impacts'][0].update(
            condition='Если в ожиданиях компании было заложено снижение стоимости финансирования',
            mechanism='Стоимость капитала может оказаться выше ожиданий снижения.')
        self.assertTrue(self.verify(value).passed)

    def test_saved_B01_contract_question_does_not_assert_a_rate_type(self):
        value=valid_draft();value['actions'][0]['rationale']=(
            'Нужно понять, есть ли заимствования с плавающей ставкой или предстоящее рефинансирование, '
            'чтобы оценить влияние сохранения ставки на процентные расходы.')
        self.assertTrue(self.verify(value).passed)
        for suffix,code in (
            (' Стоимость обслуживания плавающей ставки может влиять на расходы.','UNSUPPORTED_COMPANY_CONDITION'),
            (' У компании есть кредитный долг.','UNSUPPORTED_FACTUAL_PREMISE'),
        ):
            with self.subTest(suffix=suffix):
                changed=copy.deepcopy(value);changed['actions'][0]['rationale']+=suffix
                self.assertIn(code,[issue.code for issue in self.verify(changed).issues])

    def test_sample_concentration_cannot_prove_distress(self):
        for mechanism in (
            'Концентрация платежей доказывает кассовый дефицит, который может потребовать нового финансирования.',
            'Переданная платёжная выборка подтверждает зависимость бизнеса; это может влиять на финансирование.',
        ):
            value=valid_draft();value['impacts'][0]['mechanism']=mechanism
            with self.subTest(mechanism=mechanism):
                self.assertIn('UNSUPPORTED_PAYMENT_CONCLUSION',[issue.code for issue in self.verify(value).issues])
        value=valid_draft();value['impacts'][0]['mechanism']=(
            'Концентрация платежей не доказывает кассовый дефицит; график расчётов может влиять на потребность в финансировании.')
        self.assertTrue(self.verify(value).passed)

    def test_projection_links_each_existing_mechanism_to_its_own_facts(self):
        value=valid_draft();second=copy.deepcopy(value['impacts'][0])
        second.update(id='capex',channel='investment',
            condition='Если компания рассматривает модернизацию производства стали',
            mechanism='Стоимость нового кредита может влиять на выбор этапности инвестиций.',
            evidence_ids=['news:event','company:mainokved_name'])
        value['impacts'].append(second)
        draft=DraftInsight.model_validate(value);verification=self.verify(value)
        self.assertTrue(verification.passed)
        projected=project_insight(draft,self.context,verification)
        for impact in draft.impacts:
            self.assertIn(impact.mechanism,projected.why_important)
        self.assertIn('Отрасль: Производство стали [Профиль компании]',projected.why_important)
        self.assertIn('ОКВЭД: 24.10 [Профиль компании]',projected.why_important)

    async def test_peer_share_retains_its_denominator_in_public_evidence(self):
        rows=[CounterpartyItem(synthetic_u='A',direction='SUPPLIER',total_amount=70,transaction_count=1),
              CounterpartyItem(synthetic_u='B',direction='SUPPLIER',total_amount=30,transaction_count=1)]
        context=await prepare_context(self.company,self.event,rows,None,AgentConfig())
        peer=next(key for key in context.evidence if key.startswith('payments:outgoing:'))
        value=valid_draft();value['impacts'][0]['evidence_ids'].append(peer)
        draft=DraftInsight.model_validate(value);verification=verify_draft(draft,context)
        projected=project_insight(draft,context,verification)
        self.assertIn('payments:summary',{item.id for item in selected_evidence(draft,context)})
        self.assertTrue(any('largest_group' in item.claim for item in projected.evidences))
        self.assertIn('100.0 / 100.0 = 100.0%',projected.why_important)
        self.assertIn('период, валюта и полнота неизвестны',projected.why_important)

    def test_calculation_only_anchor_keeps_its_provenance_limitations(self):
        calculations=add_calculations(self.context,financial_inputs=budget())
        value=valid_draft();value['impacts'][0]['evidence_ids']=['news:event','calculation:cash_after_planned_capex']
        draft=DraftInsight.model_validate(value);verification=self.verify(value)
        self.assertTrue(verification.passed)
        projected=project_insight(draft,self.context,verification,calculations)
        self.assertIn(calculations[0]['description'],projected.why_important)
        self.assertIn('независимо не проверены',projected.why_important)

    async def test_public_projection_version_preserves_legacy_saved_qa(self):
        result=await AgentInsightEngine(FakeLLMClient(json.dumps(valid_draft()))).generate_analysis(
            self.company,self.event,[],RecordingTracer())
        self.assertEqual(result.report['projection_version'],2)
        # Build the old format, then emulate a stored bundle without a version.
        legacy=result.model_copy(deep=True)
        legacy.insight=project_insight(legacy.analysis,self.context,legacy.verification,projection_version=1)
        legacy.evidence=selected_evidence(legacy.analysis,self.context,projection_version=1)
        legacy.report.pop('projection_version')
        for bundle in (result,AgentAnalysisResult.model_validate_json(legacy.model_dump_json())):
            answer=await ManagementActionEngine(None).answer_question(
                'Что произошло и какие источники?',analysis=bundle,company=self.company,tracer=RecordingTracer())
            self.assertEqual(answer.status,'complete')
            self.assertEqual(answer.report['llm_calls'],0)


if __name__=='__main__': unittest.main()
