import { useLayoutEffect, useRef, type KeyboardEvent } from "react";

export function FilterTabs<T extends string>({
  value,
  onChange,
  idPrefix,
  items,
  label,
}: {
  value: T;
  onChange: (value: T) => void;
  idPrefix: string;
  items: { id: T; label: string; count?: number }[];
  label: string;
}) {
  const tabsRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const tabs = tabsRef.current;
    if (!tabs) return;
    const measure = () => {
      const active = tabs.querySelector<HTMLButtonElement>('[aria-selected="true"]');
      if (!active) return;
      tabs.style.setProperty("--tab-left", `${active.offsetLeft}px`);
      tabs.style.setProperty("--tab-width", `${active.offsetWidth}px`);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(tabs);
    for (const button of tabs.querySelectorAll("button")) observer.observe(button);
    return () => observer.disconnect();
  }, [value, items]);
  function navigate(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next: number;
    if (event.key === "ArrowRight") next = (index + 1) % items.length;
    else if (event.key === "ArrowLeft")
      next = (index + items.length - 1) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else return;
    event.preventDefault();
    onChange(items[next].id);
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  }
  return (
    <div ref={tabsRef} className="overview-tabs" role="tablist" aria-label={label}>
      {items.map((tab, index) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          id={`${idPrefix}-tab-${tab.id}`}
          aria-controls={`${idPrefix}-panel-${tab.id}`}
          aria-selected={value === tab.id}
          tabIndex={value === tab.id ? 0 : -1}
          onClick={() => onChange(tab.id)}
          onKeyDown={(e) => navigate(e, index)}
        >
          {tab.label}
          {tab.count !== undefined && (
            <span className="overview-tab-count">{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
