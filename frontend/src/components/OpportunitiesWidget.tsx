import { Icon } from "./Icon";
import { Panel } from "./ui";
import type { EventPanelProps } from "./BusinessInsights";

function benefitPoints(text: string): string[] {
  const normalized = text.replace(/^(?:Сгенерированный\s+)?AI-анализ:\s*/iu, "").replace(/^компании рекомендуется\s*/iu, "");
  return normalized.split(/\n+|;\s*|\s+и\s+(?=хеджировать|снизить|сохранить|зафиксировать|оптимизировать|актуализировать)/iu)
    .map(point => point.trim()).filter(Boolean).slice(0, 3)
    .map(point => point.charAt(0).toUpperCase() + point.slice(1).replace(/[.;]$/, ""));
}

export function BankProducts({insights, news, selected, onSelect, onOpen}: EventPanelProps) {
  const products = insights.filter(insight => insight.relevant_bank_product);
  const source = news.find(item => item.id === String(selected?.event_id));
  const product = selected?.relevant_bank_product;
  const benefits = selected ? benefitPoints(selected.ai_view) : [];
  return <Panel className="bank-products bank-products-wide">
    <div className="bank-products-header">
      <div className="analysis-panel-heading"><div><h2>Продукты банка</h2><p>Решения, которые могут быть полезны в текущей ситуации.</p></div></div>
      <label className="bank-product-picker"><span className="sr-only">Продукт банка по событию</span><select value={product ? selected!.id : ""} onChange={event => onSelect(Number(event.target.value))} disabled={!products.length}>
        {!product && <option value="">Продукт для события не предложен</option>}
        {products.map(insight => <option key={insight.id} value={insight.id}>Продукты банка: {insight.relevant_bank_product}{products.length > 1 ? ` · ${news.find(item => item.id === String(insight.event_id))?.category ?? "Событие"}` : ""}</option>)}
      </select></label>
    </div>
    {selected && product ? <div className="bank-product-surface">
      <div className="bank-product-intro">
        <span className="bank-product-symbol"><Icon name={/фактор|лизинг/i.test(product) ? "briefcase" : "percent"} size={29}/></span>
        <div className="bank-product-copy"><h3>{product}</h3><p>{selected.why_important}</p>
          <div className="bank-product-actions"><button className="bank-product-details" onClick={() => onOpen({title: product, text: [selected.why_important, selected.bank_view, selected.ai_view, selected.recommended_action].join("\n\n")})}>Подробнее <Icon name="arrow" size={16}/></button>{source && <a className="bank-event-category" href={`#/news/${source.id}`}>{source.category}<Icon name="external" size={13}/></a>}</div>
        </div>
      </div>
      <div className="bank-product-benefits"><h3>Как это поможет</h3><ul>{benefits.map((benefit, index) => <li key={index}><span className="bank-benefit-check"><Icon name="check" size={13}/></span><span>{benefit}</span></li>)}</ul></div>
    </div> : <p className="event-panel-empty">{selected ? "Для этой новости банковский продукт не предложен. Рекомендации доступны выше." : "Продукты и сценарии появятся после автоматического анализа новостей."}</p>}
  </Panel>;
}
