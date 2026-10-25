import { Icon } from "./Icon";
import { Panel } from "./ui";
import type { Insight, LiveNews } from "../api";
import { eventRecommendations } from "../recommendations";

export interface EventPanelProps {
  insights: Insight[];
  news: LiveNews[];
  selected?: Insight;
  onSelect: (id: number) => void;
  onOpen: (detail: {title: string; text: string}) => void;
}

export function EventRecommendations({selected, onOpen}: EventPanelProps) {
  const recommendations = selected ? eventRecommendations(selected.recommended_action) : [];
  return <Panel className="event-recommendations">
    <div className="analysis-panel-heading recommendation-heading">
      <div><h2><span className="recommendations-spark"><Icon name="sparkles" size={24}/></span>Рекомендации по событию</h2><p>Как можно использовать ситуацию и минимизировать риски.</p></div>

    </div>
    <ul className="recommendation-list" aria-label="Рекомендуемые действия">
      {recommendations.map((recommendation, index) => <li key={index}>
        <button className="recommendation-card" onClick={() => onOpen({title: recommendation.title, text: [recommendation.description, selected?.why_important, selected?.recommended_action].filter(Boolean).join("\n\n")})}>
          <span className="recommendation-icon"><Icon name={recommendation.icon} size={23}/></span>
          <span className="recommendation-copy"><strong>{recommendation.title}</strong><span>{recommendation.description}</span></span>
          <Icon name="right" size={18}/>
        </button>
      </li>)}
    </ul>
    {!recommendations.length && <p className="event-panel-empty">Рекомендации появятся после анализа новости.</p>}
  </Panel>;
}
