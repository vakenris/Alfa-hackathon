"""Companion store for retaining verified AgentAnalysisResult structures.

Allows deterministic, grounded Q&A and follow-up sensitivity analyses
without spending additional LLM tokens.
"""

from typing import Optional
from backend.app.agent.models import AgentAnalysisResult


class CompanionStore:
    def __init__(self):
        self._by_job: dict[str, AgentAnalysisResult] = {}
        self._by_insight: dict[int, AgentAnalysisResult] = {}
        self._by_pair: dict[tuple[int, int], AgentAnalysisResult] = {}

    def save(
        self,
        job_id: str,
        company_id: int,
        event_id: int,
        analysis: AgentAnalysisResult,
    ) -> None:
        self._by_job[job_id] = analysis
        self._by_pair[(company_id, event_id)] = analysis

    def link_insight(self, insight_id: int, job_id: str) -> None:
        if job_id in self._by_job:
            self._by_insight[insight_id] = self._by_job[job_id]

    def get_by_insight_id(self, insight_id: int) -> Optional[AgentAnalysisResult]:
        return self._by_insight.get(insight_id)

    def get_by_pair(self, company_id: int, event_id: int) -> Optional[AgentAnalysisResult]:
        return self._by_pair.get((company_id, event_id))

    def clear(self) -> None:
        self._by_job.clear(self._by_insight.clear() or {})
        self._by_pair.clear()


companion_store = CompanionStore()
