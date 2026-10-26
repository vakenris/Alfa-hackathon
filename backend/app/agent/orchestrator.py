"""Bounded Python agent: plan -> tools/context -> interpretation -> check/adapt."""
import asyncio
import json
from copy import deepcopy
from pydantic import ValidationError
from .config import AgentConfig
from .context import prepare_context, merge_bank_context
from .models import (AgentAnalysisResult,AgentConfigurationError,AgentContextError,AgentError,
                     AgentEvidenceError,AgentLLMError,AgentLLMTimeoutError,AgentResponseError,AgentTimeoutError,CheckIssue,DraftInsight)
from .prompts import build_prompt
from .runtime import RunTelemetry,ObservedLLMClient,ValidationAttempt
from .planning import build_plan,rank_news
from .verification import verify_draft,project_insight,selected_evidence


def decode_response(response,limit):
    if not isinstance(response,str) or len(response)>limit: raise AgentResponseError('RESPONSE_SIZE_OR_TYPE')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    def nonfinite(_): raise ValueError('Nonfinite JSON')
    try:
        return DraftInsight.model_validate(json.loads(response,object_pairs_hook=pairs,parse_constant=nonfinite))
    except json.JSONDecodeError:
        raise AgentResponseError('MALFORMED_ANALYSIS_JSON') from None
    except (ValueError,TypeError,RecursionError,ValidationError):
        raise AgentResponseError('INVALID_ANALYSIS_JSON') from None


class AgentInsightEngine:
    def __init__(self,llm_client=None,*,analytics_source=None,config=None):
        self.llm_client=llm_client
        self.analytics_source=analytics_source
        self.config=config or AgentConfig()
        self._llm_lock=asyncio.Lock()

    async def generate_insight(self,company,event,counterparties,tracer):
        return (await self.generate_analysis(company,event,counterparties,tracer)).insight

    @staticmethod
    def rank_news(company,events,counterparties=(),*,limit=20):
        return rank_news(company,events,counterparties,limit=limit)

    async def generate_analysis(self,company,event,counterparties,tracer,*,financial_inputs=None,loan_sensitivity=None):
        if self.llm_client is None: raise AgentConfigurationError('EXPLICIT_LLM_CLIENT_REQUIRED')
        telemetry=RunTelemetry(max_calls=self.config.max_llm_calls)
        plan={};observations=[];adaptations=[]
        async def emit(name,description):
            try:
                async with asyncio.timeout(self.config.trace_timeout_seconds):
                    await tracer.emit_step(name,description)
            except Exception: raise AgentError('TRACE_UNAVAILABLE') from None
        try:
            async with asyncio.timeout(self.config.run_timeout_seconds) as run_deadline:
                if len(counterparties)>self.config.max_input_counterparties:
                    raise AgentContextError('COUNTERPARTY_INPUT_LIMIT')
                company=company.model_copy(deep=True);event=event.model_copy(deep=True)
                counterparties=[item.model_copy(deep=True) for item in counterparties]
                financial_inputs=deepcopy(financial_inputs);loan_sensitivity=deepcopy(loan_sensitivity)
                source=self.analytics_source
                if getattr(source,'search_available',True) is False or self.config.max_bank_documents==0: source=None
                plan=build_plan(company,event,counterparties,source,
                    has_financial_inputs=financial_inputs is not None or loan_sensitivity is not None)
                await emit('Планирование','Выбраны шаги: '+', '.join(step['tool'] for step in plan['steps']))
                bank_step=next((step for step in plan['steps'] if step['tool']=='search_bank'),None)
                # Reserve space for the shared plan and observations; never raise external limits.
                context_config=self.config.model_copy(update={'max_context_chars':max(2000,self.config.max_context_chars-2000)})
                context=await prepare_context(company.model_copy(deep=True),event.model_copy(deep=True),
                    [item.model_copy(deep=True) for item in counterparties],source if bank_step else None,context_config,
                    bank_query=bank_step['query'] if bank_step else None)
                for step in plan['steps']:
                    if step['tool']=='calculate_sensitivity': continue
                    ids=[key for key,value in context.evidence.items() if value.kind=={'inspect_company':'company','summarize_payments':'payment','search_bank':'bank'}[step['tool']]]
                    source_failed=step['tool']=='search_bank' and any(w in context.warnings for w in
                        ('BANK_SOURCE_TIMEOUT','BANK_SOURCE_UNAVAILABLE_OR_INVALID','BANK_SOURCE_ID_CONFLICT','BANK_DOCUMENT_HASH_CONFLICT'))
                    observations.append({'tool':step['tool'],'status':'error' if source_failed else 'ok' if ids else 'empty','evidence_ids':ids,
                                         **({'query':step['query'],'warnings':context.warnings} if step['tool']=='search_bank' else {})})
                    await emit('Инструмент',step['tool']+': '+observations[-1]['status'])
                blocked_search_warnings={'BANK_SOURCE_TIMEOUT','BANK_SOURCE_UNAVAILABLE_OR_INVALID','BANK_SOURCE_ID_CONFLICT',
                                         'BANK_DOCUMENT_HASH_CONFLICT','CONTEXT_OPTIONAL_EVIDENCE_OMITTED'}
                if (bank_step and not any(item.kind=='bank' for item in context.evidence.values())
                        and not blocked_search_warnings.intersection(context.warnings)
                        and bank_step['query'].casefold().strip()!=bank_step['fallback_query'].casefold().strip()):
                    await emit('Адаптация','Поиск не дал материалов; проверяется альтернативный тематический запрос.')
                    previous_warnings=context.warnings
                    context=await prepare_context(company,event,counterparties,source,context_config,bank_query=bank_step['fallback_query'])
                    context.warnings=list(dict.fromkeys([*previous_warnings,*context.warnings]))
                    context.payload['warnings']=context.warnings
                    adaptations.append({'reason':'EMPTY_BANK_SEARCH','action':'alternative_query','query':bank_step['fallback_query']})
                    source_failed=any(w in context.warnings for w in
                        ('BANK_SOURCE_TIMEOUT','BANK_SOURCE_UNAVAILABLE_OR_INVALID','BANK_SOURCE_ID_CONFLICT','BANK_DOCUMENT_HASH_CONFLICT'))
                    observations.append({'tool':'search_bank','status':'error' if source_failed else 'ok' if any(item.kind=='bank' for item in context.evidence.values()) else 'empty',
                                         'query':bank_step['fallback_query'],'evidence_ids':[key for key,value in context.evidence.items() if value.kind=='bank'],
                                         'warnings':context.warnings})
                if context.warnings:
                    adaptations.append({'reason':'SOURCE_LIMITATIONS','action':'qualitative_with_explicit_gaps','warnings':context.warnings})
                from .financial import add_calculations
                calculations=add_calculations(context,financial_inputs=financial_inputs,loan_sensitivity=loan_sensitivity)
                if financial_inputs is not None or loan_sensitivity is not None:
                    observations.append({'tool':'calculate_sensitivity','status':'ok' if calculations else 'needs_data',
                                         'evidence_ids':[key for key,value in context.evidence.items() if value.kind=='calculation']})
                context.payload['agent_context']={'plan':plan,'observations':observations,'adaptations':adaptations}
                observed=ObservedLLMClient(self.llm_client,telemetry=telemetry)
                draft=None;issues=None;phase='draft'
                attempts=min(self.config.max_llm_calls,1+self.config.max_recovery_attempts)
                for attempt in range(attempts):
                    remaining_documents=self.config.max_bank_documents-sum(item.kind=='bank' for item in context.evidence.values())
                    searches=sum(item['tool']=='search_bank' for item in observations)
                    can_request=bool(source is not None and remaining_documents>0 and searches<2 and attempt+1<attempts
                                     and not blocked_search_warnings.intersection(context.warnings))
                    context.payload['agent_context'].update(can_request_bank_search=can_request,
                        remaining_searches=max(0,2-searches),remaining_bank_documents=max(0,remaining_documents),
                        remaining_llm_calls=attempts-attempt)
                    prompt=build_prompt(context.payload,draft=draft,issues=issues,
                                        max_context_chars=self.config.max_context_chars,
                                        max_prompt_chars=self.config.max_prompt_chars)
                    await emit('Анализ LLM','Интерпретация накопленного контекста.' if phase=='draft' else
                        'Повторная интерпретация с дополненным контекстом.' if phase=='reanalysis' else 'Одно адресное исправление по точным нарушениям.')
                    try:
                        async with asyncio.timeout(self.config.llm_timeout_seconds) as call_deadline:
                            async with self._llm_lock:
                                response=await observed.generate(prompt)
                    except TimeoutError:
                        if call_deadline.expired(): telemetry.record_deadline('llm_deadline')
                        raise AgentLLMTimeoutError('LLM_DEADLINE') from None
                    except Exception: raise AgentLLMError('LLM_CALL_FAILED') from None
                    try:
                        draft=decode_response(response,self.config.max_response_chars)
                    except AgentResponseError as error:
                        if str(error)!='MALFORMED_ANALYSIS_JSON' or attempt+1>=attempts:
                            raise
                        issues=[CheckIssue(code='MALFORMED_ANALYSIS_JSON',path='response')]
                        telemetry.validation_attempts.append(ValidationAttempt(call_number=attempt+1,phase=phase,
                            issue_codes=['MALFORMED_ANALYSIS_JSON'],facts=0,impacts=0))
                        adaptations.append({'reason':'INVALID_JSON_SYNTAX','action':'format_correction'})
                        draft=None;phase='repair'
                        await emit('Адаптация','Исправляется только формат JSON в оставшемся бюджете вызовов.')
                        continue
                    request=draft.context_request
                    used_queries={item.get('query','').casefold().strip() for item in observations if item['tool']=='search_bank'}
                    if request is not None and can_request and request.query.casefold().strip() not in used_queries:
                        telemetry.validation_attempts.append(ValidationAttempt(call_number=attempt+1,phase=phase,
                            issue_codes=['PENDING_CONTEXT_REQUEST'],facts=0,impacts=len(draft.impacts)))
                        await emit('Адаптация','Интерпретация выявила пробел; выполняется один дополнительный банковский поиск.')
                        plan['steps'].append({'tool':'search_bank','query':request.query,'reason':request.reason,'origin':'context_request'})
                        additional_config=context_config.model_copy(update={'max_bank_documents':remaining_documents})
                        additional=await prepare_context(company,event,counterparties,source,additional_config,bank_query=request.query)
                        previous_ids=set(context.evidence)
                        context=merge_bank_context(context,additional,context_config)
                        ids=[key for key,value in context.evidence.items() if value.kind=='bank' and key not in previous_ids]
                        failed=bool(blocked_search_warnings.intersection(additional.warnings))
                        observations.append({'tool':'search_bank','query':request.query,'status':'error' if failed else 'ok' if ids else 'empty',
                                             'evidence_ids':ids,'warnings':additional.warnings,'origin':'context_request'})
                        adaptations.append({'reason':'MODEL_CONTEXT_REQUEST','action':'search_then_reanalyse','query':request.query,
                                            'data_gap':request.reason,'evidence_ids':ids})
                        context.payload['agent_context']={'plan':plan,'observations':observations,'adaptations':adaptations}
                        draft=None;issues=None;phase='reanalysis'
                        continue
                    verification=verify_draft(draft,context)
                    telemetry.validation_attempts.append(ValidationAttempt(call_number=attempt+1,phase=phase,
                        issue_codes=[item.code for item in verification.issues],facts=verification.checked_facts,impacts=len(draft.impacts)))
                    await emit('Самопроверка',f'Оснований: {verification.checked_facts}; нарушений: {len(verification.issues)}.')
                    if verification.passed: break
                    issues=verification.issues
                    if attempt+1<attempts:
                        adaptations.append({'reason':'VERIFICATION_FAILED','action':'targeted_correction','issues':[i.model_dump(exclude_none=True) for i in issues]})
                        phase='repair'
                        await emit('Адаптация','Уточняется только ответ с нарушениями; новые факты не разрешаются.')
                if not verification.passed:
                    error=AgentEvidenceError('ANALYSIS_VERIFICATION_FAILED')
                    error.verification=verification.model_dump(mode='json')
                    raise error
                report=telemetry.build_report().model_dump(mode='json')
                report.update(architecture='bounded_agent_v2',company_synthetic_u=company.synthetic_u,event_id=event.id,
                              plan=plan,tool_observations=observations,adaptations=adaptations)
                report['validation_summary']={'passed':True,'impacts':len(draft.impacts),'facts':verification.checked_facts,
                                              'repairs':sum(item['action'] in ('targeted_correction','format_correction') for item in adaptations),
                                              'reanalyses':sum(item['action']=='search_then_reanalyse' for item in adaptations),'tool_calls':len(observations)}
                report['sources_summary']={'visible':len(context.sources),'verified_bank':sum(item.kind=='bank' and item.provenance_status=='verified' for item in context.evidence.values()),
                                           'bank_article_content_hashes':context.bank_content_hashes}
                used=selected_evidence(draft,context)
                report['personalization_summary']={kind:sum(item.kind==kind for item in used) for kind in ('company','payment','bank','calculation')}
                report['news_selection']=rank_news(company,[event],counterparties,limit=1)
                report['quality_evaluated_by_expert']=False
                return AgentAnalysisResult(company_id=company.id,event_id=event.id,
                    insight=project_insight(draft,context,verification,calculations),analysis=draft,business_context=context.business,
                    sources=context.sources,verification=verification,report=report,evidence=selected_evidence(draft,context),calculations=calculations)
        except TimeoutError:
            if not run_deadline.expired(): raise
            telemetry.record_deadline('run_deadline')
            error=AgentTimeoutError('RUN_DEADLINE')
            error.run_report=telemetry.build_report().model_dump(mode='json')
            error.run_report.update(architecture='bounded_agent_v2',company_synthetic_u=company.synthetic_u,event_id=event.id,
                                    plan=plan,tool_observations=observations,adaptations=adaptations)
            raise error from None
        except (AgentError,asyncio.CancelledError) as error:
            error.run_report=telemetry.build_report().model_dump(mode='json')
            error.run_report.update(architecture='bounded_agent_v2',company_synthetic_u=company.synthetic_u,event_id=event.id,
                                    plan=plan,tool_observations=observations,adaptations=adaptations)
            if hasattr(error,'verification'): error.run_report['verification']=error.verification
            raise
