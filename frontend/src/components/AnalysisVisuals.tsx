
export function PlanProgress({
  completed,
  total,
}: {
  completed: number;
  total: number;
}) {
  const circumference = 2 * Math.PI * 21;
  const progress = total > 0 ? Math.min(completed / total, 1) : 0;
  return (
    <div
      className="plan-progress-visual"
      role="progressbar"
      aria-label="Выполнение плана"
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={completed}
      aria-valuetext={`${completed} из ${total} действий выполнено`}
    >
      <svg viewBox="0 0 52 52" width="52" height="52" aria-hidden="true">
        <circle className="plan-ring-track" cx="26" cy="26" r="21" />
        <circle
          className="plan-ring-value"
          cx="26"
          cy="26"
          r="21"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - progress)}
        />
      </svg>
      <span aria-hidden="true">
        <strong>{completed}</strong>
        <small>/{total}</small>
      </span>
    </div>
  );
}

