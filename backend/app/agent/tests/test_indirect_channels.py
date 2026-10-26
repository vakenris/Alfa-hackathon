"""Conditional economic transmission and analytical actions; no network or DB."""
import json
import unittest
from unittest.mock import patch

from backend.app.agent import AgentInsightEngine, FakeLLMClient, ManagementActionEngine, RecordingTracer
from backend.app.agent.config import AgentConfig
from backend.app.agent.context import prepare_context
from backend.app.agent.models import DraftInsight
from backend.app.agent.planning import channels_for_text
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.agent.verification import verify_draft


def demand_draft():
    value=valid_draft()
    value['impacts'][0].update(
        channel='demand',
        condition='Если покупатели строительных проектов зависят от доступности ипотечного финансирования',
        mechanism='Стоимость ипотеки может влиять на доступность сделок для покупателей и спрос на строительные проекты.',
        evidence_ids=['news:event','company:mainokved_name'],
        data_needs=['Структура заказов и зависимость покупателей от ипотечного финансирования.'],
    )
    value['actions'][0].update(
        title='Сверить банковский прогноз с допущениями по спросу',
        rationale='Проверить применимость сценария до подготовки решения.',
    )
    return value


class IndirectChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        guard.start();self.addCleanup(guard.stop)
        self.company,self.event=fixtures()
        self.company=self.company.model_copy(update={'mainokved_code':'41.20',
            'mainokved_name':'Строительство жилых и нежилых зданий'})
        self.context=await prepare_context(self.company,self.event,[],None,AgentConfig())

    def verify(self,value,context=None):
        return verify_draft(DraftInsight.model_validate(value),context or self.context)

    def test_rate_to_mortgage_to_demand_preserves_the_downstream_channel(self):
        self.assertNotIn('demand',channels_for_text(self.event.content))
        value=demand_draft();report=self.verify(value)
        self.assertTrue(report.passed,report.model_dump())
        self.assertIn('SEMANTIC_EXPERT_REVIEW_REQUIRED',report.warnings)

    async def test_transmission_is_not_specific_to_rates_or_construction(self):
        event=self.event.model_copy(update={'title':'Ограничены перевозки',
            'content':'Ограничены контейнерные перевозки по транспортному маршруту.'})
        context=await prepare_context(self.company,event,[],None,AgentConfig())
        value=valid_draft();value['impacts'][0].update(channel='inputs',
            condition='Если компания использует затронутый маршрут перевозок',
            mechanism='Стоимость альтернативных перевозок может влиять на себестоимость материалов.',
            data_needs=['Маршруты перевозок и условия поставок.'])
        self.assertNotIn('inputs',channels_for_text(event.content))
        report=self.verify(value,context)
        self.assertTrue(report.passed,report.model_dump())

    async def test_company_and_model_keywords_cannot_make_unrelated_news_relevant(self):
        event=self.event.model_copy(update={'title':'Шахматный турнир','content':'Завершился шахматный турнир.'})
        context=await prepare_context(self.company,event,[],None,AgentConfig())
        report=self.verify(demand_draft(),context)
        self.assertIn('UNSUPPORTED_CAUSAL_CHANNEL',[issue.code for issue in report.issues])

    def test_bridge_must_include_an_actual_event_topic_and_the_claimed_effect(self):
        value=demand_draft()
        for condition,mechanism in (
            ('Если компания планирует инвестиционные проекты','Спрос может влиять на решения компании.'),
            ('Если компания использует морские перевозки','Перевозки могут влиять на спрос на продукцию.'),
            ('Если покупатели используют ипотеку','Стоимость ипотеки может влиять на условия финансирования.'),
        ):
            with self.subTest(mechanism=mechanism):
                value['impacts'][0].update(condition=condition,mechanism=mechanism)
                self.assertIn('UNSUPPORTED_CAUSAL_CHANNEL',[issue.code for issue in self.verify(value).issues])

    def test_conditional_bridge_does_not_waive_fact_reference_number_or_certainty_checks(self):
        cases=(
            ('Компания получила кредит, поэтому стоимость ипотеки может влиять на спрос.','UNSUPPORTED_FACTUAL_PREMISE'),
            ('У компании есть ипотечные обязательства, поэтому спрос может зависеть от кредита.','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Признак кредитного продукта подтверждает наличие долга; ипотека может влиять на спрос.','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Стоимость ипотеки может снизить спрос на пять процентов.','UNSUPPORTED_INFERENCE_NUMBER'),
            ('Стоимость ипотеки гарантированно снизит спрос.','FALSE_CERTAINTY'),
            ('Банк прогнозирует спад спроса, который может зависеть от ипотеки.','UNSUPPORTED_BANK_ATTRIBUTION'),
        )
        for mechanism,code in cases:
            with self.subTest(code=code):
                value=demand_draft();value['impacts'][0]['mechanism']=mechanism
                self.assertIn(code,[issue.code for issue in self.verify(value).issues])
        value=demand_draft();value['impacts'][0]['evidence_ids'].append('company:foreign_debt')
        self.assertIn('UNKNOWN_EVIDENCE',[issue.code for issue in self.verify(value).issues])
        value=demand_draft();value['impacts'][0]['condition']='Покупатели компании используют ипотеку'
        self.assertIn('CONDITION_REQUIRED',[issue.code for issue in self.verify(value).issues])

    def test_sverit_and_noun_forms_are_analytical_actions(self):
        for title in ('Сверить банковский прогноз','Сверять предпосылки прогноза',
                      'Сверка банковского прогноза','Проверка графика расчётов',
                      'Сопоставление инвестиционных планов','Запрос актуальных банковских условий'):
            with self.subTest(title=title):
                value=demand_draft();value['actions'][0]['title']=title
                self.assertTrue(self.verify(value).passed)

    def test_new_analytical_verbs_do_not_waive_execution_or_premise_checks(self):
        for title,code in (
            ('Сверить прогноз и подписать кредитный договор','UNSUPPORTED_ACTION'),
            ('Сверка прогноза и покупка валюты','UNSUPPORTED_ACTION'),
            ('Сверить условия полученного компанией кредита','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Сверить бюджет, у компании есть кредитный долг','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Сверить: признак кредитного продукта доказывает наличие долга','UNSUPPORTED_FACTUAL_PREMISE'),
        ):
            with self.subTest(title=title):
                value=demand_draft();value['actions'][0]['title']=title
                self.assertIn(code,[issue.code for issue in self.verify(value).issues])

    async def test_public_agent_and_saved_qa_keep_demand_instead_of_relabelling_it(self):
        client=FakeLLMClient(json.dumps(demand_draft(),ensure_ascii=False))
        core=AgentInsightEngine(client)
        result=await core.generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(result.analysis.impacts[0].channel,'demand')
        self.assertEqual(len(client.prompts),1)
        answer=await ManagementActionEngine(core).answer_question('Какие риски для спроса?',
            analysis=result,company=self.company,tracer=RecordingTracer())
        self.assertEqual(answer.status,'complete')
        self.assertIn(result.analysis.impacts[0].mechanism,answer.summary)
        self.assertTrue(answer.source_refs)
        self.assertEqual(len(client.prompts),1)
        self.assertEqual(answer.report['llm_calls'],0)


if __name__=='__main__':unittest.main()
