import { useEffect, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { HomePage } from "./pages/HomePage";
import { NewsPage } from "./pages/NewsPage";
import { AccountPage } from "./pages/AccountPage";
import { AnalysisPage } from "./pages/AnalysisPage";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { companies, news } from "./data/dashboard";
import "./App.css";
import "./viewport.css";
import "./sidebar.css";

import "./news.css";
import "./overview.css";
import "./analysis.css";
import "./event-detail.css";
import "./analysis-refresh.css";
function App() {
  const [route, setRoute] = useState(location.hash.slice(1) || "/");
  const [context, setContext] = useLocalStorage("alfa-company", {
    company: companies[0],
  });
  const company = companies.includes(context.company)
    ? context.company
    : companies[0];
  const setCompany = (c: string) => setContext({ company: c });
  const [profile, setProfile] = useLocalStorage("alfa-profile", {
    name: "Александр Петров",
    position: "Финансовый директор",
    email: "alex.petrov@technoprom.ru",
    phone: "+7 (999) 123-45-67",
  });
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
        {path === "/account" ? (
          <AccountPage
            profile={profile}
            setProfile={setProfile}
            company={company}
            setCompany={setCompany}
          />
        ) : path === "/analysis" ? (
          <AnalysisPage
            key={company}
            company={company}
            sourceId={analysisSource}
          />
        ) : item ? (
          <NewsPage key={item.id} item={item} />
        ) : path === "/" ? (
          <HomePage company={company} setCompany={setCompany} />
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
