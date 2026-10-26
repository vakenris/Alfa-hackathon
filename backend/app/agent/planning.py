"""Transparent Python planning and candidate ranking, without another LLM or RAG."""
import re
from .backend_source import _features

TOPICS = {
    'financing':r'ключев\w*\s+ставк|процентн\w*\s+ставк|кредит|ипотек|финансир|инфляц',
    'investment':r'инвест|модернизац|строител|оборудован|сырь|металл|сталь',
    'fx':r'валют|доллар|юан|евро|обменн\w*\s+курс',
    'logistics':r'логист|перевоз|маршрут|фрахт|порт|постав',
    'inputs':r'сырь|энерг|металл|сталь|нефт|газ|себестоим|комплект',
    'regulation':r'закон|налог|пошлин|санкци|регулир|ограничен|требован|отч[её]тност',
    'demand':r'спрос|продаж|потреблен|заказ|доход',
}
SEARCH = {'financing':'ключевая ставка','investment':'инвестиции',
          'fx':'валюта курс','logistics':'логистика перевозки','inputs':'сырье себестоимость',
          'regulation':'регулирование налоги','demand':'спрос потребление'}
SOURCE_CHANNELS = {'monetary':'financing','working_capital':'financing','currency':'fx',
                   'logistics':'logistics','metals':'inputs','energy':'inputs','labor':'inputs','taxation':'regulation'}


def _direct_channels(text):
    """Actual topic signals, without planning defaults such as financing -> CAPEX."""
    _,source_topics=_features(text)
    channels=[key for key,pattern in TOPICS.items() if re.search(pattern,text,re.I)]
    return list(dict.fromkeys([*channels,*(SOURCE_CHANNELS[key] for key in sorted(source_topics) if key in SOURCE_CHANNELS)]))


def channels_for_text(text):
    """Planning priorities, not an exhaustive list of possible economic effects."""
    channels=_direct_channels(text)
    if 'financing' in channels and 'investment' not in channels:
        channels.append('investment')
    return channels


def supports_causal_channel(channel, event, condition, mechanism):
    """Check a bounded event-to-mechanism bridge independently of its label.

    Planning priorities do not establish factual relevance. Even an event's
    direct channel, or ``other``, needs a bridge in the mechanism itself; a
    condition mentioning credit cannot justify an unrelated conclusion.
    Lexical links keep topics outside the channel vocabulary usable. Explicit
    references to the affected segment also permit cautious sector summaries.
    These are relevance heuristics, not proof of economic causality.
    """
    event_channels=set(_direct_channels(event))
    body_channels=set(_direct_channels(mechanism))
    # Cost of capital is an explicit financing link even without the literal
    # words "кредит" or "ставка" (as in the saved B01 CAPEX mechanism).
    capital_cost=r'\bстоимост\w*\s+капитал\w*'
    if re.search(capital_cost,event,re.I): event_channels.add('financing')
    if re.search(capital_cost,mechanism,re.I): body_channels.add('financing')
    event_terms,_=_features(event)
    body_terms,_=_features(mechanism)
    # The search stemmer is deliberately small: e.g. "условия" and "условий"
    # retain different suffixes. Permit a shared nontrivial stem here as well.
    shared_terms={left for left in event_terms for right in body_terms
                  if left.startswith(right) or right.startswith(left)}
    upstream=bool(event_channels & body_channels or shared_terms)
    # Preserve an explicit reference to the same affected segment without
    # requiring every industry to appear in a closed topic dictionary.
    segment=r'\bзатронут\w*\s+(?:сегмент|отрасл|рын|област)\w*'
    anaphora=bool(re.search(segment,condition,re.I) and re.search(segment,mechanism,re.I))
    # Only a neutral summary of changed conditions and decision preparation
    # may rely on this reference. Merely adding "в затронутом сегменте" to
    # an unrelated concrete outcome cannot establish an economic bridge.
    neutral_summary=bool(re.search(r'\bизмен\w*\s+услов\w*',mechanism,re.I)
        and re.search(r'\b(?:план|выбор|вариант|решени|работ)\w*',mechanism,re.I))
    if anaphora and neutral_summary and (channel in event_channels or channel=='other' and shared_terms):
        return True
    return upstream and (channel=='other' or channel in body_channels)


def build_plan(company,event,counterparties,source,*,has_financial_inputs=False):
    text=event.title+' '+event.content[:2000]
    channels=channels_for_text(text)
    relevance=rank_news(company,[event],counterparties,limit=1)[0]
    needs_context=bool(channels or relevance['requires_analysis'])
    if 'financing' in channels and (company.mainokved_code or company.mainokved_name):
        channels=list(dict.fromkeys([*channels,'investment']))
    steps=[{'tool':'inspect_company','reason':'Определить известные признаки и границы данных.'}]
    if counterparties and needs_context:
        steps.append({'tool':'summarize_payments','reason':'Проверить размер и концентрацию переданной выборки, исключив собственные переводы.'})
    if source is not None and needs_context:
        steps.append({'tool':'search_bank','query':event.title[:240],
                      'fallback_query':SEARCH[channels[0]] if channels else (company.mainokved_name or event.title)[:240],
                      'reason':'Сопоставить событие с датированной банковской аналитикой.'})
    if has_financial_inputs:
        steps.append({'tool':'calculate_sensitivity','reason':'Выполнить расчёт только по явно предоставленным параметрам и их provenance.'})
    return {'policy':'bounded_python_planner_v2','focus_channels':channels,'selection_reasons':relevance['reasons'],'steps':steps,
            'completion':['interpret','verify'],'max_search_attempts':2,'max_response_corrections':1}


def rank_news(company,events,counterparties=(),*,limit=20):
    """Explainable candidate ordering; scores are heuristics, never probabilities.

    Does not fetch news, infer market share or certify relevance. Final analysis
    can reject a ranked candidate. Names/directions do not prove business roles.
    """
    if type(limit) is not int or not 1<=limit<=100 or len(events)>1000:
        raise ValueError('BOUNDED_NEWS_SELECTION_REQUIRED')
    company_text=' '.join(str(x or '') for x in (company.mainokved_name,company.region,company.city))
    terms,topics=_features(company_text)
    names={p.name.strip() for p in counterparties if p.name and len(p.name.strip())>=5 and p.synthetic_u!=company.synthetic_u}
    results=[];seen={}
    for event in events:
        signature=event.model_dump_json()
        if event.id in seen:
            if seen[event.id]!=signature: raise ValueError('NEWS_ID_CONTENT_CONFLICT')
            continue
        seen[event.id]=signature
        text=event.title+' '+event.content[:4000]
        event_terms,event_topics=_features(text)
        matches=sorted(terms & event_terms);related=sorted(topics & event_topics)
        mentioned=sorted(name for name in names if name.casefold() in text.casefold())
        macro=bool(re.search(TOPICS['financing'],text,re.I) and (company.mainokved_code or company.mainokved_name))
        score=min(1.0,0.15*min(len(matches),3)+0.25*bool(related)+0.3*bool(mentioned)+0.2*macro)
        reasons=[]
        if matches: reasons.append('Совпавшие признаки: '+', '.join(matches))
        if related: reasons.append('Совпавшие темы отрасли: '+', '.join(related))
        if mentioned: reasons.append('Упомянуты участники платёжной выборки: '+', '.join(mentioned))
        if macro: reasons.append('Макрособытие допускает условный канал финансирования; наличие долга не установлено.')
        results.append({'event_id':event.id,'score':score,'reasons':reasons or ['Связь по доступным признакам не найдена.'],
                        'score_kind':'heuristic_not_probability','requires_analysis':score>0})
    return sorted(results,key=lambda item:(-item['score'],item['event_id']))[:limit]
