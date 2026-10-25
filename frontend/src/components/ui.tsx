import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import type { Tone } from "../data/dashboard";
export function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={`panel ${className}`}>{children}</section>;
}
export function IconBubble({
  name,
  tone = "red",
  size = 24,
}: {
  name: IconName;
  tone?: Tone;
  size?: number;
}) {
  return (
    <span className={`icon-bubble tone-${tone}`}>
      <Icon name={name} size={size} />
    </span>
  );
}
export function Badge({
  children,
  tone = "red",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  return <span className={`badge tone-${tone}`}>{children}</span>;
}
export function CircleButton({
  icon,
  label,
  onClick,
}: {
  icon: IconName;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className="circle-button" aria-label={label} onClick={onClick}>
      <Icon name={icon} />
    </button>
  );
}
export function Modal({
  title,
  children,
  onClose,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
}) {
  const titleId = useId();
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    d?.showModal();
    return () => d?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${className}`}
      aria-labelledby={titleId}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal-heading">
        <h2 id={titleId}>{title}</h2>
        <CircleButton icon="close" label="Закрыть окно" onClick={onClose} />
      </div>
      {children}
    </dialog>
  );
}
