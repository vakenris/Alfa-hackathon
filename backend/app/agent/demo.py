"""Offline product-shaped example. Explicit fixtures demonstrate plumbing, not LLM quality."""
import asyncio
from datetime import datetime, timezone
import json
from . import AgentInsightEngine,AnalyticsDocument,FakeLLMClient,MockAnalyticsSource,RecordingTracer,ManagementActionEngine
from backend.app.schemas.company import CompanyResponse,CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


async def demo():
    company=CompanyResponse(id=1,synthetic_u='DEMO_METAL',name='Учебное производство профилей',mainokved_code='24.33',
                            mainokved_name='Производство профилей холодной гибкой',cl_product_lea_mark=True)
    event=NewsArticleResponse(id=2,title='Синтетическое решение о ключевой ставке',source='Синтетический пример',
        published_at=datetime(2026,10,23,tzinfo=timezone.utc),content='Банк России решил сохранить ключевую ставку на уровне 14% годовых.',category='Тест')
    peers=[CounterpartyItem(synthetic_u='DEMO_PEER_A',name='Учебный участник А',direction='BENEFICIARY',total_amount=700000,transaction_count=7),
           CounterpartyItem(synthetic_u='DEMO_PEER_B',name='Учебный участник Б',direction='BENEFICIARY',total_amount=300000,transaction_count=3),
           CounterpartyItem(synthetic_u=company.synthetic_u,direction='PAYER',total_amount=2000000,transaction_count=2)]
    bank=AnalyticsDocument(article=event.model_copy(update={'id':3,'title':'Учебный банковский сценарий',
        'published_at':datetime(2026,9,11,tzinfo=timezone.utc),'content':'В учебном сценарии ожидается сохранение ключевой ставки до конца года.',
        'is_bank_analytics':True}),source_document_id='synthetic:bank:3',provenance_status='synthetic')
    reply={'applicability':'conditional','impacts':[
        {'id':'funding','channel':'financing','direction':'mixed','condition':'Если компания планирует новое финансирование и платежи по нему пересекаются с крупными расчётами',
         'mechanism':'Стоимость нового займа может влиять на выбор предложения, а концентрация исходящих расчётов в выборке может потребовать сверки графика платежей и запаса ликвидности.',
         'evidence_ids':['news:event','company:mainokved_name','company:cl_product_lea_mark','payments:summary','bank:3:excerpt'],
         'data_needs':['План новых заимствований и предложения кредиторов.','Платёжный календарь, валюта и полнота платёжных данных.']},
        {'id':'capex','channel':'investment','direction':'mixed','condition':'Если рассматривается модернизация с внешним финансированием',
         'mechanism':'Стоимость капитала может влиять на выбор этапности модернизации; сопоставление предложений с предоставленным сценарием может помочь отделить текущие условия от ожиданий.',
         'evidence_ids':['news:event','company:mainokved_name','bank:3:excerpt'],
         'data_needs':['Состав проектов, ожидаемые денежные потоки и варианты финансирования.']}],
        'scenarios':[{'label':'Условия финансирования сохраняются','condition':'Если новые предложения кредиторов не становятся доступнее',
                      'outcome':'Может возрасти значимость поэтапного исполнения инвестиционной программы и проверки запаса ликвидности.',
                      'evidence_ids':['news:event','company:mainokved_name','bank:3:excerpt']}],
        'actions':[{'title':'Сопоставить новые кредитные предложения с графиком крупных исходящих платежей',
                    'rationale':'Проверить возможные пики потребности в ликвидности до подготовки решения.',
                    'impact_ids':['funding'],'required_data':['Оферты кредиторов и платёжный календарь.'],'owner_role':'TREASURY'},
                   {'title':'Сравнить поэтапное финансирование модернизации с переносом отдельных расходов',
                    'rationale':'Подготовить решение по проектам с учётом стоимости капитала и доступности средств.',
                    'impact_ids':['capex'],'required_data':['Экономика проектов и источники финансирования.'],'owner_role':'CFO'}],
        'missing_data':['Наличие долга, условия действующих договоров и фактическая инвестиционная программа не установлены.']}
    trace=RecordingTracer();client=FakeLLMClient(json.dumps(reply,ensure_ascii=False))
    core=AgentInsightEngine(client,analytics_source=MockAnalyticsSource([bank]))
    result=await core.generate_analysis(company,event,peers,trace)
    qa=await ManagementActionEngine(core).answer_question('Какие источники и допущения связаны с финансированием?',analysis=result,company=company,tracer=trace)
    return {'mode':'OFFLINE_FIXTURE_NOT_REAL_LLM','real_provider_calls':0,'insight':result.insight.model_dump(mode='json'),
            'verification':result.verification.model_dump(mode='json'),'agent_report':result.report,'qa':qa.model_dump(mode='json'),'steps':trace.steps}


if __name__=='__main__':print(json.dumps(asyncio.run(demo()),ensure_ascii=False,indent=2))
