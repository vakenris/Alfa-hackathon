"""Agent behavior and shared-LLM integration fixtures; no real API, environment or DB."""
import asyncio
import copy
from datetime import datetime, timezone, timedelta
import json
import unittest
from unittest.mock import patch

from backend.app.agent import AgentConfig,AgentInsightEngine,FakeLLMClient,RecordingTracer,MockAnalyticsSource
from backend.app.agent.models import AnalyticsDocument,AgentEvidenceError,AgentLLMError,AgentResponseError,AgentLLMTimeoutError,AgentTimeoutError
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.company import CompanyResponse,CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


def fixtures():
    return (CompanyResponse(id=1,synthetic_u='OFFLINE_U',name='Учебный завод',mainokved_code='24.10',mainokved_name='Производство стали'),
        NewsArticleResponse(id=2,title='Банк России сохранил ключевую ставку',content='Банк России решил сохранить ключевую ставку на уровне 14% годовых.',
                            source='Синтетический тест',published_at=datetime(2026,10,23,tzinfo=timezone.utc),category='Тест'))


class EmptySearchSource:
    async def search(self,query,*,company,event,limit): return []


def valid_draft():
    return {'applicability':'conditional','impacts':[{'id':'financing','channel':'financing','direction':'mixed',
        'condition':'Если компания рассматривает новое финансирование',
        'mechanism':'Стоимость заёмных средств может влиять на сравнение предложений и инвестиционные решения.',
        'evidence_ids':['news:event','company:mainokved_code'],'data_needs':['Планы финансирования и договорные предложения.']}],
        'actions':[{'title':'Сравнить предложения по новому финансированию','rationale':'Проверить доступные условия до подготовки решения.',
                    'impact_ids':['financing'],'required_data':['Планы финансирования и предложения кредиторов.'],'owner_role':'CFO'}],
        'missing_data':['Долг и условия договоров неизвестны.']}


class AgentBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.company,self.event=fixtures()
        self.guard=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.guard.start();self.addCleanup(self.guard.stop)

    async def run_draft(self,payload=None,*,source=None,config=None,company=None,event=None,peers=()):
        client=FakeLLMClient(json.dumps(payload or valid_draft(),ensure_ascii=False));trace=RecordingTracer()
        result=await AgentInsightEngine(client,analytics_source=source,config=config).generate_analysis(
            company or self.company,event or self.event,list(peers),trace)
        return result,client,trace

    async def test_public_contract_plan_tools_and_references_are_real(self):
        r,c,t=await self.run_draft(source=EmptySearchSource())
        self.assertEqual(set(r.insight.model_dump()),set(MLInsightResult.model_fields))
        self.assertEqual(len(c.prompts),1)
        self.assertEqual(r.insight.evidences[0].claim,self.event.content)
        self.assertTrue(r.verification.passed)
        self.assertEqual(r.report['company_synthetic_u'],self.company.synthetic_u)
        self.assertEqual(r.report['architecture'],'bounded_agent_v2')
        self.assertTrue(r.report['plan']['steps'])
        self.assertIn('Планирование',[x['step_name'] for x in t.steps])
        self.assertIn('Самопроверка',[x['step_name'] for x in t.steps])
        self.assertEqual(r.report['adaptations'][0]['reason'],'EMPTY_BANK_SEARCH')

    async def test_partial_profile_supports_qualitative_analysis_without_inventing_finances(self):
        r,_,_=await self.run_draft()
        self.assertEqual(r.analysis.applicability,'conditional')
        self.assertNotIn('revenue_company',r.business_context.known_features)
        self.assertIsNone(r.insight.relevant_bank_product)
        self.assertEqual(r.calculations,[])

    async def test_fallback_source_failure_is_not_reported_as_empty_search(self):
        class EmptyThenFail:
            def __init__(self): self.calls=0
            async def search(self,*args,**kwargs):
                self.calls+=1
                if self.calls==1: return []
                raise RuntimeError('Sensitive source diagnostic must not escape')
        source=EmptyThenFail()
        result,client,_=await self.run_draft(source=source)
        observations=[item for item in result.report['tool_observations'] if item['tool']=='search_bank']
        self.assertEqual([item['status'] for item in observations],['empty','error'])
        self.assertIn('BANK_SOURCE_UNAVAILABLE_OR_INVALID',result.verification.warnings)
        self.assertEqual(len(client.prompts),1)
        self.assertNotIn('Sensitive source diagnostic',json.dumps(result.model_dump(mode='json')))

    async def test_filtered_future_result_can_trigger_one_alternative_search(self):
        old=AnalyticsDocument(article=self.event.model_copy(update={'id':99,'is_bank_analytics':True,
            'published_at':self.event.published_at-timedelta(days=1),'content':'Ожидается сохранение ключевой ставки до конца года.'}))
        future=old.model_copy(update={'article':old.article.model_copy(update={'id':100,
            'published_at':self.event.published_at+timedelta(days=1),'content':'Недоступный будущий прогноз.'})})
        class Source:
            calls=0
            async def search(inner,*args,**kwargs):
                inner.calls+=1
                return [future] if inner.calls==1 else [old]
        source=Source();draft=valid_draft();draft['impacts'][0]['evidence_ids'].append('bank:99:excerpt')
        result,client,_=await self.run_draft(draft,source=source)
        self.assertEqual(source.calls,2)
        self.assertIn(old.article.content,client.prompts[0])
        self.assertNotIn(future.article.content,client.prompts[0])
        self.assertIn('FUTURE_BANK_SOURCE_EXCLUDED',result.verification.warnings)
        self.assertEqual(len(client.prompts),1)

    async def test_sector_relevance_can_plan_search_without_macro_keyword(self):
        pharma=self.company.model_copy(update={'mainokved_code':'21.20','mainokved_name':'Производство лекарственных препаратов'})
        for company,title,text,channel in [
            (self.company,'Цены стального проката','Стоимость стального проката выросла.','inputs'),
            (pharma,'Обращение лекарственных препаратов','Изменились условия обращения лекарственных препаратов.','other'),
        ]:
            with self.subTest(title=title):
                event=self.event.model_copy(update={'title':title,'content':text})
                source=EmptySearchSource();draft=valid_draft()
                draft['impacts'][0].update(channel=channel,condition='Если деятельность компании связана с затронутым сегментом',
                    mechanism='Изменение условий в затронутом сегменте может влиять на планы и выбор вариантов работы.')
                result,_,_=await self.run_draft(draft,source=source,company=company,event=event)
                self.assertTrue(result.verification.passed)
                self.assertTrue(result.report['news_selection'][0]['requires_analysis'])
                self.assertIn('search_bank',[item['tool'] for item in result.report['tool_observations']])

    async def test_unrelated_news_does_not_trigger_available_bank_search(self):
        class ForbiddenSearch:
            async def search(inner,*args,**kwargs): raise AssertionError('UNRELATED_TOOL_CALL')
        event=self.event.model_copy(update={'title':'Шахматный турнир','content':'Завершился шахматный турнир.'})
        draft={'applicability':'insufficient','missing_data':['Связь события с деятельностью компании не установлена.']}
        result,client,_=await self.run_draft(draft,source=ForbiddenSearch(),event=event)
        self.assertEqual([item['tool'] for item in result.report['tool_observations']],['inspect_company'])
        self.assertEqual(len(client.prompts),1)

    async def test_irrelevant_event_stays_insufficient(self):
        event=self.event.model_copy(update={'title':'Итоги шахматного турнира','content':'Завершился городской шахматный турнир.'})
        draft={'applicability':'insufficient','missing_data':['Связь спортивного события с деятельностью компании не установлена.']}
        r,_,_=await self.run_draft(draft,event=event)
        self.assertFalse(r.analysis.impacts);self.assertFalse(r.analysis.actions)
        self.assertEqual(len(r.report['plan']['steps']),1)

    async def test_same_unrelated_event_cannot_be_forced_into_financing(self):
        event=self.event.model_copy(update={'content':'Завершился шахматный турнир.'})
        with self.assertRaises(AgentEvidenceError) as caught:
            await self.run_draft(event=event,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))
        self.assertIn('UNSUPPORTED_CAUSAL_CHANNEL',[x['code'] for x in caught.exception.run_report['verification']['issues']])

    async def test_obtained_credit_hidden_premises_and_numbers_are_rejected(self):
        attacks=[('Полученный компанией кредит может снизить стоимость финансирования.','UNSUPPORTED_FACTUAL_PREMISE'),
                 ('Компания получила кредит, что может поддержать инвестиции.','UNSUPPORTED_FACTUAL_PREMISE'),
                 ('Если компания планирует модернизацию, компания уже получила кредит.','UNSUPPORTED_FACTUAL_PREMISE'),
                 ('Стоимость финансирования может снизиться на 5%.','UNSUPPORTED_INFERENCE_NUMBER'),
                 ('Стоимость финансирования может снизиться на пять процентов.','UNSUPPORTED_INFERENCE_NUMBER'),
                 ('Стоимость финансирования может снизиться на полпроцента.','UNSUPPORTED_INFERENCE_NUMBER'),
                 ('Стоимость финансирования может снизиться на два процентных пункта.','UNSUPPORTED_INFERENCE_NUMBER'),
                 ('Стоимость финансирования может снизиться вдвое.','UNSUPPORTED_INFERENCE_NUMBER'),
                 ('Процентные расходы могут сократиться на миллион рублей.','UNSUPPORTED_INFERENCE_NUMBER'),
                 ('Платежи по кредиту могут снизиться вследствие сохранения ключевой ставки.','UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE'),
                 ('Банк рекомендует компании кредит, который может поддержать инвестиции.','UNSUPPORTED_BANK_ATTRIBUTION'),
                 ('Расходы гарантированно снизятся.','FALSE_CERTAINTY')]
        for text,code in attacks:
            with self.subTest(text=text):
                d=valid_draft();d['impacts'][0]['mechanism']=text
                with self.assertRaises(AgentEvidenceError) as caught:
                    await self.run_draft(d,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))
                self.assertIn(code,[x['code'] for x in caught.exception.run_report['verification']['issues']])

    async def test_B03_conditional_contract_type_and_limit_statement_are_accepted(self):
        d=valid_draft();d['impacts'][0]['condition']='Если у компании имеются действующие кредиты с плавающей ставкой'
        d['impacts'][0]['mechanism']+=' Это не доказывает изменение действующих платежей.'
        r,_,_=await self.run_draft(d)
        self.assertTrue(r.verification.passed)

    @staticmethod
    def inference_in_field(field,text):
        draft=valid_draft()
        if field=='mechanism':
            draft['impacts'][0]['mechanism']=text
            path='impacts[0].mechanism'
        elif field=='outcome':
            draft['scenarios']=[{'label':'Проверка условий финансирования',
                'condition':'Если компания рассматривает новое финансирование',
                'outcome':text,'evidence_ids':['news:event','company:mainokved_code']}]
            path='scenarios[0].outcome'
        else:
            draft['actions'][0]['rationale']=text
            path='actions[0].rationale'
        return draft,path

    async def test_alternative_company_subjects_cannot_smuggle_debt_or_distress_into_any_inference(self):
        statements=(
            'У предприятия есть кредитный долг, поэтому стоимость финансирования может влиять на бюджет.',
            'Компания обслуживает действующий кредит, поэтому стоимость финансирования может влиять на бюджет.',
            'Предприятие находится на грани банкротства из-за действующих кредитов, что может ограничить финансирование.',
        )
        for field in ('mechanism','outcome','rationale'):
            for text in statements:
                with self.subTest(field=field,text=text):
                    draft,path=self.inference_in_field(field,text)
                    with self.assertRaises(AgentEvidenceError) as caught:
                        await self.run_draft(draft,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))
                    issues=caught.exception.run_report['verification']['issues']
                    self.assertIn(('UNSUPPORTED_FACTUAL_PREMISE',path),[(i['code'],i['path']) for i in issues])

    async def test_financial_guards_apply_to_scenarios_and_action_rationales_as_well_as_impacts(self):
        cases=(
            ('Платежи по кредитам могут снизиться вследствие сохранения ключевой ставки.',
             'UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE'),
            ('Признак кредитного продукта подтверждает наличие долга, поэтому расходы могут зависеть от ставки.',
             'UNSUPPORTED_FACTUAL_PREMISE'),
            ('Стоимость обслуживания плавающей ставки может влиять на расходы.',
             'UNSUPPORTED_COMPANY_CONDITION'),
        )
        for field in ('mechanism','outcome','rationale'):
            for text,code in cases:
                with self.subTest(field=field,text=text):
                    draft,path=self.inference_in_field(field,text)
                    with self.assertRaises(AgentEvidenceError) as caught:
                        await self.run_draft(draft,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))
                    issues=caught.exception.run_report['verification']['issues']
                    self.assertIn((code,path),[(i['code'],i['path']) for i in issues])

    async def test_unrelated_limitation_does_not_mask_an_asserted_payment_change(self):
        text='Платежи по кредитам могут снизиться вследствие сохранения ключевой ставки. Условия договоров неизвестны.'
        for field in ('mechanism','outcome','rationale'):
            with self.subTest(field=field):
                draft,path=self.inference_in_field(field,text)
                with self.assertRaises(AgentEvidenceError) as caught:
                    await self.run_draft(draft,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))
                issues=caught.exception.run_report['verification']['issues']
                self.assertIn(('UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE',path),[(i['code'],i['path']) for i in issues])

    async def test_conditional_contract_assumptions_and_requests_for_data_remain_legal(self):
        draft=valid_draft()
        draft['impacts'][0]['condition']='Если у предприятия есть кредиты с плавающей ставкой'
        draft['impacts'][0]['mechanism']='Флаг продукта не доказывает получение кредита; стоимость финансирования может влиять на сравнение предложений.'
        draft['scenarios']=[{'label':'Предоставленный сценарий финансирования',
            'condition':'Если предприятие рассматривает новое финансирование',
            'outcome':'Предоставленные данные могут помочь сравнить условия финансирования.',
            'evidence_ids':['news:event','company:mainokved_code']}]
        draft['actions'][0]['title']='Проверить условия фиксированных и плавающих ставок'
        draft['actions'][0]['rationale']='Проверить остаток долга и условия договоров до подготовки решения.'
        result,_,_=await self.run_draft(draft)
        self.assertTrue(result.verification.passed)
        self.assertIn(draft['impacts'][0]['condition'],result.insight.consequences)
        self.assertIn(draft['scenarios'][0]['outcome'],result.insight.ai_view)
        self.assertIn(draft['actions'][0]['rationale'].rstrip('.'),result.insight.recommended_action)

    async def test_reference_to_supplied_material_is_not_a_received_loan_assertion(self):
        for text in ('Предоставленный сценарий может помочь сравнить предложения по финансированию.',
                     'Кредит может быть получен после проверки условий финансирования.'):
            d=valid_draft();d['impacts'][0]['mechanism']=text
            result,_,_=await self.run_draft(d)
            self.assertTrue(result.verification.passed)
        d=valid_draft();d['impacts'][0]['mechanism']='Предоставленный сценарий показывает полученный компанией кредит, который может поддержать инвестиции.'
        with self.assertRaises(AgentEvidenceError):
            await self.run_draft(d,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))

    async def test_false_citation_and_unknown_company_reference_cannot_enter_result(self):
        for key in ('news:foreign','company:foreign_loan'):
            d=valid_draft();d['impacts'][0]['evidence_ids'].append(key)
            with self.assertRaises(AgentEvidenceError) as caught:
                await self.run_draft(d,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))
            self.assertIn('UNKNOWN_EVIDENCE',[x['code'] for x in caught.exception.run_report['verification']['issues']])
        d=valid_draft();d['facts']=[{'quote':'Выдуманная банковская цитата'}]
        with self.assertRaises(AgentResponseError): await self.run_draft(d)

    async def test_unsafe_action_is_rejected_even_when_prefixed_with_safe_action(self):
        for title in ('Взять кредит для модернизации','Сравнить предложения и подписать кредитный договор','Проверить бюджет. Уволить сотрудников.','Проверить бюджет и сократить штат'):
            d=valid_draft();d['actions'][0]['title']=title
            with self.assertRaises(AgentEvidenceError):
                await self.run_draft(d,config=AgentConfig(max_llm_calls=1,max_recovery_attempts=0))

    async def test_one_targeted_correction_has_field_path_and_bounded_budget(self):
        bad=valid_draft();bad['impacts'][0]['mechanism']='Компания получила кредит, что может поддержать инвестиции.'
        client=FakeLLMClient(responses=[json.dumps(bad),json.dumps(valid_draft())])
        r=await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(len(client.prompts),2)
        self.assertIn('impacts[0].mechanism',client.prompts[1])
        self.assertIn('UNSUPPORTED_FACTUAL_PREMISE',client.prompts[1])
        self.assertEqual(r.report['validation_summary']['repairs'],1)
        self.assertEqual(r.report['adaptations'][-1]['action'],'targeted_correction')
        badclient=FakeLLMClient(responses=[json.dumps(bad)]*3)
        with self.assertRaises(AgentEvidenceError) as caught:
            await AgentInsightEngine(badclient).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(len(badclient.prompts),2)
        self.assertEqual(len(caught.exception.run_report['calls']),2)

    async def test_bank_search_adapts_and_accumulates_results(self):
        doc=AnalyticsDocument(article=self.event.model_copy(update={'id':9,'title':'Банковский прогноз ставки',
            'content':'Мы ожидаем сохранения ключевой ставки до конца года.','is_bank_analytics':True,
            'published_at':self.event.published_at-timedelta(days=10)}),source_document_id='local:test',source_page=1,source_hash='f'*64)
        class Search:
            queries=[]
            async def search(inner,query,*,company,event,limit):
                inner.queries.append(query);return [] if len(inner.queries)==1 else [doc]
        source=Search();d=valid_draft();d['impacts'][0]['evidence_ids'].append('bank:9:excerpt')
        r,c,_=await self.run_draft(d,source=source)
        self.assertEqual(len(source.queries),2);self.assertNotEqual(*source.queries)
        self.assertIn(doc.article.content,c.prompts[0])
        self.assertIn('Локальный непроверенный',r.insight.ai_view)
        self.assertEqual(r.insight.bank_view,'Позиция Альфа-Банка не предоставлена.')

    async def test_verified_bank_attribution_is_exact_and_forecast_dates_retained(self):
        from backend.app.agent import ManagementActionEngine
        docs=[];d=valid_draft()
        for n,days in [(9,10),(10,20)]:
            article=self.event.model_copy(update={'id':n,'title':'Обзор ставки','content':f'Прогноз ставки на конец 2027 года составляет {n}% годовых.',
                'published_at':self.event.published_at-timedelta(days=days),'is_bank_analytics':True})
            docs.append(AnalyticsDocument(article=article,source_document_id=f'doc:{n}',provenance_status='verified'))
            d['impacts'][0]['evidence_ids'].append(f'bank:{n}:excerpt')
        d['source_comparisons']=[{'evidence_ids':['bank:9:excerpt','bank:10:excerpt'],'relationship':'different_vintages',
            'explanation':'Публикации относятся к разным датам; сопоставимость целевых периодов и предпосылок требует проверки.'}]
        r,_,_=await self.run_draft(d,source=MockAnalyticsSource(docs))
        for doc in docs:
            self.assertIn(doc.article.content,r.insight.bank_view)
            self.assertIn(doc.article.published_at.date().isoformat(),r.insight.bank_view)
        self.assertNotIn('UNACKNOWLEDGED_CONTRADICTION',[x.code for x in r.verification.issues])
        answer=await ManagementActionEngine(None).answer_question('Сравни банковские прогнозы и их источники.',
            analysis=r,company=self.company,tracer=RecordingTracer())
        self.assertEqual(answer.status,'complete')
        self.assertTrue(all(ref.source_id.startswith('bank:') for ref in answer.source_refs))
        for doc in docs:
            self.assertIn(doc.article.published_at.date().isoformat(),answer.summary)
            ref=next(item for item in answer.source_refs if item.source_document_id==doc.source_document_id)
            self.assertEqual(ref.published_at,doc.article.published_at)
            self.assertEqual(ref.quote,doc.article.content)

    async def test_future_bank_source_is_not_visible_to_model(self):
        doc=AnalyticsDocument(article=self.event.model_copy(update={'id':9,'is_bank_analytics':True,
            'published_at':self.event.published_at+timedelta(days=1),'content':'Будущий банковский материал.'}))
        r,c,_=await self.run_draft(source=MockAnalyticsSource([doc]))
        self.assertNotIn(doc.article.content,c.prompts[0])
        self.assertIn('FUTURE_BANK_SOURCE_EXCLUDED',r.verification.warnings)

    async def test_payment_summary_is_used_without_full_rows_or_supplier_inference(self):
        peers=[CounterpartyItem(synthetic_u=f'PEER_{n}',direction='SUPPLIER',total_amount=n+1,transaction_count=1) for n in range(50)]
        peers.append(CounterpartyItem(synthetic_u=self.company.synthetic_u,direction='PAYER',total_amount=9999,transaction_count=1))
        d=valid_draft();d['impacts'][0]['evidence_ids'].append('payments:summary')
        r,c,_=await self.run_draft(d,peers=peers)
        self.assertIn('payments:summary',c.prompts[0])
        self.assertNotIn('PEER_0"',c.prompts[0])
        self.assertEqual(r.business_context.self_transfer_rows,1)
        self.assertTrue(any(x.kind=='payment' for x in r.evidence))
        self.assertIn('не поставки',c.prompts[0])

    async def test_json_duplicate_keys_nan_and_oversized_text_are_diagnosed(self):
        for reply in ('{"applicability":"conditional","applicability":"insufficient"}', '{"value":NaN}', 'x'*13000):
            client=FakeLLMClient(reply)
            with self.assertRaises(AgentResponseError):
                await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
            self.assertEqual(len(client.prompts),1)

    async def test_http_error_is_not_retried_or_exposed(self):
        client=FakeLLMClient(responses=[RuntimeError('SECRET_PROVIDER_BODY')])
        with self.assertRaises(AgentLLMError) as caught:
            await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertNotIn('SECRET_PROVIDER_BODY',str(caught.exception));self.assertEqual(len(client.prompts),1)

    async def test_timeout_and_cancellation_leave_usage_report(self):
        class Slow:
            async def generate(inner,prompt): await asyncio.sleep(10)
        with self.assertRaises(AgentLLMTimeoutError) as caught:
            await AgentInsightEngine(Slow(),config=AgentConfig(llm_timeout_seconds=.01)).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(caught.exception.run_report['calls'][0]['timeout_kind'],'llm_deadline')
        task=asyncio.create_task(AgentInsightEngine(Slow()).generate_analysis(self.company,self.event,[],RecordingTracer()))
        await asyncio.sleep(.02);task.cancel()
        with self.assertRaises(asyncio.CancelledError) as cancelled: await task
        self.assertEqual(cancelled.exception.run_report['calls'][0]['status'],'cancelled')

    async def test_concurrent_runs_do_not_share_company_context_or_usage(self):
        client=FakeLLMClient(json.dumps(valid_draft()));engine=AgentInsightEngine(client)
        other=self.company.model_copy(update={'id':3,'synthetic_u':'OTHER_U','region':'Другой регион'})
        a,b=await asyncio.gather(engine.generate_analysis(self.company,self.event,[],RecordingTracer()),
                                 engine.generate_analysis(other,self.event,[],RecordingTracer()))
        self.assertNotEqual(a.company_id,b.company_id)
        self.assertNotEqual(a.report['company_synthetic_u'],b.report['company_synthetic_u'])
        self.assertEqual(len(a.report['calls']),1);self.assertEqual(len(b.report['calls']),1)
        self.assertIsNone(a.report['totals']['total_tokens'])

    def test_candidate_ranking_changes_with_company_and_keeps_unrelated_news_low(self):
        steel=self.event.model_copy(update={'id':10,'title':'Рынок стали','content':'Изменились цены на сталь и металл.'})
        chess=self.event.model_copy(update={'id':11,'title':'Шахматный турнир','content':'Завершился шахматный турнир.'})
        ranked=AgentInsightEngine.rank_news(self.company,[chess,steel,self.event])
        self.assertEqual(ranked[-1]['event_id'],11)
        self.assertEqual(ranked[-1]['score'],0)
        self.assertTrue(any(x['reasons'] for x in ranked if x['event_id']==10))
        pharma=self.company.model_copy(update={'id':8,'synthetic_u':'PHARMA','mainokved_code':'21.20','mainokved_name':'Производство лекарственных препаратов'})
        drug=steel.model_copy(update={'id':12,'title':'Обращение лекарственных препаратов','content':'Изменились условия обращения лекарственных препаратов.'})
        self.assertEqual(AgentInsightEngine.rank_news(pharma,[steel,drug,chess])[0]['event_id'],12)
        self.assertEqual(AgentInsightEngine.rank_news(self.company,[steel,drug,chess])[0]['event_id'],10)
        with self.assertRaisesRegex(ValueError,'NEWS_ID_CONTENT_CONFLICT'):
            AgentInsightEngine.rank_news(self.company,[steel,steel.model_copy(update={'content':'Другой текст с тем же ID.'})])

    def test_prompt_schema_preserves_required_action_title_property(self):
        from backend.app.agent.prompts import build_prompt
        schema=json.loads(build_prompt({}).split('JSON Schema:\n')[1].split('\nВходные данные')[0])
        def check(value):
            if isinstance(value,dict):
                if 'properties' in value:
                    self.assertLessEqual(set(value.get('required',[])),set(value['properties']))
                for child in value.values():check(child)
            elif isinstance(value,list):
                for child in value:check(child)
        check(schema)
        self.assertIn('title',schema['$defs']['ProposedAction']['properties'])


if __name__=='__main__':unittest.main()
