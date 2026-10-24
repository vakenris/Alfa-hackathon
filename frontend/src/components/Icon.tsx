export type IconName =
  | "home"
  | "chart"
  | "user"
  | "building"
  | "briefcase"
  | "down"
  | "right"
  | "left"
  | "arrow"
  | "alert"
  | "trend"
  | "calendar"
  | "mail"
  | "phone"
  | "edit"
  | "percent"
  | "database"
  | "info"
  | "external"
  | "idea"
  | "sparkles"
  | "close";
const paths: Record<IconName, string> = {
  home: "m3 10 9-7 9 7M5 9v12h5v-7h4v7h5V9",
  chart: "M4 20V13h3v7zm7 0V8h3v12zm7 0V3h3v17z",
  user: "M20 21v-2a8 8 0 0 0-16 0v2ZM16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  building:
    "M5 21V3h14v18ZM9 7h.01M15 7h.01M9 11h.01M15 11h.01M9 15h.01M15 15h.01M11 21v-3h2v3",
  briefcase: "M3 7h18v14H3ZM8 7V3h8v4M3 11l9 4 9-4",
  down: "m6 9 6 6 6-6",
  right: "m9 5 7 7-7 7",
  left: "m15 5-7 7 7 7",
  arrow: "M4 12h16m-7-7 7 7-7 7",
  alert: "m12 3 10 18H2ZM12 9v5M12 17h.01",
  trend: "m3 17 6-6 5 5 7-12m-7 0h7v7",
  calendar:
    "M4 5h16v16H4ZM8 3v4m8-4v4M4 10h16M8 14h.01M12 14h.01M16 14h.01M8 18h.01",
  mail: "M3 5h18v14H3Zm0 0 9 8 9-8",
  phone: "M7 3 3 5c-1 7 9 17 16 16l2-4-5-3-2 2c-3-1-5-3-6-6l2-2Z",
  edit: "m4 16-1 5 5-1L21 7l-5-5ZM14 4l5 5",
  percent:
    "m5 20 14-16M9 6a3 3 0 1 1-6 0 3 3 0 0 1 6 0M21 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  database:
    "M21 6c0 2-4 4-9 4S3 8 3 6s4-4 9-4 9 2 9 4ZM3 6v6c0 2 4 4 9 4s9-2 9-4V6M3 12v6c0 2 4 4 9 4s9-2 9-4v-6",
  info: "M12 11v6m0-10h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  external: "M14 3h7v7m0-7L10 14M10 3H3v18h18v-7",
  idea: "M9 18h6m-5 3h4M8 13a6 6 0 1 1 8 0c-2 1-2 3-2 3h-4s0-2-2-3Z",
  sparkles: "m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3ZM21 2v4m-2-2h4",
  close: "m6 6 12 12M6 18 18 6",
};
export function Icon({ name, size = 24 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
