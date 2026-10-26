import { useEffect, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { HomePage } from "./pages/HomePage";
import { NewsPage } from "./pages/NewsPage";
import { AccountPage } from "./pages/AccountPage";
import { AnalysisPage } from "./pages/AnalysisPage";
import { useAutomaticInsights } from "./hooks/useAutomaticInsights";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { request, mapArticle, errorMessage, roles } from "./api";
import type { Company, Article, LiveNews } from "./api";
import "./App.css";
import "./viewport.css";
import "./sidebar.css";

import "./news.css";
import "./overview.css";
import "./analysis.css";
import "./event-detail.css";
import "./analysis-refresh.css";
import "./risk-widget.css";
import "./event-recommendations.css";
import "./analysis-reference.css";
import "./content-layout.css";
const demoProfile = {
  name: "Александр Петров",
  position: "Финансовый директор",
  email: "alex.petrov@example.com",
  phone: "+7 (999) 123-45-67",
};
function App() {
  const [route, setRoute] = useState(location.hash.slice(1) || "/");
  const [companies, setCompanies] = useState<Company[]>([]);
  const [feed, setFeed] = useState<{companyId:number; role:string; items:LiveNews[]} | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [context, setContext] = useLocalStorage("alfa-company-id", { id: 0 });
  const [roleContext, setRoleContext] = useLocalStorage("alfa-role", {value: roles[0]});
  const role = roles.includes(roleContext.value) ? roleContext.value : roles[0];
  const setRole = (value: string) => setRoleContext({value});
  const selected = companies.find(c => c.id === context.id) ?? companies[0];
  const company = selected?.name ?? "";
  const news = feed?.companyId === selected?.id && feed?.role === role ? feed.items : [];
  const feedLoading = !!selected && (feed?.companyId !== selected.id || feed?.role !== role);
  const automatic = useAutomaticInsights(selected?.id, feedLoading ? "" : news.map(item => item.id).join(",") || "empty", role);
  const setCompany = (name: string) => { const c = companies.find(c => c.name === name); if (c) setContext({id:c.id}); };
  useEffect(() => {
    const controller = new AbortController();
    request<Company[]>("/companies", role, {signal:controller.signal}).then(setCompanies).catch(e => { if (!controller.signal.aborted) setError(errorMessage(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [role, revision]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function loadFeed() {
      try {
        const data = await request<{items: Article[]}>(`/companies/${selected!.id}/feed?limit=100`, role, {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)])});
        if (!controller.signal.aborted) { setFeed({companyId: selected!.id, role, items: data.items.map(mapArticle)}); setError(""); }
      } catch (e) { if (!controller.signal.aborted) setError(errorMessage(e)); }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => void loadFeed(), 60000); }
    }
    void loadFeed();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [selected, role, revision]);
  const [savedProfile, setProfile] = useLocalStorage("alfa-profile", demoProfile);
  const profile = {
    name: savedProfile.name?.trim() && savedProfile.name !== "Ваше имя" ? savedProfile.name : demoProfile.name,
    position: savedProfile.position?.trim() || demoProfile.position,
    email: savedProfile.email?.trim() || demoProfile.email,
    phone: savedProfile.phone?.trim() || demoProfile.phone,
  };
  useEffect(() => {
    const onHash = () => {
      setRoute(location.hash.slice(1) || "/");
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const path = route.split("?")[0];
  const analysisSource =
    new URLSearchParams(route.split("?")[1]).get("source") ?? undefined;
  const page =
    path === "/account"
      ? "account"
      : path === "/analysis"
        ? "analysis"
        : "home";
  useEffect(() => {
    document.title = `${page === "account" ? "Личный кабинет" : page === "analysis" ? "Анализ" : "Главная"} — Альфа Бизнес`;
  }, [page]);
  const item = path.startsWith("/news/")
    ? news.find((n) => n.id === path.split("/")[2])
    : undefined;
  return (
    <>
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        К содержимому
      </a>
      <Sidebar page={page} />
      <main
        id="main-content"
        className={`main-content ${item ? "detail-layout" : ""}`}
        tabIndex={-1}
      >
        {error ? <div role="alert" className="not-found"><p>{error}</p><button className="primary-button" onClick={() => {setError(""); setRevision(r => r + 1);}}>Повторить</button></div> : loading ? <p role="status">Загрузка компаний…</p> : !selected ? <p>Компаний пока нет.</p> : path === "/account" ? (
          <AccountPage
            companies={companies.map(c => c.name)}
            profile={profile}
            setProfile={setProfile}
            company={company}
            setCompany={setCompany}
          />
        ) : path === "/analysis" ? (
          <AnalysisPage
            key={selected.id}
            company={company}
            automatic={automatic}
            news={news}
            sourceId={analysisSource}
          />
        ) : feedLoading ? <p role="status">Загрузка новостей…</p> : item ? (
          <NewsPage key={`${selected.id}:${item.id}:${role}`} item={item} automatic={automatic} role={role} />
        ) : path === "/" ? (
          <HomePage automatic={automatic} key={`${selected.id}:${role}`} companies={companies.map(c => c.name)} news={news} role={role} setRole={setRole} company={company} setCompany={setCompany} />
        ) : (
          <div className="not-found">
            <h1>Страница не найдена</h1>
            <a className="primary-button" href="#/">
              На главную
            </a>
          </div>
        )}
      </main>
    </>
  );
}
export default App;
