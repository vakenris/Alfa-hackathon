from typing import Dict, List, Optional
from pydantic import BaseModel


class SystemMetricsResponse(BaseModel):
    status: str
    uptime_seconds: float
    total_companies: int
    total_news: int
    total_insights_generated: int
    active_jobs: int


class BudgetMetricsResponse(BaseModel):
    daily_budget_rub: float
    spent_budget_rub: float
    remaining_budget_rub: float
    total_tokens_used: int
    budget_exhausted: bool


class AgentQualityMetricsResponse(BaseModel):
    average_agent_steps: float
    average_latency_seconds: float
    success_rate_percentage: float
    total_evaluations: int
    quality_score_overall: float  # e.g. 4.8 / 5.0
