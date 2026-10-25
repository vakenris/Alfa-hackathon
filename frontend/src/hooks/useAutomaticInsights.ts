import { useEffect, useState } from "react";
import { errorMessage, request } from "../api";
import type { Insight } from "../api";

export interface AutomaticAnalysis {
  insights: Insight[];
  jobs: {event_id: number; status: string; error_message: string | null}[];
  loading: boolean;
  error: string;
}
const empty: AutomaticAnalysis = {insights: [], jobs: [], loading: true, error: ""};
export function useAutomaticInsights(companyId: number | undefined, feedKey: string, role: string): AutomaticAnalysis {
  const key = `${companyId}:${role}:${feedKey}`;
  const [snapshot, setSnapshot] = useState<{key: string; data: AutomaticAnalysis}>({key: "", data: empty});
  useEffect(() => {
    if (!companyId || !feedKey) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await request<{items: Insight[]; jobs: AutomaticAnalysis["jobs"]}>(`/companies/${companyId}/analytics/automatic-insights`, role, {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)])});
        if (controller.signal.aborted) return;
        setSnapshot({key, data: {insights: result.items, jobs: result.jobs, loading: false, error: ""}});
        const pending = result.jobs.some(job => job.status === "PENDING" || job.status === "PROCESSING");
        timer = setTimeout(() => void poll(), pending ? 2000 : 15000);
      } catch (error) {
        if (controller.signal.aborted) return;
        setSnapshot(current => ({key, data: {...(current.key === key ? current.data : empty), loading: false, error: errorMessage(error)}}));
        timer = setTimeout(() => void poll(), 10000);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [companyId, feedKey, role, key]);
  return snapshot.key === key ? snapshot.data : empty;
}
