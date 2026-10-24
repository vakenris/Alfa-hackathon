import { Icon } from "./Icon";
import { useLocalStorage } from "../hooks/useLocalStorage";
import type { IconName } from "./Icon";
export type Page = "home" | "analysis" | "account";
const links: { page: Page; href: string; label: string; icon: IconName }[] = [
  { page: "home", href: "#/", label: "Главная", icon: "home" },
  { page: "analysis", href: "#/analysis", label: "Анализ", icon: "chart" },
  { page: "account", href: "#/account", label: "Личный кабинет", icon: "user" },
];
export function Sidebar({ page }: { page: Page }) {
  const [preferences, setPreferences] = useLocalStorage("alfa-navigation", {
    expanded: false,
  });
  const expanded = preferences.expanded === true;
  const toggleLabel = expanded ? "Свернуть навигацию" : "Развернуть навигацию";
  return (
    <aside className={`sidebar ${expanded ? "sidebar--expanded" : ""}`}>
      <a className="brand" href="#/" aria-label="На главную">
        А<span />
      </a>
      <nav id="primary-navigation" aria-label="Основная навигация">
        {links.map((l) => (
          <a
            key={l.page}
            href={l.href}
            title={l.label}
            aria-label={l.label}
            aria-current={page === l.page ? "page" : undefined}
            className={`nav-link ${page === l.page ? "active" : ""}`}
          >
            <Icon name={l.icon} size={29} />
            <span className="nav-label">{l.label}</span>
          </a>
        ))}
      </nav>
      <button
        type="button"
        className="sidebar-toggle"
        aria-expanded={expanded}
        aria-controls="primary-navigation"
        aria-label={toggleLabel}
        title={toggleLabel}
        onClick={() => setPreferences({ expanded: !expanded })}
      >
        <Icon name="right" size={20} />
      </button>
    </aside>
  );
}
