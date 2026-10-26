import { useState } from "react";
import { Badge, IconBubble, Panel } from "./ui";
import { Icon } from "./Icon";
import { askQuestion } from "../api";
import type { QuestionAnswerResponse } from "../api";

interface InsightQAProps {
  insightId: number;
  role: string;
}

export function InsightQA({ insightId, role }: InsightQAProps) {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<QuestionAnswerResponse[]>([]);

  // Suggested questions based on active user role
  const suggestionsByRole: Record<string, string[]> = {
    "Финансовый директор": [
      "Каково влияние изменения ставки на оборотный капитал?",
      "Какие риски для стоимости кредитного портфеля?",
      "Каковы рекомендации по оптимизации казначейских потоков?",
    ],
    "Генеральный директор": [
      "Как это событие отразится на контрагентах и цепочке поставок?",
      "Какие новые бизнес-возможности открываются для компании?",
      "Каковы стратегические риски для операционной деятельности?",
    ],
    "Бизнес-аналитик": [
      "Объясни расчёт чувствительности к ставке ЦБ",
      "Сравни позицию Альфа-Банка с отраслевыми прогнозами",
      "Какие качественные сценарии предусмотрены моделью?",
    ],
  };

  const currentSuggestions =
    suggestionsByRole[role] || suggestionsByRole["Финансовый директор"];

  async function handleAsk(queryToAsk?: string) {
    const q = (queryToAsk ?? question).trim();
    if (!q || loading) return;

    setLoading(true);
    setError(null);

    try {
      const res = await askQuestion(insightId, q, role);
      setHistory((prev) => [res, ...prev]);
      if (!queryToAsk) {
        setQuestion("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить ответ");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel className="insight-qa-panel">
      <div className="qa-header">
        <div className="qa-title-row">
          <IconBubble name="chart" tone="red" size={20} />
          <div>
            <h3>Вопрос-ответ по событию (AI Drilldown)</h3>
            <p className="qa-subtitle">
              Интерактивный анализ с проверкой фактов и ролевой адаптацией (роль:{" "}
              <strong>{role}</strong>)
            </p>
          </div>
        </div>
        <Badge tone="red">Критерий 10: Q&A</Badge>
      </div>

      <div className="qa-suggestions">
        <span className="suggestions-label">Частые вопросы:</span>
        <div className="chips-list">
          {currentSuggestions.map((item, idx) => (
            <button
              key={idx}
              type="button"
              className="chip-btn"
              disabled={loading}
              onClick={() => {
                setQuestion(item);
                void handleAsk(item);
              }}
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      <form
        className="qa-form"
        onSubmit={(e) => {
          e.preventDefault();
          void handleAsk();
        }}
      >
        <div className="qa-input-wrapper">
          <input
            type="text"
            className="qa-input"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Задайте свой вопрос по рискам, ликвидности или банковским решениям…"
            disabled={loading}
          />
          <button
            type="submit"
            className="qa-submit-btn primary-button"
            disabled={loading || !question.trim()}
          >
            {loading ? (
              <>
                <span className="qa-spinner" /> Анализ…
              </>
            ) : (
              <>
                <Icon name="arrow" size={16} /> Спросить AI
              </>
            )}
          </button>
        </div>
      </form>

      {error && (
        <div className="qa-error-alert" role="alert">
          <Icon name="info" size={16} />
          <span>{error}</span>
        </div>
      )}

      {history.length > 0 && (
        <div className="qa-history">
          {history.map((item, index) => (
            <div key={index} className="qa-history-item">
              <div className="qa-question-bubble">
                <span className="qa-bubble-label">Вопрос:</span>
                <strong>{item.question}</strong>
              </div>
              <div className="qa-answer-bubble">
                <div className="qa-answer-content">
                  {item.answer.split("\n\n").map((paragraph, pIdx) => (
                    <p key={pIdx}>{paragraph}</p>
                  ))}
                </div>
                {item.relevant_source && (
                  <div className="qa-source-badge">
                    <Icon name="database" size={14} />
                    <span>
                      <strong>Обоснование:</strong> {item.relevant_source}
                    </span>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
