"""Action grammar and lossless, bounded repair preparation. No provider or DB IO."""
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from backend.app.agent import AgentConfig, AgentInsightEngine, FakeLLMClient, RecordingTracer
from backend.app.agent.context import json_text, prepare_context
from backend.app.agent.models import AgentContextError, AgentEvidenceError, AnalyticsDocument, CheckIssue, DraftInsight
from backend.app.agent.prompts import build_prompt, recovery_context
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.agent.tools import MockAnalyticsSource
from backend.app.agent.verification import verify_draft
from backend.app.schemas.company import CounterpartyItem


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        guard.start();self.addCleanup(guard.stop)
        self.company,self.event=fixtures()
        self.context=await prepare_context(self.company,self.event,[],None,AgentConfig())

    def issues(self, title, *, rationale=None):
        value=valid_draft();value['actions'][0]['title']=title
        if rationale is not None: value['actions'][0]['rationale']=rationale
        return verify_draft(DraftInsight.model_validate(value),self.context).issues

    def test_analytic_nouns_and_infinitives_are_valid_without_repair(self):
        for title in (
            'Сверка кредитного и лизингового портфеля',
            'Проверка графика расчётов и ликвидности',
            'Сопоставление инвестиционных планов',
            'Запрос актуальных банковских условий',
            'Анализ вариантов финансирования',
            'Проверка условий получения и погашения кредита',
            'Проверка условий фиксированных и плавающих ставок',
            'Проверить условия фиксированных и плавающих ставок',
        ):
            with self.subTest(title=title): self.assertEqual(self.issues(title),[])

    def test_analytic_prefix_does_not_allow_execution_verbs_or_nouns(self):
        for title in (
            'Получение кредита для модернизации', 'Погашение кредита',
            'Проверка бюджета и взять кредит', 'Сверка условий и подписать договор',
            'Сравнение вариантов и покупка валюты', 'Проверка графика; погашение кредита',
            'Проверка бюджета, покупка валюты',
            'Сверка бюджета. Закрытие производства', 'Проверка бюджета и сокращение штата',
        ):
            with self.subTest(title=title):
                self.assertIn('UNSUPPORTED_ACTION',[issue.code for issue in self.issues(title)])

    def test_analytic_titles_do_not_exempt_premises_numbers_or_bank_attribution(self):
        cases=(
            ('Сверка полученного компанией кредита','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Проверка бюджета: у компании есть кредитный долг','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Проверка: признак кредитного продукта подтверждает наличие долга','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Проверка экономии пяти процентов','UNSUPPORTED_INFERENCE_NUMBER'),
            ('Сверка: банк рекомендует кредит','UNSUPPORTED_BANK_ATTRIBUTION'),
            ('Проверка гарантированного снижения расходов','FALSE_CERTAINTY'),
        )
        for title,code in cases:
            with self.subTest(title=title):
                issues=self.issues(title)
                self.assertIn((code,'actions[0].title'),[(issue.code,issue.path) for issue in issues])
        for rationale,code in (
            ('Компания получила кредит, поэтому расходы могут зависеть от ставки.','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Стоимость обслуживания плавающей ставки может влиять на расходы.','UNSUPPORTED_COMPANY_CONDITION'),
            ('Признак кредитного продукта подтверждает наличие долга.','UNSUPPORTED_FACTUAL_PREMISE'),
        ):
            with self.subTest(rationale=rationale):
                issues=self.issues('Сверка кредитного портфеля',rationale=rationale)
                self.assertIn((code,'actions[0].rationale'),[(issue.code,issue.path) for issue in issues])

    async def bank_payload(self):
        article=self.event.model_copy(update={
            'id':9,'is_bank_analytics':True,'published_at':self.event.published_at-timedelta(days=1),
            'content':'Прогноз на конец 2027 года: 12%. Снижение не гарантировано; при иных условиях прогноз меняется.',
        })
        document=AnalyticsDocument(article=article,source_url='https://bank.invalid/review',
            source_document_id='fixture:bank',source_page=3,source_hash='a'*64,provenance_status='unverified')
        rows=[CounterpartyItem(synthetic_u='PEER',direction='BENEFICIARY',total_amount=50,transaction_count=2)]
        context=await prepare_context(self.company,self.event,rows,MockAnalyticsSource([document]),AgentConfig())
        context.payload['agent_context']={'plan':{'steps':['inspect_company','search_bank']},
            'observations':[{'tool':'search_bank','status':'ok'}],
            'adaptations':[{'issues':[{'code':'SENT_ONLY_ONCE'}]}], 'remaining_llm_calls':1}
        return context.payload

    async def test_repair_is_lossless_for_evidence_and_constraints_without_execution_history(self):
        payload=await self.bank_payload();before=copy.deepcopy(payload)
        compact=recovery_context(payload)
        restored=[{**compact['source_metadata'][item['source_id']],**item} for item in compact['evidence']]
        self.assertEqual(restored,payload['evidence'])
        self.assertEqual(compact['business_context'],payload['business_context'])
        self.assertEqual(compact['event'],payload['event'])
        self.assertEqual(compact['warnings'],payload['warnings'])
        self.assertNotIn('known_features',compact['company'])
        self.assertNotIn('observations',compact['agent_context'])
        self.assertNotIn('adaptations',compact['agent_context'])
        self.assertFalse(compact['agent_context']['can_request_bank_search'])
        draft=DraftInsight.model_validate(valid_draft())
        issue=CheckIssue(code='UNSUPPORTED_ACTION',path='actions[0].title',excerpt='Сравнить предложения')
        prompt=build_prompt(payload,draft=draft,issues=[issue])
        self.assertEqual(prompt.count('"code":"UNSUPPORTED_ACTION"'),1)
        self.assertNotIn('SENT_ONLY_ONCE',prompt)
        self.assertEqual(prompt.count(draft.model_dump_json(exclude_defaults=True)),1)
        self.assertEqual(payload,before)
        self.assertIn('https://bank.invalid/review',prompt)
        self.assertIn('a'*64,prompt)
        self.assertIn('Снижение не гарантировано',prompt)

    async def test_unknown_draft_reference_and_text_cannot_become_source_facts(self):
        payload=await self.bank_payload()
        bad=valid_draft();bad['impacts'][0]['evidence_ids'].append('company:invented_credit')
        bad['impacts'][0]['mechanism']='Компания получила кредит, что может влиять на расходы.'
        draft=DraftInsight.model_validate(bad)
        prompt=build_prompt(payload,draft=draft,issues=[CheckIssue(code='UNKNOWN_EVIDENCE',path='impacts[0].evidence_ids')])
        sent=json.loads(prompt.split('Входные данные (JSON):\n',1)[1])
        self.assertNotIn('company:invented_credit',{item['id'] for item in sent['evidence']})
        self.assertEqual(len(sent['evidence']),len(payload['evidence']))
        self.assertNotIn('Компания получила кредит',[item['quote'] for item in sent['evidence']])

    async def test_exact_prompt_and_context_bounds_fail_closed_without_truncation(self):
        payload=await self.bank_payload();draft=DraftInsight.model_validate(valid_draft())
        issues=[CheckIssue(code='FALSE_CERTAINTY',path='impacts[0].mechanism',excerpt='гарантированно')]
        prompt=build_prompt(payload,draft=draft,issues=issues)
        context_size=len(json_text(recovery_context(payload)))
        self.assertEqual(build_prompt(payload,draft=draft,issues=issues,max_prompt_chars=len(prompt),
                                     max_context_chars=context_size),prompt)
        for limits in ({'max_prompt_chars':len(prompt)-1},{'max_context_chars':context_size-1}):
            with self.subTest(limits=limits),self.assertRaisesRegex(AgentContextError,'PROMPT_LIMIT'):
                build_prompt(payload,draft=draft,issues=issues,**limits)

    async def test_conflicting_source_metadata_is_not_silently_merged(self):
        payload=await self.bank_payload()
        item=copy.deepcopy(payload['evidence'][1]);item['id']='company:conflict'
        item['provenance_status']='verified';payload['evidence'].append(item)
        with self.assertRaisesRegex(AgentContextError,'RECOVERY_SOURCE_METADATA_CONFLICT'):
            recovery_context(payload)

    async def test_actual_agent_recovery_uses_compact_context_and_still_verifies_replacement(self):
        bad=valid_draft();bad['impacts'][0]['mechanism']='Компания получила кредит, что может влиять на расходы.'
        for replacement,should_pass in ((valid_draft(),True),(bad,False)):
            client=FakeLLMClient(responses=[json.dumps(bad),json.dumps(replacement)])
            engine=AgentInsightEngine(client)
            if should_pass:
                result=await engine.generate_analysis(self.company,self.event,[],RecordingTracer())
                self.assertTrue(result.verification.passed)
                self.assertEqual(result.report['validation_summary']['repairs'],1)
            else:
                with self.assertRaises(AgentEvidenceError):
                    await engine.generate_analysis(self.company,self.event,[],RecordingTracer())
            self.assertEqual(len(client.prompts),2)
            second=json.loads(client.prompts[1].split('Входные данные (JSON):\n',1)[1])
            self.assertIn('source_metadata',second)
            self.assertNotIn('adaptations',second['agent_context'])

    async def test_recovery_size_failure_does_not_spend_second_call(self):
        bad=valid_draft();bad['actions'][0]['title']='Взять кредит'
        bad['actions'][0]['rationale']='Нужна проверка условий. '*20
        probe=FakeLLMClient(json.dumps(valid_draft()))
        await AgentInsightEngine(probe).generate_analysis(self.company,self.event,[],RecordingTracer())
        first_size=len(probe.prompts[0])
        # A validated first prompt still fits; the complete repair must be checked separately.
        config=AgentConfig(max_prompt_chars=first_size)
        client=FakeLLMClient(responses=[json.dumps(bad),json.dumps(valid_draft())])
        with self.assertRaisesRegex(AgentContextError,'PROMPT_LIMIT'):
            await AgentInsightEngine(client,config=config).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(len(client.prompts),1)


if __name__=='__main__': unittest.main()
