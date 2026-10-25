import { useState } from "react";
import { Icon } from "../components/Icon";
import { CircleButton, Modal, Panel } from "../components/ui";
import { BankProducts } from "../components/OpportunitiesWidget";
import { eventRecommendations } from "../recommendations";
import type { AutomaticAnalysis } from "../hooks/useAutomaticInsights";
import type { LiveNews } from "../api";
import { EventRecommendations } from "../components/BusinessInsights";

function analysisPoints(text: string): string[] {
  return text.split(/(?:^|\s)\d+[.)]\s+|\n+/)
    .map(point => point.replace(/^[•—-]\s*/, "").trim())
    .filter(Boolean);
}

export function AnalysisPage({
  company,
  automatic,
  news,
  sourceId,
}: {
  company: string;
  automatic: AutomaticAnalysis;
  news: LiveNews[];
  sourceId?: string;
}) {
  const [detail, setDetail] = useState<{title:string; text:string} | null>(null);
  const {insights, loading, error} = automatic;
  const pending = automatic.jobs.filter(job => job.status === "PENDING" || job.status === "PROCESSING").length;
  const failed = automatic.jobs.filter(job => job.status === "FAILED").length;
  const companyRisks = insights.map(insight => {
    const source = news.find(item => item.id === String(insight.event_id));
    return {id: String(insight.id), title: source?.title ?? insight.what_happened, area: source?.category ?? "Влияние на бизнес", impact: insight.consequences, consequences: analysisPoints(insight.consequences), action: insight.recommended_action, sourceId: String(insight.event_id), sourceLabel: source?.title ?? "Исходная новость"};
  });
  const [riskPage, setRiskPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(companyRisks.length / 2));
  const currentPage = Math.min(riskPage, pageCount - 1);
  const visibleRisks = companyRisks.slice(currentPage * 2, currentPage * 2 + 2);
  const [selectedInsightId, setSelectedInsightId] = useState<number>();
  const selectedInsight = insights.find(item => item.id === selectedInsightId)
    ?? insights.find(item => String(item.event_id) === sourceId)
    ?? insights[0];
  const selectedSource = news.find(item => item.id === String(selectedInsight?.event_id));
  const recommendationCount = selectedInsight ? eventRecommendations(selectedInsight.recommended_action).length : 0;
  const productCount = new Set(insights.map(item => item.relevant_bank_product).filter(Boolean)).size;
  function selectInsight(id: number) {
    setSelectedInsightId(id);
    const index = companyRisks.findIndex(item => item.id === String(id));
    if (index >= 0) setRiskPage(Math.floor(index / 2));
  }
  function selectRecommendations(source: string) {
    const insight = insights.find(item => String(item.event_id) === source);
    if (!insight) return;
    selectInsight(insight.id);
    requestAnimationFrame(() => {
      const card = document.querySelector<HTMLButtonElement>(".recommendation-card");
      card?.focus({preventScroll: true});
      card?.scrollIntoView({block: "nearest", behavior: "instant"});
    });
  }
  return (
    <div className="analysis-page company-analysis analysis-reference">
      <header className="page-header">
        <div>
          <h1>Влияние на бизнес</h1>
          <p className="page-subtitle">
            Что требует внимания и какие действия стоит запланировать.
          </p>
        </div>
        <span className="sr-only">Компания: {company}</span>
      <div className="analysis-summary" aria-label="Сводка анализа">
        <span>
          <Icon name="alert" size={20} />
          <strong>{companyRisks.length}</strong> риска
        </span>
        <span>
          <Icon name="trend" size={20} />
          <strong>{productCount}</strong> {productCount === 1 ? "продукт банка" : productCount < 5 ? "продукта банка" : "продуктов банка"}
        </span>
        <span>
          <Icon name="calendar" size={20} />
          <strong>{recommendationCount}</strong> {recommendationCount === 1 ? "рекомендация" : recommendationCount < 5 ? "рекомендации" : "рекомендаций"}
        </span>
      </div>
      </header>
      <div className="analysis-workspace analysis-overview">
        <Panel className="risk-register risk-widget">
          <div className="analysis-panel-heading">
            <h2>Требуют внимания</h2>
            {pageCount > 1 && <div className="risk-pagination" aria-label="Страницы событий">
              <CircleButton icon="left" label="Предыдущие события" onClick={() => setRiskPage((currentPage + pageCount - 1) % pageCount)}/>
              <span aria-live="polite">{currentPage + 1} / {pageCount}</span>
              <CircleButton icon="right" label="Следующие события" onClick={() => setRiskPage((currentPage + 1) % pageCount)}/>
            </div>}
          </div>
          <div className="analysis-progress">
            {error && <p role="alert">Не удалось загрузить анализ. Переподключаемся автоматически…</p>}
            {(loading || pending > 0) && <p className="automatic-analysis-status" role="status">{loading ? "Загрузка анализа…" : `Автоматически анализируем новости: готово ${automatic.jobs.length - pending - failed} из ${automatic.jobs.length}.`}</p>}
            {failed > 0 && <p role="alert">Для {failed} новостей анализ пока недоступен. Повторим автоматически.</p>}
            {!loading && !pending && !failed && !error && !companyRisks.length && <p>Новостей для анализа пока нет. Новые события будут обработаны автоматически.</p>}
          </div>
          <div className="risk-register-list" data-card-count={visibleRisks.length}>
            {visibleRisks.map((risk, index) => (
              <article
                className={`risk-entry ${selectedSource?.id === risk.sourceId ? "is-source-related" : ""}`}
                key={risk.id}
              >
                <div className="risk-entry-heading">
                  <span className="risk-entry-icon">
                    <Icon
                      name={/логист|транспорт|перевоз/i.test(risk.area) ? "truck" : /финанс|макро/i.test(risk.area) ? "percent" : "building"}
                      size={26}
                    />
                  </span>
                  <div>
                    <span className="risk-area">{risk.area}</span>
                    <h3><button aria-label={`Полный анализ: ${risk.title}`} className="risk-details-button" onClick={() => setDetail({title:risk.title, text:`${risk.impact}\n\nЧто сделать\n${risk.action}`})}>{risk.title}</button></h3>
                  </div>

                  <span className="risk-entry-index" aria-hidden="true">{String(currentPage * 2 + index + 1).padStart(2, "0")}</span>
                </div>
                <ol className="risk-consequences" aria-label="Последствия события">
                  {risk.consequences.slice(0, 2).map((consequence, pointIndex) => (
                    <li key={pointIndex}>
                      <span className="risk-consequence-number" aria-hidden="true">{pointIndex + 1}</span>
                      <span className="risk-consequence-text">{consequence}</span>
                    </li>
                  ))}
                </ol>
                <div className="risk-flow-action">
                  <span className="risk-action-light"><Icon name="idea" size={22}/></span>
                  <strong className="risk-action-label">Что сделать</strong>
                  <p className="risk-action-copy">{risk.action}</p>
                  <button
                    type="button"
                    className="risk-action-open"
                    onClick={() => selectRecommendations(risk.sourceId)}
                    aria-label={`Посмотреть рекомендации: ${risk.title}`}
                  >
                    <Icon name="arrow" size={23}/>
                  </button>
                </div>
                <a className="analysis-source" href={`#/news/${risk.sourceId}`}>
                  <span className="risk-source-label">{risk.sourceLabel}</span> <Icon name="external" size={13} />
                </a>
              </article>
            ))}
          </div>
        </Panel>
        <EventRecommendations insights={insights} news={news} selected={selectedInsight} onSelect={selectInsight} onOpen={setDetail}/>
      </div>
      <BankProducts insights={insights} news={news} selected={selectedInsight} onSelect={selectInsight} onOpen={setDetail}/>
      {detail && <Modal title={detail.title} onClose={() => setDetail(null)}><p className="api-text">{detail.text}</p></Modal>}
    </div>
  );
}
