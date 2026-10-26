"""Real bounded control flow, immutable context and budget gates; no provider IO."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx

from backend.app.agent import AgentConfig, AgentInsightEngine, FakeLLMClient, RecordingTracer, RuntimeAgentEngine
from backend.app.agent.context import merge_bank_context, prepare_context
from backend.app.agent.models import AgentContextError, AgentEvidenceError, AnalyticsDocument
from backend.app.agent.tools import MockAnalyticsSource
from backend.app.agent.tests.test_grounded import fixtures, valid_draft, EmptySearchSource
from backend.app.agent.tests.test_context_and_actions import loan
from backend.app.llm.client import ChatCompletionsClient, ChatCompletionsConfig
from backend.app.schemas.company import CounterpartyItem


def request():
    return {'applicability':'insufficient','missing_data':['Нужен содержательный обзор решения, а не календарь публикаций.'],
            'context_request':{'kind':'bank_search','query':'денежная политика прогноз ключевой ставки',
                               'reason':'Первый материал не содержит оценки решения.'}}


class BoundedAdaptationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.company,self.event=fixtures()
        guard=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        guard.start();self.addCleanup(guard.stop)

    def document(self,identifier,text):
        return AnalyticsDocument(article=self.event.model_copy(update={'id':identifier,'is_bank_analytics':True,
            'published_at':self.event.published_at-timedelta(days=1),'content':text}),
            source_document_id=f'synthetic:{identifier}',source_hash='f'*64)

    def client(self,replies):
        return FakeLLMClient(responses=[json.dumps(item,ensure_ascii=False) if isinstance(item,dict) else item for item in replies])

    async def test_interpretation_requests_context_and_keeps_prior_evidence_and_calculation(self):
        first=self.document(8,'Календарь публикаций банковских материалов.')
        second=self.document(9,'Ожидается сохранение ключевой ставки до конца года.')
        class Source:
            queries=[]
            async def search(inner,query,**kwargs):
                inner.queries.append(query);return [first] if len(inner.queries)==1 else [second]
        source=Source();answer=valid_draft();answer['impacts'][0]['evidence_ids'].append('bank:9:excerpt')
        client=self.client([request(),answer]);trace=RecordingTracer()
        result=await AgentInsightEngine(client,analytics_source=source).generate_analysis(
            self.company,self.event,[],trace,loan_sensitivity=loan())
        self.assertEqual(len(client.prompts),2);self.assertEqual(len(source.queries),2)
        for text in (first.article.content,second.article.content,'calculation:simple_interest_cost_delta'):
            self.assertIn(text,client.prompts[1])
        self.assertEqual(result.report['validation_summary']['reanalyses'],1)
        self.assertEqual(result.report['validation_summary']['repairs'],0)
        self.assertEqual(result.report['validation_attempts'][1]['phase'],'reanalysis')
        self.assertTrue(any(item['origin']=='context_request' for item in result.report['plan']['steps'] if 'origin' in item))
        self.assertEqual(result.calculations[0]['amount'],'2500.00')
        self.assertIsNone(result.analysis.context_request)

    async def test_failed_or_exhausted_search_is_not_retried_after_model_request(self):
        for fail,expected_calls in ((False,2),(True,1)):
            with self.subTest(source_failure=fail):
                class Source:
                    calls=0
                    async def search(inner,*args,**kwargs):
                        inner.calls+=1
                        if fail: raise RuntimeError('PRIVATE_SOURCE_ERROR')
                        return []
                source=Source();client=self.client([request(),valid_draft()])
                result=await AgentInsightEngine(client,analytics_source=source).generate_analysis(self.company,self.event,[],RecordingTracer())
                self.assertEqual(source.calls,expected_calls);self.assertEqual(len(client.prompts),2)
                self.assertEqual(result.report['validation_summary']['reanalyses'],0)
                self.assertNotIn('PRIVATE_SOURCE_ERROR',result.model_dump_json())

    async def test_pending_request_after_refinement_cannot_trigger_third_call_or_publish(self):
        class Source:
            calls=0
            async def search(inner,*args,**kwargs):
                inner.calls+=1
                return [self.document(inner.calls+7,'Календарь аналитических публикаций.')]
        source=Source();client=self.client([request(),request()])
        with self.assertRaises(AgentEvidenceError) as caught:
            await AgentInsightEngine(client,analytics_source=source).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(source.calls,2);self.assertEqual(len(client.prompts),2)
        self.assertIn('PENDING_CONTEXT_REQUEST',[item['code'] for item in caught.exception.run_report['verification']['issues']])

    async def test_changed_article_body_between_queries_is_rejected(self):
        class Source:
            calls=0
            async def search(inner,*args,**kwargs):
                inner.calls+=1
                return [self.document(8,'Первая версия материала.' if inner.calls==1 else 'Подменённая версия материала.')]
        client=self.client([request(),valid_draft()])
        with self.assertRaisesRegex(AgentContextError,'BANK_SOURCE_CHANGED_DURING_RUN'):
            await AgentInsightEngine(client,analytics_source=Source()).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(len(client.prompts),1)

    async def test_another_exact_passage_of_unchanged_article_preserves_both(self):
        text=('Оформление документа. '*60+'\n\nПрогноз ключевой ставки требует проверки.\n\n'+
              'Технические замечания. '*60+'\n\nЛогистика и перевозки требуют проверки маршрутов.')
        source=MockAnalyticsSource([self.document(8,text)]);config=AgentConfig(max_article_chars=250)
        first=await prepare_context(self.company,self.event,[],source,config,bank_query='ключевая ставка')
        second=await prepare_context(self.company,self.event,[],source,config,bank_query='логистика перевозки')
        merged=merge_bank_context(first,second,config)
        self.assertEqual(merged.evidence['bank:8:excerpt'],first.evidence['bank:8:excerpt'])
        quotes=[item.quote for item in merged.evidence.values() if item.kind=='bank']
        self.assertEqual(len(quotes),2)
        self.assertTrue(all(quote in text for quote in quotes))
        self.assertEqual(len(merged.bank_content_hashes),1)

    async def test_json_syntax_has_one_format_correction_inside_existing_budget(self):
        client=self.client(['{not-json',valid_draft()])
        result=await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertEqual(len(client.prompts),2);self.assertTrue(result.verification.passed)
        self.assertEqual(result.report['validation_summary']['repairs'],1)
        self.assertEqual(result.report['adaptations'][0]['action'],'format_correction')

    async def test_call_uses_snapshot_when_caller_mutates_original_dtos(self):
        company=self.company;event=self.event
        class MutatingClient:
            async def generate(inner,prompt):
                company.id=777;company.synthetic_u='CHANGED';event.id=888
                return json.dumps(valid_draft(),ensure_ascii=False)
        result=await AgentInsightEngine(MutatingClient()).generate_analysis(company,event,[],RecordingTracer())
        self.assertEqual(result.company_id,1);self.assertEqual(result.event_id,2)
        self.assertEqual(result.report['company_synthetic_u'],'OFFLINE_U')

    async def test_production_runtime_rejects_unbudgeted_clients_before_any_request(self):
        requests=[]
        def transport(request):
            requests.append(request);raise AssertionError('BUDGET_GATE_BYPASSED')
        plain=ChatCompletionsClient(ChatCompletionsConfig(api_url='https://offline.invalid/chat',model='offline-approved'),
                                    transport=httpx.MockTransport(transport))
        class Custom:
            async def generate(inner,prompt): raise AssertionError('CUSTOM_CLIENT_CALLED')
        for client in (plain,Custom()):
            runtime=RuntimeAgentEngine(client_factory=lambda:client,source_factory=EmptySearchSource,
                                       approved_model_ids={'offline-approved'})
            with self.assertRaisesRegex(ValueError,'shared BudgetedChatCompletionsClient'):
                await runtime.generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertFalse(requests)


if __name__=='__main__':unittest.main()
