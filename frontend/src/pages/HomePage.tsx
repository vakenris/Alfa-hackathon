import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Icon } from "../components/Icon";
import { CircleButton } from "../components/ui";
import { FilterTabs } from "../components/FilterTabs";
import { NewsCard } from "../components/NewsCard";
import { eventRecommendations } from "../recommendations";
import type { AutomaticAnalysis } from "../hooks/useAutomaticInsights";
import { roles } from "../api";
import type { LiveNews } from "../api";
function plural(count: number, one: string, few: string, many: string) {
  const n = count % 100;
  return n >= 11 && n <= 14 ? many : n % 10 === 1 ? one : n % 10 >= 2 && n % 10 <= 4 ? few : many;
}
export function HomePage({
  company,
  automatic,
  setCompany, companies, news, role, setRole,
}: {
  automatic: AutomaticAnalysis;
  companies: string[];
  news: LiveNews[];
  role: string;
  setRole: (role: string) => void;
  company: string;
  setCompany: (c: string) => void;
}) {
  const newsTopics = [{id:"all", label:"Все новости", category:undefined as string | undefined}, ...Array.from(new Set(news.map(n => n.category))).map((category, i) => ({id:`topic-${i}`, label:category, category}))];
  const trackRef = useRef<HTMLDivElement>(null);
  const [topic, setTopic] = useState("all");
  const [displayedTopic, setDisplayedTopic] = useState("all");
  const contentRef = useRef<HTMLDivElement>(null);
  const animationRef = useRef<Animation | null>(null);
  const transitionId = useRef(0);
  useEffect(() => () => {
    transitionId.current += 1;
    animationRef.current?.cancel();
  }, []);
  const category = newsTopics.find((item) => item.id === displayedTopic)?.category;
  const items = category
    ? news.filter((item) => item.category === category)
    : news;
  useEffect(() => { trackRef.current?.scrollTo({left: 0, behavior: "instant"}); }, [displayedTopic]);
  function scrollNews(direction: -1 | 1) {
    const track = trackRef.current;
    const card = track?.querySelector<HTMLElement>(".news-card");
    if (!track || !card) return;
    const gap = parseFloat(getComputedStyle(track).columnGap) || 0;
    const max = track.scrollWidth - track.clientWidth;
    const atEnd = track.scrollLeft >= max - 2;
    const atStart = track.scrollLeft <= 2;
    const left = direction > 0 && atEnd ? 0
      : direction < 0 && atStart ? max
      : track.scrollLeft + direction * (card.getBoundingClientRect().width + gap);
    track.scrollTo({left, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
  }
  async function transitionNews(update: () => void) {
    const id = ++transitionId.current;
    animationRef.current?.cancel();
    const content = contentRef.current;
    if (!content || matchMedia("(prefers-reduced-motion: reduce)").matches) {
      update();
      return;
    }
    const fadeOut = content.animate(
      [{ opacity: 1, transform: "translateY(0)" }, { opacity: 0, transform: "translateY(-5px)" }],
      { duration: 160, easing: "ease-in", fill: "forwards" },
    );
    animationRef.current = fadeOut;
    try { await fadeOut.finished; } catch { return; }
    if (id !== transitionId.current) return;
    flushSync(update);
    fadeOut.cancel();
    animationRef.current = content.animate(
      [{ opacity: 0, transform: "translateY(7px)" }, { opacity: 1, transform: "translateY(0)" }],
      { duration: 260, easing: "cubic-bezier(.2,.7,.2,1)" },
    );
  }
  function changeTopic(next: string) {
    if (next === topic) return;
    setTopic(next);
    void transitionNews(() => { setDisplayedTopic(next); });
  }
  const recommendationCount = new Set(automatic.insights.flatMap(insight => eventRecommendations(insight.recommended_action).map(action => action.title))).size;
  const productCount = new Set(automatic.insights.map(insight => insight.relevant_bank_product).filter(Boolean)).size;
  const pending = automatic.loading || automatic.jobs.some(job => job.status === "PENDING" || job.status === "PROCESSING");
  return (
    <div className="home-page home-organized">
      <header className="home-heading home-heading-row">
        <h1 id="news-page-title">Новости бизнеса</h1>
      <div className="home-context-bar">
          <div className="selectors">
            <label className="select-control">
              <Icon name="building" />
              <span className="sr-only">Компания</span>
              <select
                value={company}
                onChange={(e) => setCompany(e.target.value)}
              >
                {companies.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
              <Icon name="down" size={18} />
            </label>
            <label className="select-control">
              <Icon name="briefcase" />
              <span className="sr-only">Должность</span>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                {roles.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
              <Icon name="down" size={18} />
            </label>
          </div>
      </div>
      </header>
      <section className="news-section" aria-labelledby="news-page-title">
        <div className="home-news-tools">
          <FilterTabs
            value={topic}
            onChange={changeTopic}
            idPrefix="home"
            items={newsTopics}
            label="Темы новостей"
          />
        </div>
        <div
          ref={contentRef}
          className="overview-content"
          role="tabpanel"
          id={`home-panel-${displayedTopic}`}
          aria-labelledby={`home-tab-${displayedTopic}`}
          tabIndex={0}
        >
          <div className="news-carousel">
            {!items.length && <p role="status">Новостей пока нет.</p>}
            {items.length > 1 && (
              <div className="carousel-prev">
                <CircleButton
                  icon="left"
                  label="Предыдущая новость"
                  onClick={() => scrollNews(-1)}
                />
              </div>
            )}
            <div
              ref={trackRef}
              tabIndex={0}
              role="region"
              aria-label="Горизонтальная лента новостей"
              onKeyDown={(event) => {
                if (event.target !== event.currentTarget) return;
                if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
                  event.preventDefault();
                  scrollNews(event.key === "ArrowRight" ? 1 : -1);
                }
              }}
              className={`news-grid ${items.length === 1 ? "news-grid--single" : ""}`}
            >
              {items.map((item) => (
                <NewsCard key={item.id} item={item} />
              ))}
            </div>
            {items.length > 1 && (
              <div className="carousel-next">
                <CircleButton
                  icon="right"
                  label="Следующая новость"
                  onClick={() => scrollNews(1)}
                />
              </div>
            )}
          </div>
        </div>
      </section>
      <section className="home-company-summary" aria-labelledby="company-summary-title">
        <div className="company-summary-heading"><h2 id="company-summary-title">Для вашей компании</h2>{(automatic.error || pending) && <span className="sr-only" role="status">{automatic.error ? "Анализ временно недоступен. Данные обновятся автоматически." : "Анализируем новые события."}</span>}</div>
        <div className="company-summary-grid">
          <a className="company-summary-card" href="#/analysis"><span className="company-summary-icon"><Icon name="chart" size={22}/></span><span><strong>{automatic.loading ? "—" : automatic.insights.length} <span>{plural(automatic.insights.length, "событие разобрано", "события разобраны", "событий разобрано")}</span></strong><small>Влияние новостей на ваш бизнес</small></span><Icon name="right" size={17}/></a>
          <a className="company-summary-card" href="#/analysis"><span className="company-summary-icon"><Icon name="document" size={22}/></span><span><strong>{automatic.loading ? "—" : recommendationCount} <span>{plural(recommendationCount, "рекомендация", "рекомендации", "рекомендаций")}</span></strong><small>Действия по текущим событиям</small></span><Icon name="right" size={17}/></a>
          <a className="company-summary-card" href="#/analysis"><span className="company-summary-icon"><Icon name="briefcase" size={22}/></span><span><strong>{automatic.loading ? "—" : productCount} <span>{plural(productCount, "решение банка", "решения банка", "решений банка")}</span></strong><small>Продукты, предложенные в анализе</small></span><Icon name="right" size={17}/></a>
        </div>
      </section>
    </div>
  );
}
