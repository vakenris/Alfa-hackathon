import { useState } from "react";
import { Icon } from "../components/Icon";
import { Badge, IconBubble, Modal, Panel } from "../components/ui";
import { newsInsights } from "../data/newsInsights";
import type { NewsItem } from "../data/dashboard";
const sources = [
  {
    name: "РБК",
    logo: "РБК",
    color: "#c90000",
    time: "12:30",
    url: "https://www.rbc.ru/",
  },
  {
    name: "ТАСС",
    logo: "ТАСС",
    color: "#1010a4",
    time: "12:28",
    url: "https://tass.ru/",
  },
  {
    name: "Интерфакс",
    logo: "↗",
    color: "#ff6409",
    time: "12:25",
    url: "https://www.interfax.ru/",
  },
  {
    name: "Банк России",
    logo: "БР",
    color: "#191d20",
    time: "12:20",
    url: "https://cbr.ru/",
  },
  {
    name: "Ведомости",
    logo: "В",
    color: "#e80025",
    time: "12:18",
    url: "https://www.vedomosti.ru/",
  },
];
export function NewsPage({ item }: { item: NewsItem }) {
  const [detail, setDetail] = useState<{ title: string; text: string } | null>(
    null,
  );
  const insight = newsInsights[item.id];
  return (
    <div className="article-page">
      <div className="article-grid">
        <div className="article-main">
          <Panel className="article-content">
            <div className="article-toolbar">
              <a className="back-link" href="#/">
                <Icon name="left" size={18} />
                Назад к новостям
              </a>
              <div className="news-meta">
                <span className="category">{item.category}</span>
                <span className="category">{item.time}</span>
              </div>
            </div>
            <h1>{item.title}</h1>
            <p className="article-description">{item.description}</p>
            <img
              className="article-image"
              src={
                item.id === "key-rate" ? "/images/bank-detail.png" : item.image
              }
              alt={item.imageAlt}
            />
            <div className="takeaway">
              <IconBubble name="chart" />
              <div>
                <strong>Ключевой вывод</strong>
                <p>{item.takeaway}</p>
              </div>
            </div>
          </Panel>
        </div>
        <div className="article-aside">
          <Panel className="event-effects">
            <div className="section-heading">
              <h2>Что меняет это событие</h2>
              <button
                className="icon-button"
                aria-label="Оценка влияния"
                onClick={() =>
                  setDetail({
                    title: "Последствия конкретного события",
                    text: "Здесь показан механизм влияния одного события. Сводные приоритеты и план компании находятся в разделе «Анализ». Персональные финансовые расчёты не подключены.",
                  })
                }
              >
                <Icon name="info" />
              </button>
            </div>
            <div className="effect-list">
              {item.effects.map((e, i) => (
                <button
                  className="effect-row"
                  key={e.title}
                  onClick={() =>
                    setDetail({
                      title: e.title,
                      text: insight.effects[i] ?? e.description,
                    })
                  }
                >
                  <IconBubble
                    name={i === 0 ? "percent" : i === 1 ? "database" : "chart"}
                    tone={i === 2 ? "orange" : "red"}
                  />
                  <span className="row-copy">
                    <strong>{e.title}</strong>
                    <span>{e.description}</span>
                  </span>
                  <Badge tone={i === 2 ? "orange" : "red"}>{e.status}</Badge>
                  <Icon name="right" size={19} />
                </button>
              ))}
            </div>
          </Panel>
          <Panel className="event-analysis-link">
            <span className="event-analysis-icon">
              <Icon name="chart" size={24} />
            </span>
            <div>
              <h2>От события — к общему плану</h2>
              <p>
                Приоритеты компании, сценарии и действия по всем событиям
                собраны в разделе «Анализ».
              </p>
            </div>
            <a
              href={`#/analysis?source=${item.id}`}
              className="event-analysis-button"
            >
              К сводному анализу <Icon name="arrow" size={18} />
            </a>
          </Panel>
        </div>
      </div>
      <Panel className="sources">
        <h2>Источники</h2>
        <div className="sources-list">
          {sources.map((s) => (
            <a href={s.url} target="_blank" rel="noreferrer" key={s.name}>
              <span className="source-logo" style={{ background: s.color }}>
                {s.logo}
              </span>
              <span>
                <strong>{s.name}</strong>
                <small>Сегодня, {s.time}</small>
              </span>
              <Icon name="external" size={17} />
            </a>
          ))}
        </div>
        <p className="demo-note">
          Демо-новость по макету. Ссылки ведут на главные страницы изданий.
        </p>
      </Panel>
      {detail && (
        <Modal title={detail.title} onClose={() => setDetail(null)}>
          <p>{detail.text}</p>
          <button className="primary-button" onClick={() => setDetail(null)}>
            Понятно
          </button>
        </Modal>
      )}
    </div>
  );
}
