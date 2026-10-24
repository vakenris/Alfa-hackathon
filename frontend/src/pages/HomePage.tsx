import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Icon } from "../components/Icon";
import { CircleButton } from "../components/ui";
import { FilterTabs } from "../components/FilterTabs";
import { NewsCard } from "../components/NewsCard";
import { companies, roles, news, newsTopics } from "../data/dashboard";
export function HomePage({
  company,
  setCompany,
}: {
  company: string;
  setCompany: (c: string) => void;
}) {
  const [role, setRole] = useState(roles[0]);
  const [offset, setOffset] = useState(0);
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
  const ordered = [...items.slice(offset), ...items.slice(0, offset)];
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
    void transitionNews(() => { setDisplayedTopic(next); setOffset(0); });
  }
  return (
    <div className="home-page">
      <div className="home-top">
        <div className="welcome">
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
          <h1 id="news-page-title">Новости бизнеса</h1>
          <p className="page-subtitle">
            События рынка и отраслей, которые стоит держать в поле зрения.
          </p>
        </div>
      </div>
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
            {items.length > 1 && (
              <div className="carousel-prev">
                <CircleButton
                  icon="left"
                  label="Предыдущая новость"
                  onClick={() =>
                    void transitionNews(() => setOffset((current) => (current + items.length - 1) % items.length))
                  }
                />
              </div>
            )}
            <div
              className={`news-grid ${items.length === 1 ? "news-grid--single" : ""}`}
            >
              {ordered.map((item) => (
                <NewsCard key={item.id} item={item} />
              ))}
            </div>
            {items.length > 1 && (
              <div className="carousel-next">
                <CircleButton
                  icon="right"
                  label="Следующая новость"
                  onClick={() => void transitionNews(() => setOffset((current) => (current + 1) % items.length))}
                />
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
