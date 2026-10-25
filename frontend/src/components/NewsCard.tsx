import { Icon } from "./Icon";
import { Badge } from "./ui";
import type { NewsItem } from "../data/dashboard";
export function NewsCard({ item }: { item: NewsItem }) {
  return (
    <article className="news-card">
      <a
        href={`#/news/${item.id}`}
        className="news-image-link"
        tabIndex={-1}
        aria-hidden="true"
      >
        <img className="news-image" src={item.image} alt="" />
      </a>
      <div className="news-card-body">
        <div className="news-meta">
          <span className="category">{item.category}</span>
          <span>{item.time}</span>
        </div>
        <h3>
          <a href={`#/news/${item.id}`}>{item.title}</a>
        </h3>
        <p className="news-card-summary">{item.description}</p>
        <div className="news-card-footer">
          <Badge tone="red">Влияние на бизнес <Icon name="chart" size={27} /></Badge>
          <a
            className="circle-button"
            href={`#/news/${item.id}`}
            aria-label={`Читать: ${item.title}`}
          >
            <Icon name="arrow" />
          </a>
        </div>
      </div>
    </article>
  );
}
