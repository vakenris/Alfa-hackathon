"""One check over selected evidence and explicitly hypothetical prose; deterministic UI projection."""
import re
import json
from datetime import timezone
from .models import CheckIssue, VerificationReport, BANK_VIEW_UNAVAILABLE, DISCLAIMER
from backend.app.ml.interface import MLInsightResult
from backend.app.schemas.insight import EvidenceItem
from .planning import supports_causal_channel

CONDITION = re.compile(r'^(?:если\b|при условии\b|в случае\b)',re.I)
CONDITION_CONTINUATION = re.compile(r'^\s*(?:(?:а|и|или)\s+|если\b|при условии\b|в случае\b)',re.I)
MODAL = re.compile(r'может|могут|возмож|зависит|зависеть',re.I)
COMPLETED = re.compile(
    r'\b(?:полученн\w*|выданн\w*|предоставленн\w*|привлеч[её]нн\w*|заключ[её]нн\w*|'
    r'подписанн\w*|погашенн\w*|реализованн\w*|запущенн\w*|'
    r'получил[аио]?|выдал[аио]?|предоставил[аио]?|заключил[аио]?|подписал[аио]?|'
    r'погасил[аио]?|реализовал[аио]?|запустил[аио]?|привл[её]к(?:ла|ли|ло)?|'
    r'получен[аоы]?|выдан[аоы]?|заключ[её]н[аоы]?|погашен[аоы]?)\b',re.I)
COMPANY_SUBJECT = r'(?:компани[яи]|предприяти[ея]|завод[а]?|клиент[а]?)'
ASSERTION = re.compile(
    rf'\b(?:у\s+{COMPANY_SUBJECT}\s+(?:есть|имеется|имеются)|'
    rf'{COMPANY_SUBJECT}\s+(?:(?:уже|сейчас|фактически)\s+)?'
    r'(?:имеет|получает|владеет|обслуживает|погашает|выплачивает|'
    r'находится\s+на\s+грани\s+банкротства))\b',re.I)
PRODUCT_AS_LOAN = re.compile(
    r'(?:флаг|признак|продукт)[^.!?;]{0,70}?'
    r'(?P<verb>подтвержда\w*|доказыва\w*|означа\w*)'
    r'[^.!?;]{0,50}(?:получени\w*\s+кредит|наличи\w*\s+(?:долг|кредит)|действующ\w*\s+кредит)',re.I)
CONFIDENT = re.compile(r'гарантир|неизбеж|несомнен|безусловно|однозначно|точно\s+(?:выраст|сниз|будет)',re.I)
BANK_POSITION = re.compile(r'(?:банк|альфа)[^.!?]{0,45}(?:счита|рекоменду|прогнозиру|утвержда)|по (?:мнению|оценке) банка|позиция банка',re.I)
EXECUTE = re.compile(r'\b(?:взять|выдать|погасить|купить|продать|подписать|закрыть|уволить|перечислить|остановить)\b|сократ\w*\s+(?:штат|персонал)',re.I)
PREPARE = re.compile(r'^(?:запросить|проверить|сравнить|сверить|сверять|сопоставить|оценить|подготовить|уточнить|проанализировать|изучить|обсудить|рассмотреть|собрать)\b',re.I)
ANALYTIC_NOUN = re.compile(r'^(?:сверка|проверка|сравнение|сопоставление|оценка|подготовка|уточнение|анализ|изучение|обсуждение|рассмотрение|сбор|запрос)\b',re.I)
# A safe analytical heading cannot introduce a second executive action. Match
# nominative action names, not objects such as "условия получения и погашения".
EXECUTIVE_NOUN = re.compile(
    r'(?:[,;.!?\n]|\b(?:и|затем|далее)\b)\s*'
    r'(?:получение|выдача|погашение|покупка|продажа|подписание|закрытие|увольнение|'
    r'перечисление|остановка|сокращение\s+(?:штата|персонала))\b',re.I)
WRITTEN_QUANTITY = re.compile(
    r'\b(?:нол[ьяю]|нуль|нулев\w*|один|одна|одну|одно[йгму]\w*|два|две|двух|три|тр[её]х|'
    r'четыр[её]\w*|пят[ьи]\w*|шест[ьи]\w*|сем[ьи]\w*|восем\w*|девят\w*|десят\w*|'
    r'двадцат\w*|тридцат\w*|сорок\w*|пятьдесят|сто|сотн\w*|несколько|пару)'
    r'\s+(?:[\w-]+\s+){0,2}(?:процент\w*|пункт\w*|рубл\w*|доллар\w*|юан\w*|евро|'
    r'тысяч\w*|миллион\w*|миллиард\w*|лет|год\w*|месяц\w*|дней|дня)\b|'
    r'\bполпроцент\w*|\b(?:одно|двух|тр[её]х|пяти|десяти)процент\w*',re.I)
EXPECTATION_COMPARISON = re.compile(
    r'(?:выше|ниже|дороже|дешевле|хуже|лучше)[^.!?;]{0,45}ожидани\w*', re.I)
HOLD_INCREASE = re.compile(
    r'сохранени\w*\s+(?:ключев\w*\s+)?ставк\w*[^.!?;]{0,65}'
    r'(?P<effect>повыс\w*|повыш\w*|увелич\w*|удорож\w*|ухудш\w*)[^.!?;]{0,45}'
    r'(?:расход\w*|стоимост\w*|услови\w*\s+(?:кредит|финансир)\w*)', re.I)
PAYMENT_CONCLUSION = re.compile(
    r'(?:концентраци\w*|плат[её]ж\w*|выборк\w*)[^.!?;]{0,80}'
    r'(?P<verb>доказыва\w*|подтвержда\w*)[^.!?;]{0,65}'
    r'(?:дефицит\w*|кассов\w*\s+разрыв\w*|неплат[её]ж\w*|зависимост\w*)', re.I)


def unsupported_hold_baseline(text, event, assumption=''):
    """Catch specific invented comparisons; not a semantic economic verifier."""
    if not (re.search(r'ставк', event, re.I) and re.search(r'сохран|без изменен|не измен', event, re.I)):
        return False
    # An expressly hypothetical planning expectation is a comparison base, not
    # an assertion that the company or the market actually expected a decline.
    hypothetical_expectation = bool(CONDITION.search(assumption) and re.search(
        r'ожидани\w*|залож\w*[^.;!?]{0,60}(?:снижен|повышен)', assumption, re.I))
    increase=any(not re.search(r'\bне\s+(?:означает|доказывает|влеч[её]т|вед[её]т\s+к)\s*'
                              r'(?:(?:автоматическ\w*|немедленн\w*)\s+)*$|\bне\s+(?:может\s+)?$',
                              text[match.start():match.start('effect')],re.I)
                 for match in HOLD_INCREASE.finditer(text))
    return bool(increase or (
        EXPECTATION_COMPARISON.search(text) and not hypothetical_expectation))
def completed_assertion(text):
    for match in COMPLETED.finditer(text):
        if re.search(r'(?:может|могут)\s+быть\s*$',text[:match.start()],re.I):
            continue
        if re.match(r'(?:полученн|предоставленн)',match.group(),re.I) and re.match(
            r'\s+(?:(?:банковск|локальн|исходн|учебн|синтетическ)\w*\s+)?(?:сценари|источник|материал|анализ|данн|прогноз)\w*\b',text[match.end():],re.I):
            continue
        return True
    return False


def verify_draft(draft, context):
    issues=[]
    def fail(code,path,text='',claim=None):
        issues.append(CheckIssue(code=code,claim_id=claim,path=path,excerpt=text[:160] or None))
    def text_guard(text,path,*,premise=False,condition=False,financial=False,analytical_title=False,assumption=''):
        if WRITTEN_QUANTITY.search(text) or re.search(r'\d|вдвое|втрое|удво|утро|на\s+(?:половину|треть|четверть)|\b(?:тысяч\w*|миллион\w*|миллиард\w*)\b',text,re.I):
            fail('UNSUPPORTED_INFERENCE_NUMBER',path,text)
        if CONFIDENT.search(text): fail('FALSE_CERTAINTY',path,text)
        if BANK_POSITION.search(text): fail('UNSUPPORTED_BANK_ATTRIBUTION',path,text)
        if premise and (completed_assertion(text) or ASSERTION.search(text)):
            fail('UNSUPPORTED_FACTUAL_PREMISE',path,text)
        if financial:
            # The same financial guard applies to every published inference,
            # including scenarios and action rationales. It is a bounded check,
            # not proof that arbitrary prose follows semantically from a source.
            markers=list(re.finditer(r'плавающ|фиксированн|остаток долга',text,re.I))
            # "есть ли ... с плавающей ставкой" requests an unknown condition;
            # its scope ends at a comma or sentence boundary, never the whole paragraph.
            unknown_type=any(not re.search(r'\b(?:есть|имеется|имеются)\s+ли\b[^,;.!?]*$',
                                          text[:match.start()],re.I) for match in markers)
            if unknown_type and not (PREPARE.search(text) or analytical_title):
                fail('UNSUPPORTED_COMPANY_CONDITION',path,text)
            for match in PRODUCT_AS_LOAN.finditer(text):
                if not re.search(r'\bне\s*$',text[:match.start('verb')],re.I):
                    fail('UNSUPPORTED_FACTUAL_PREMISE',path,match.group())
            for match in PAYMENT_CONCLUSION.finditer(text):
                if not re.search(r'\bне\s*$',text[:match.start('verb')],re.I):
                    fail('UNSUPPORTED_PAYMENT_CONCLUSION',path,match.group())
            if unsupported_hold_baseline(text,event,assumption):
                fail('UNSUPPORTED_COMPARISON_BASELINE',path,text)
            if re.search(r'сохран|без изменен|не измен',event,re.I):
                for clause in re.split(r'[.!?;]',text):
                    if (re.search(r'(?:плат[её]ж|процентн\w*\s+расход)[^.!?]{0,60}(?:выраст|увелич|сниз|сократ)',clause,re.I)
                            and not re.search(r'не (?:означает|доказывает)|неизвест',clause,re.I)):
                        fail('UNCHANGED_RATE_IMPLIES_PAYMENT_CHANGE',path,clause)
        if condition and not CONDITION.search(text): fail('CONDITION_REQUIRED',path,text)
        # An explicit conditional premise must not smuggle in a completed event
        # after its boundary. Do not grant a whole paragraph immunity for 'if'.
        if condition:
            for boundary in re.finditer(r'([,;.!?])([^,;.!?]*)',text):
                clause=boundary.group(2)
                coordinated=boundary.group(1)==',' and CONDITION_CONTINUATION.search(clause)
                if completed_assertion(clause) or (ASSERTION.search(clause) and not coordinated):
                    fail('UNSUPPORTED_FACTUAL_PREMISE',path,clause)
    def refs(ids,path):
        for key in ids:
            if key not in context.evidence: fail('UNKNOWN_EVIDENCE',path,key)
        if 'news:event' not in ids: fail('MISSING_EVENT_ANCHOR',path)
        if not any(context.evidence[key].kind in ('company','payment','calculation') for key in ids if key in context.evidence):
            fail('MISSING_PERSONALIZATION',path)
    if draft.applicability=='insufficient':
        if draft.impacts or draft.scenarios or draft.actions: fail('INSUFFICIENT_HAS_CONCLUSIONS','applicability')
        if not draft.missing_data: fail('MISSING_CLARIFICATION','missing_data')
    elif not draft.impacts or not draft.actions:
        fail('INCOMPLETE_INTERPRETATION','applicability')
    if draft.context_request is not None:
        fail('PENDING_CONTEXT_REQUEST','context_request')
    if len({item.id for item in draft.impacts})!=len(draft.impacts): fail('DUPLICATE_IMPACT','impacts')
    event=context.evidence['news:event'].quote
    for n,item in enumerate(draft.impacts):
        root=f'impacts[{n}]'
        refs(item.evidence_ids,root+'.evidence_ids')
        text_guard(item.condition,root+'.condition',condition=True)
        text_guard(item.mechanism,root+'.mechanism',premise=True,financial=True,assumption=item.condition)
        if not MODAL.search(item.mechanism): fail('CAUSAL_LINK_NOT_CONDITIONAL',root+'.mechanism',item.mechanism)
        if not supports_causal_channel(item.channel,event,item.condition,item.mechanism):
            fail('UNSUPPORTED_CAUSAL_CHANNEL',root+'.channel',item.channel)
        if not item.data_needs: fail('MISSING_DATA_NEEDS',root+'.data_needs')
        for j,text in enumerate(item.data_needs): text_guard(text,f'{root}.data_needs[{j}]',premise=True)
    for n,item in enumerate(draft.scenarios):
        root=f'scenarios[{n}]';refs(item.evidence_ids,root+'.evidence_ids')
        text_guard(item.label,root+'.label',premise=True)
        text_guard(item.condition,root+'.condition',condition=True)
        text_guard(item.outcome,root+'.outcome',premise=True,financial=True,assumption=item.condition)
        if not MODAL.search(item.outcome): fail('CAUSAL_LINK_NOT_CONDITIONAL',root+'.outcome',item.outcome)
    known={item.id for item in draft.impacts}
    for n,item in enumerate(draft.actions):
        root=f'actions[{n}]'
        if not set(item.impact_ids)<=known: fail('UNKNOWN_IMPACT_REFERENCE',root+'.impact_ids')
        analytical_title=bool(PREPARE.search(item.title) or ANALYTIC_NOUN.search(item.title))
        if not analytical_title or EXECUTE.search(item.title) or EXECUTIVE_NOUN.search(item.title):
            fail('UNSUPPORTED_ACTION',root+'.title',item.title)
        for field,text in [('title',item.title),('rationale',item.rationale)]:
            text_guard(text,root+'.'+field,premise=True,financial=True,
                       analytical_title=field=='title' and analytical_title)
        for j,text in enumerate(item.required_data): text_guard(text,f'{root}.required_data[{j}]',premise=True)
    compared=set(); groups=set()
    for n,item in enumerate(draft.source_comparisons):
        root=f'source_comparisons[{n}]'
        keys=item.evidence_ids
        group=frozenset(keys)
        if len(group)!=len(keys): fail('DUPLICATE_COMPARISON_EVIDENCE',root+'.evidence_ids')
        if group in groups: fail('DUPLICATE_SOURCE_COMPARISON',root+'.evidence_ids')
        groups.add(group)
        bank=[]
        for key in keys:
            entry=context.evidence.get(key)
            if entry is None: fail('UNKNOWN_EVIDENCE',root+'.evidence_ids',key)
            elif entry.kind!='bank': fail('NON_BANK_COMPARISON_EVIDENCE',root+'.evidence_ids',key)
            else:
                bank.append(entry)
                compared.add(key)
        if item.relationship=='different_vintages':
            dates=[entry.published_at for entry in bank]
            instants={value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
                      for value in dates if value is not None}
            if len(bank)!=len(keys) or any(value is None for value in dates) or len(instants)<2:
                fail('INVALID_COMPARISON_VINTAGES',root+'.relationship',item.relationship)
        text_guard(item.explanation,root+'.explanation',premise=True,financial=True)
    for n,text in enumerate(draft.missing_data): text_guard(text,f'missing_data[{n}]',premise=True)
    selected={'news:event'}|{key for item in [*draft.impacts,*draft.scenarios,*draft.source_comparisons] for key in item.evidence_ids}
    used_bank={key for key in selected if key in context.evidence and context.evidence[key].kind=='bank'}
    if len(used_bank)>=2 and not used_bank<=compared:
        fail('MISSING_SOURCE_COMPARISON','source_comparisons',', '.join(sorted(used_bank-compared)))
    return VerificationReport(passed=not issues,issues=issues,
        warnings=list(dict.fromkeys([*context.warnings,'SEMANTIC_EXPERT_REVIEW_REQUIRED',
                                    *(['UNRESOLVED_SOURCE_COMPARISON'] if draft.source_comparisons else [])])),
        checked_facts=len(selected & context.evidence.keys()),checked_impacts=len(draft.impacts))


def selected_evidence(draft,context,*,projection_version=2):
    ids=['news:event',*(key for item in [*draft.impacts,*draft.scenarios,*draft.source_comparisons] for key in item.evidence_ids),
         *(key for key,value in context.evidence.items() if value.kind=='calculation')]
    if projection_version>=2 and 'payments:summary' in context.evidence and any(
            context.evidence[key].kind=='payment' for key in ids if key in context.evidence):
        # A displayed peer share retains the denominator and scope as evidence.
        ids.append('payments:summary')
    return [context.evidence[key] for key in dict.fromkeys(ids) if key in context.evidence]


def project_insight(draft,context,verification,calculations=(),*,projection_version=2):
    if type(projection_version) is not int or projection_version not in (1,2):
        raise ValueError('UNSUPPORTED_PROJECTION_VERSION')
    evidence=selected_evidence(draft,context,projection_version=projection_version)
    peer_directions={json.loads(item.quote)['direction'] for item in evidence if item.kind=='payment'
                     and 'share_of_passed_direction_amount' in json.loads(item.quote)}
    labels={'mainokved_code':'ОКВЭД','mainokved_name':'Отрасль','region':'Регион','city':'Город',
            'org_type':'Форма организации из профиля','okfs_name':'Форма собственности из профиля',
            'company_age_years':'Возрастная категория из профиля','cl_type':'Класс клиента из профиля',
            'amount_workers_fns':'Диапазон работников ФНС','amount_workers_spark':'Диапазон работников СПАРК',
            'cl_product_loa_mark':'Признак кредитного продукта','cl_product_lea_mark':'Признак лизинга',
            'revenue_company':'Выручка из профиля (год и валюта не заданы)'}
    company=[];anchors={}
    for item in evidence:
        facts=[]
        if item.kind=='company':
            data=json.loads(item.quote)
            facts.extend(labels.get(key,key)+': '+('отмечен в профиле' if value is True else str(value)) for key,value in data.items())
        elif item.kind=='calculation' and projection_version>=2:
            facts.append(json.loads(item.quote)['description'])
        elif item.kind=='payment':
            data=json.loads(item.quote)
            if 'directions' in data:
                enriched=projection_version>=2 and any('largest_group' in totals for totals in data['directions'].values())
                if enriched:
                    facts.append('Переданная платёжная выборка: '+str(data['input_rows'])+
                                 ' строк; период, валюта и полнота неизвестны.')
                    for key,label in (('incoming','входящих'),('outgoing','исходящих')):
                        if peer_directions and key not in peer_directions: continue
                        totals=data['directions'][key];group=totals.get('largest_group')
                        if group and group['counterparties'] and group['share_of_passed_direction_amount'] is not None:
                            facts.append('Крупнейшие участники ('+', '.join(group['synthetic_us'])+
                                '): '+str(group['counterparties'])+' из '+str(totals['unique_counterparties'])+
                                '; сумма '+str(group['total_amount'])+' / '+str(totals['total_amount'])+
                                f" = {group['share_of_passed_direction_amount']:.1%} переданных внешних {label} платежей.")
                else:
                    facts.append('Переданная платёжная выборка: входящие '+str(data['directions']['incoming']['total_amount'])+
                        '; исходящие '+str(data['directions']['outgoing']['total_amount'])+'; период, валюта и полнота неизвестны.')
            elif 'share_of_passed_direction_amount' in data:
                share=data['share_of_passed_direction_amount']
                direction={'incoming':'входящие платежи','outgoing':'исходящие платежи','unknown':'неустановленное направление'}.get(data['direction'],data['direction'])
                facts.append('Участник платёжной выборки '+str(data.get('name') or data['synthetic_u'])+
                    ': '+direction+'; доля в переданной выборке направления '+(f'{share:.1%}' if share is not None else 'не определена')+'.')
        company.extend(facts)
        if facts: anchors[item.id]=' '.join(facts)
    if projection_version>=2 and draft.impacts:
        links=[]
        for item in draft.impacts:
            keys=[key for key in item.evidence_ids if key in anchors]
            if 'payments:summary' in keys:
                keys=[key for key in keys if context.evidence[key].kind!='payment' or key=='payments:summary']
            links.append('Основания: '+'; '.join(anchors[key]+f' [{context.evidence[key].source_title}]' for key in keys)+
                         '\n'+item.condition.rstrip('.,')+': '+item.mechanism)
        why='\n\n'.join(links)
        # A peer share's denominator is not necessarily named in the draft.
        if 'payments:summary' in anchors and not any('payments:summary' in item.evidence_ids for item in draft.impacts):
            why+='\n'+anchors['payments:summary']+' [Переданная платёжная выборка]'
    else:
        why='; '.join(company)
        if draft.impacts: why+='\n'+draft.impacts[0].condition+': '+draft.impacts[0].mechanism
        else: why+='\nСвязь с деятельностью компании по доступным данным не установлена.'
    channels={'financing':'финансирование','investment':'инвестиции','fx':'валютные расчёты','logistics':'логистика','inputs':'затраты','regulation':'регулирование','demand':'спрос','other':'прочие факторы'}
    directions={'risk':'риск','opportunity':'возможность','mixed':'возможны разные последствия'}
    impacts=[f'Условная гипотеза AI ({channels[item.channel]}, {directions[item.direction]}): {item.condition.rstrip(".")} — {item.mechanism}\nДля проверки: '+ '; '.join(item.data_needs) for item in draft.impacts]
    scenarios=[f'{item.label}: {item.condition}. {item.outcome}' for item in draft.scenarios]
    bank=[item for item in evidence if item.kind=='bank']
    official=[item for item in bank if item.provenance_status=='verified' and item.source_document_id]
    reported=[item for item in bank if item not in official]
    bank_view='\n'.join(f'{item.source_title} ({item.published_at.date().isoformat()})'+
        (' [частичная выдержка]' if item.text_truncated else '')+f': «{item.quote}»' for item in official) or BANK_VIEW_UNAVAILABLE
    ai=['Условные механизмы AI требуют проверки; это не позиция банка.']
    if scenarios: ai+=['Условные сценарии:\n'+'\n'.join(scenarios)]
    relationships={'potential_conflict':'Возможное расхождение', 'different_vintages':'Разные даты публикации',
                   'scope_requires_check':'Условия и целевые периоды требуют сверки'}
    if draft.source_comparisons:
        comparisons=[]
        for item in draft.source_comparisons:
            labels=[]
            for key in item.evidence_ids:
                entry=context.evidence[key]
                stamp=entry.published_at.isoformat() if entry.published_at is not None else 'дата неизвестна'
                labels.append(f'{entry.source_title} [{key}; {stamp}]')
            comparisons.append(relationships[item.relationship]+': '+'; '.join(labels)+'. '+item.explanation)
        ai+=['Неразрешённое сопоставление источников AI:\n'+'\n'.join(comparisons),
             'Семантика сопоставления экспертно не проверена; проверка ID и дат не доказывает согласованность или противоречие.']
    ai += [('Локальный непроверенный банковский материал'+
        (f' «{item.source_title}»' if projection_version>=2 else '')+f' ({item.published_at.date().isoformat()})')+
        (' [частичная выдержка]' if item.text_truncated else '')+f': «{item.quote}»' for item in reported]
    if draft.missing_data: ai+=['Неизвестно / требуется уточнить: '+'; '.join(draft.missing_data)]
    ai+=context.business.limitations
    calculation_text=[item['description'] for item in calculations]
    actions='\n'.join(f'{item.owner_role}: {item.title.rstrip(".")}. Зачем: {item.rationale.rstrip(".")}. Проверить: '+ '; '.join(item.required_data) for item in draft.actions)
    return MLInsightResult(
        what_happened='Источник сообщает: «'+context.evidence['news:event'].quote+'»',
        why_important=why.strip(), consequences='\n\n'.join([*impacts,*calculation_text]) or 'Обоснованных последствий по имеющимся данным не установлено.',
        bank_view=bank_view,ai_view='\n'.join(ai),
        recommended_action=(actions or 'Уточнить: '+'; '.join(draft.missing_data))+'\n'+DISCLAIMER,
        relevant_bank_product=None,
        evidences=[EvidenceItem(claim=item.quote,source_title=item.source_title,source_url=item.source_url,
                               company_feature=item.company_feature if item.company_feature and len(item.company_feature)<=255 else None) for item in evidence])
