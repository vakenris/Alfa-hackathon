import type { NewsItem } from "./data/dashboard";
export const roles = ["Финансовый директор", "Генеральный директор", "Бизнес-аналитик"];
const roleCodes: Record<string, string> = {"Финансовый директор":"CFO", "Генеральный директор":"CEO", "Бизнес-аналитик":"ANALYST"};
export const apiBase = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");
export async function request<T>(path: string, role: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {...init, headers: {"Content-Type":"application/json", "X-User-Role": roleCodes[role] || "CEO", ...init.headers}, signal: init.signal ?? AbortSignal.timeout(120000)});
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : `Ошибка API: ${response.status}`);
  }
  return response.json();
}
export function errorMessage(error: unknown) { return error instanceof Error ? error.message : "Не удалось загрузить данные"; }
export interface Company { id: number; name: string; mainokved_name: string | null; revenue_company: number | null; city: string | null }
export interface Article { id: number; title: string; category: string; source: string; published_at: string; summary: string | null; content: string; is_bank_analytics: boolean }
export type LiveNews = NewsItem & { source: string; content: string; isBankAnalytics: boolean };
export function mapArticle(a: Article): LiveNews {
  return { id: String(a.id), title: a.title, category: a.category, time: new Date(a.published_at).toLocaleString("ru-RU"), description: a.summary || a.content, takeaway: a.summary || a.content, source: a.source, content: a.content, isBankAnalytics: a.is_bank_analytics, image: a.category === "Макроэкономика" ? "/images/bank.png" : "/images/industry.png", imageAlt: "Иллюстрация к новости", impact: "medium", effects: [] };
}
export interface Insight { id: number; event_id: number; what_happened: string; why_important: string; consequences: string; bank_view: string; ai_view: string; recommended_action: string; relevant_bank_product: string | null; evidences: {claim: string; source_title: string; source_url: string | null; company_feature: string | null}[] }
export interface HistoryItem { insight_id: number; event_id: number; what_happened: string; recommended_action: string; relevant_bank_product: string | null; created_at: string }
export interface Job { status: string; insight_id: number | null; error_message: string | null; trace_steps: {step_number: number; step_name: string; description: string}[] }
export interface Summary { total_insights: number; insights_last_7_days: number; revenue_rub: number | null; active_products: Record<string, boolean>; fts_countries: string | null }
export interface Exposure { total_outgoing_rub: number; total_incoming_rub: number; net_flow_rub: number }
