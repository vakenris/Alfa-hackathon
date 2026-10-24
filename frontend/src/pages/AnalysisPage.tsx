import { useState } from "react";
import { Icon } from "../components/Icon";
import { Panel } from "../components/ui";
import { OpportunitiesWidget } from "../components/OpportunitiesWidget";
import { opportunities } from "../data/opportunities";
import {
  BusinessEvents,
} from "../components/BusinessInsights";
import { businessEvents, news } from "../data/dashboard";
import { companyRisks } from "../data/analysis";
import { useLocalStorage } from "../hooks/useLocalStorage";

export function AnalysisPage({
  company,
  sourceId,
}: {
  company: string;
  sourceId?: string;
}) {
  const selectedSource = news.find((item) => item.id === sourceId);
  const [actionSource, setActionSource] = useState<string>();
  const [plan, setPlan] = useLocalStorage<{ completedIds: string[] }>(
    `alfa-plan:${company}`,
    { completedIds: [] },
  );
  const completedIds = Array.isArray(plan.completedIds)
    ? plan.completedIds
    : [];
  const completed = businessEvents.filter((event) =>
    completedIds.includes(event.id),
  ).length;
  function toggleTask(id: string) {
    setPlan({
      completedIds: completedIds.includes(id)
        ? completedIds.filter((item) => item !== id)
        : [...completedIds, id],
    });
  }
  function focusAction(source: string) {
    setActionSource(source);
    requestAnimationFrame(() =>
      document
        .querySelector<HTMLInputElement>(
          `.action-plan-list [data-source-ids~="${source}"] input`,
        )
        ?.focus(),
    );
  }
  return (
    <div className="analysis-page company-analysis">
      <header className="page-header">
        <div>
          <h1>Влияние на бизнес</h1>
          <p className="page-subtitle">
            Что требует внимания и какие действия стоит запланировать.
          </p>
        </div>
        <div className="analysis-header-context">
          {selectedSource && (
            <a
              href={`#/news/${selectedSource.id}`}
              className="analysis-context-source"
            >
              Из новости: {selectedSource.category}{" "}
              <Icon name="left" size={13} />
            </a>
          )}
        </div>
      <div className="analysis-summary" aria-label="Сводка анализа">
        <span>
          <Icon name="alert" size={20} />
          <strong>{companyRisks.length}</strong> риска
        </span>
        <span>
          <Icon name="trend" size={20} />
          <strong>{opportunities.length}</strong> {opportunities.length === 1 ? "возможность" : opportunities.length < 5 ? "возможности" : "возможностей"}
        </span>
        <span>
          <Icon name="calendar" size={20} />
          <strong>{businessEvents.length - completed}</strong> действий осталось
        </span>
      </div>
      </header>
      <div className="analysis-workspace">
        <Panel className="risk-register">
          <div className="analysis-panel-heading">
            <h2>Требуют внимания</h2>
          </div>
          <div className="risk-register-list">
            {companyRisks.map((risk, index) => (
              <article
                className={`risk-entry ${selectedSource?.id === risk.sourceId ? "is-source-related" : ""}`}
                key={risk.id}
              >
                <div className="risk-entry-heading">
                  <span className="risk-entry-icon">
                    <Icon
                      name={risk.id === "financing" ? "percent" : "building"}
                      size={20}
                    />
                  </span>
                  <div>
                    <span className="risk-area">{risk.area}</span>
                    <h3>{risk.title}</h3>
                  </div>
                  {index === 0 && (
                    <span className="analysis-priority">Высокий приоритет</span>
                  )}
                  <span className="risk-entry-index">0{index + 1}</span>
                </div>
                <div className="risk-flow">
                  <div className="risk-flow-impact">
                    <span className="risk-flow-label">Последствие</span>
                    <p>{risk.impact}</p>
                  </div>
                  <span className="risk-flow-connector">
                    <Icon name="arrow" size={18} />
                  </span>
                  <div className="risk-flow-action">
                    <div className="risk-action-content">
                      <span className="risk-flow-label">
                        <Icon name="idea" size={16} />
                        Что сделать
                      </span>
                      <p>{risk.action}</p>
                    </div>
                    <button
                      type="button"
                      className="risk-action-open"
                      onClick={() => focusAction(risk.sourceId)}
                      aria-label={`Перейти к действию в плане: ${risk.title}`}
                    >
                      <Icon name="arrow" size={20} />
                    </button>
                  </div>
                </div>
                <a className="analysis-source" href={`#/news/${risk.sourceId}`}>
                  {risk.sourceLabel} <Icon name="external" size={13} />
                </a>
              </article>
            ))}
          </div>
        </Panel>
        <BusinessEvents
          completedIds={completedIds}
          onToggle={toggleTask}
          sourceId={actionSource ?? selectedSource?.id}
        />
      </div>
      <OpportunitiesWidget items={opportunities} />
      <p className="analysis-footnote">
        Риски и сценарии показаны на демонстрационных данных. Финансовые
        показатели компании не подключены.
      </p>
    </div>
  );
}
