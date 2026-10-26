import { useState } from "react";
import { Badge, IconBubble, Modal, Panel } from "../components/ui";
import { Icon } from "../components/Icon";
import { InsightQA } from "../components/InsightQA";
import type { AutomaticAnalysis } from "../hooks/useAutomaticInsights";
import type { LiveNews } from "../api";
export function NewsPage({item, automatic, role = "Финансовый директор"}: {item:LiveNews; automatic:AutomaticAnalysis; role?: string}) {
  const insight = automatic.insights.find(result => result.event_id === Number(item.id));
  const job = automatic.jobs.find(result => result.event_id === Number(item.id));
  const failed = job?.status === "FAILED";
  const [detail, setDetail] = useState<{title: string; text: string} | null>(null);
  const effects = [
    {title: "Влияние на компанию", text: insight ? `${insight.why_important}\n\n${insight.consequences}` : null},
    {title: "Позиция банка", text: insight?.bank_view},
    {title: "Рекомендации и возможности", text: insight ? [insight.recommended_action, insight.ai_view, insight.relevant_bank_product].filter(Boolean).join("\n\n") : null},
  ];
  const sources = insight?.evidences.length ? insight.evidences : [{source_title:item.source, claim:item.description, source_url:null, company_feature:null}];
  return (
    <div className="article-page article-compact article-organized">
      <Panel className="article-workspace">
        <header className="article-heading">
          <div className="article-toolbar">
            <a className="back-link" href="#/"><Icon name="left" size={18}/>Назад к новостям</a>
            <div className="news-meta"><span className="category">{item.category}</span><span>{item.time}</span></div>
          </div>
          <h1>{item.title}</h1>
          <p className="article-description">{item.description}</p>
        </header>
        <div className="article-overview-grid">
          <div className="article-visual">
            <img className="article-image" src={item.category === "Макроэкономика" ? "/images/bank-detail.png" : item.image} alt={item.imageAlt}/>
            <div className="takeaway"><IconBubble name="chart"/><div><strong>Ключевой вывод</strong><p>{insight?.what_happened ?? item.takeaway}</p></div></div>
          </div>
          <section className="event-effects" aria-labelledby="event-effects-title">
            <div className="section-heading"><h2 id="event-effects-title">Что меняет это событие</h2><button className="icon-button" aria-label="Оценка влияния" onClick={() => setDetail({title:"Последствия конкретного события",text:"Здесь показан автоматический анализ события для выбранной компании. Сводные рекомендации по всем событиям находятся в разделе «Анализ»."})}><Icon name="info"/></button></div>
            {!insight && <p className="automatic-analysis-status" role={failed || automatic.error ? "alert" : "status"}>{automatic.error ? "Не удалось получить анализ. Переподключаемся автоматически…" : failed ? "Не удалось подготовить анализ. Повторим автоматически." : "Анализируем событие для вашей компании…"}</p>}
            <div className="effect-list">{effects.map((effect,index) => <button className="effect-row" key={effect.title} onClick={() => setDetail({title:effect.title,text:effect.text ?? "Анализ события формируется автоматически. Результат появится после обработки."})}>
              <IconBubble name={index===0 ? "percent" : index===1 ? "database" : "chart"} tone={index===2 ? "orange" : "red"}/>
              <span className="row-copy"><strong>{effect.title}</strong><span>{effect.text ? `${effect.text.slice(0,120)}${effect.text.length>120 ? "…" : ""}` : failed ? "Анализ временно недоступен" : "Анализ формируется автоматически"}</span></span>
              <Badge tone={index===2 ? "orange" : "red"}>{effect.text ? "Подробнее" : "Ожидает"}</Badge><Icon name="right" size={18}/>
            </button>)}</div>
          </section>
        </div>
        <div className="article-footer-grid">
          {insight && <InsightQA insightId={insight.id} role={role} />}
          <section className="event-analysis-link"><span className="event-analysis-icon"><Icon name="chart" size={24}/></span><div><h2>Общая картина для компании</h2><p>Все события, рекомендации и банковские решения — в одном разделе.</p><a className="event-analysis-button" href={`#/analysis?source=${item.id}`}>К сводному анализу <Icon name="arrow" size={17}/></a></div></section>
        </div>
      </Panel>
          <Panel className="sources article-sources-widget"><h2 id="article-sources-title">Источники и обоснования</h2><div className="sources-list">{sources.map((source,index) => {
            const content = <><span className="source-logo"><Icon name="building" size={17}/></span><span><strong>{source.source_title}</strong><small>{source.claim}</small></span><Icon name="external" size={16}/></>;
            return source.source_url && /^https?:\/\//i.test(source.source_url) ? <a key={index} href={source.source_url} target="_blank" rel="noreferrer">{content}</a> : <button key={index} className="source-reference" onClick={() => setDetail({title:source.source_title,text:[source.claim,source.company_feature].filter(Boolean).join("\n")})}>{content}</button>;
          })}</div></Panel>
      {detail && <Modal title={detail.title} onClose={() => setDetail(null)}><p className="api-text">{detail.text}</p><button className="primary-button" onClick={() => setDetail(null)}>Понятно</button></Modal>}
    </div>
  );
}
