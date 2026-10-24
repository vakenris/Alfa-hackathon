import { Icon } from "./Icon";
import { Panel } from "./ui";
import { PlanProgress } from "./AnalysisVisuals";
import { businessEvents } from "../data/dashboard";

export function BusinessEvents({
  completedIds,
  onToggle,
  sourceId,
}: {
  completedIds: string[];
  onToggle: (id: string) => void;
  sourceId?: string;
}) {
  const completed = businessEvents.filter((event) =>
    completedIds.includes(event.id),
  ).length;
  return (
    <Panel className="action-plan">
      <div className="analysis-panel-heading">
        <div>
          <h2>План действий</h2>
          <span className="plan-heading-subtitle">
            Отмечайте выполненные шаги
          </span>
        </div>
        <PlanProgress completed={completed} total={businessEvents.length} />
        <span className="sr-only" role="status">
          Выполнено {completed} из {businessEvents.length}
        </span>
      </div>
      <ul className="action-plan-list">
        {businessEvents.map((event, index) => (
          <li
            key={event.id}
            data-source-ids={event.sourceIds.join(" ")}
            className={`${completedIds.includes(event.id) ? "is-complete" : ""} ${sourceId && event.sourceIds.includes(sourceId) ? "is-source-related" : ""}`}
          >
            <label>
              <input
                type="checkbox"
                checked={completedIds.includes(event.id)}
                onChange={() => onToggle(event.id)}
              />
              <span className="action-plan-icon"><Icon name={(["chart", "briefcase", "user", "building", "mail"] as const)[index]} size={20} /></span>
              <span className="action-plan-title">{event.title}</span>
              <span className="action-plan-date"><Icon name="calendar" size={14} />{event.date}</span>
            </label>
            <a
              className="action-plan-source"
              href={`#/news/${event.sourceIds[0]}`}
              aria-label={`Исходная новость: ${event.title}`}
            >
              <Icon name="arrow" size={15} />
            </a>
          </li>
        ))}
      </ul>
      <p className="analysis-footnote">
        Отметки сохраняются для выбранной компании в этом браузере.
      </p>
    </Panel>
  );
}
