import { useEffect, useState } from "react";
import { getBudgetMetrics } from "../api";
import type { BudgetMetrics } from "../api";

export function BudgetGuardianWidget({ expanded }: { expanded: boolean }) {
  const [budget, setBudget] = useState<BudgetMetrics | null>(null);

  useEffect(() => {
    let mounted = true;

    async function load() {
      try {
        const data = await getBudgetMetrics();
        if (mounted) {
          setBudget(data);
        }
      } catch {
        // Silently preserve previous budget state if background poll fails
      }
    }

    void load();
    const interval = setInterval(() => void load(), 30000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  if (!budget) return null;

  const percent = Math.min(
    100,
    Math.round((budget.spent_budget_rub / budget.daily_budget_rub) * 100)
  );

  if (!expanded) {
    return (
      <div
        className="budget-mini-badge"
        title={`Бюджет AI: ${budget.spent_budget_rub.toFixed(2)} ₽ / ${budget.daily_budget_rub} ₽ (${percent}%)`}
      >
        <span className="budget-dot" />
        <span className="budget-mini-text">{percent}%</span>
      </div>
    );
  }

  return (
    <div className="budget-guardian-widget">
      <div className="budget-widget-header">
        <span className="budget-shield-icon">🛡️</span>
        <div className="budget-title-block">
          <span className="budget-widget-title">Budget Guardian</span>
          <span className="budget-widget-sub">Лимит 500 ₽ / день</span>
        </div>
      </div>

      <div className="budget-progress-track">
        <div
          className={`budget-progress-bar ${
            percent > 90 ? "critical" : percent > 60 ? "warning" : "ok"
          }`}
          style={{ width: `${Math.max(4, percent)}%` }}
        />
      </div>

      <div className="budget-widget-stats">
        <span>
          <strong>{budget.spent_budget_rub.toFixed(2)} ₽</strong> из{" "}
          {budget.daily_budget_rub} ₽
        </span>
        <span className="budget-tokens">
          {budget.total_tokens_used.toLocaleString("ru-RU")} токенов
        </span>
      </div>
    </div>
  );
}
