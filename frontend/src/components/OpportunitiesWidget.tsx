import { useId, useState } from "react";
import { Icon } from "./Icon";
import { Panel } from "./ui";
import type { BusinessOpportunity } from "../data/opportunities";
import { news } from "../data/dashboard";

export function OpportunitiesWidget({ items }: { items: BusinessOpportunity[] }) {
  const id = useId();
  const [selectedId, setSelectedId] = useState(items[0]?.id);
  const [scenarioIds, setScenarioIds] = useState<Record<string, string>>({});
  const selected = items.find(item => item.id === selectedId) ?? items[0];
  if (!selected) return <Panel className="analysis-opportunity"><h2>Возможности для бизнеса</h2><p>Пока нет выявленных возможностей.</p></Panel>;
  const scenario = selected.scenarios.find(item => item.id === scenarioIds[selected.id]) ?? selected.scenarios[0];
  const sources = news.filter(item => selected.sourceIds.includes(item.id));
  return <Panel className="analysis-opportunity opportunities-widget">
    <div className="analysis-panel-heading">
      <h2>Возможности для бизнеса</h2>
      {items.length > 1 && <label className="opportunity-picker">
        <span className="sr-only">Выбрать возможность</span>
        <select value={selected.id} onChange={event => setSelectedId(event.target.value)}>
          {items.map(item => <option key={item.id} value={item.id}>{item.area}: {item.title}</option>)}
        </select>
      </label>}
    </div>
    <div className="opportunity-layout">
      <div className="opportunity-copy" key={selected.id}>
        <span className="opportunity-topic-icon"><Icon name={selected.icon} size={24}/></span>
        <div>
          <h3>{selected.title}</h3><p>{selected.description}</p>
          <div className="opportunity-sources" aria-label="Связанные новости">
            {sources.map(source => <a className="analysis-source" key={source.id} href={`#/news/${source.id}`} title={source.title}>{source.category}<Icon name="external" size={13}/></a>)}
          </div>
        </div>
      </div>
      <div className="financing-scenario opportunity-scenarios">
        {scenario ? <>
          <span className="scenario-label" id={id}>Сценарии и возможные действия</span>
          <div className="scenario-switch" role="group" aria-labelledby={id}>
            {selected.scenarios.map(item => <button type="button" key={item.id} aria-pressed={item.id === scenario.id} onClick={() => setScenarioIds(current => ({...current, [selected.id]: item.id}))}>{item.label}</button>)}
          </div>
          <div className="scenario-outcome" aria-live="polite"><div><p>{scenario.effect}</p><p className="opportunity-next-step"><strong>Следующий шаг: </strong>{scenario.action}</p></div></div>
        </> : <p>Для оценки возможности нужны дополнительные данные.</p>}
      </div>
    </div>
  </Panel>;
}
