"""Metrics API — operational and product quality metrics for the insight service.

These endpoints serve two audiences:
- **Business / product team** — insight pipeline success rate, avg latency, role breakdown.
- **Monitoring / ops** — jobs throughput, error rate.

Endpoints:
    GET /metrics/pipeline   — Job success/fail/pending counts + avg latency.
    GET /metrics/insights   — Total insights, breakdown by day (last N days).
    GET /metrics/audit      — Recent audit log entries; supports role and path filters.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.core.config import settings
from backend.app.db.models.audit import AuditLog
from backend.app.db.models.company import Company
from backend.app.db.models.news import NewsArticle
from backend.app.db.models.insight import Insight
from backend.app.db.models.agent_trace import JobRecord
from backend.app.schemas.metrics import SystemMetricsResponse, BudgetMetricsResponse
from backend.app.services.budget_service import get_budget_store

SERVER_START_TIME = time.time()
_spent_budget_rub = 14.50
_total_tokens_used = 18450

router = APIRouter(prefix="/metrics", tags=["Metrics"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class PipelineMetrics(BaseModel):
    total_jobs: int
    pending: int
    processing: int
    success: int
    failed: int
    success_rate_pct: float         # 0–100
    avg_latency_ms: Optional[float] # среднее время ответа аудит-middleware


class InsightDayStat(BaseModel):
    date: str           # YYYY-MM-DD
    count: int


class InsightMetrics(BaseModel):
    total_insights: int
    days: List[InsightDayStat]


class AuditEntry(BaseModel):
    id: int
    user_role: str
    method: str
    path: str
    status_code: int
    duration_ms: Optional[float]
    created_at: datetime


class AuditMetrics(BaseModel):
    total_records: int
    entries: List[AuditEntry]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/pipeline", response_model=PipelineMetrics)
async def get_pipeline_metrics(
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    """Job pipeline health: counters for each status and overall success rate."""
    counts_q = await db.execute(
        select(JobRecord.status, func.count(JobRecord.id).label("cnt"))
        .group_by(JobRecord.status)
    )
    counts = {row[0]: row[1] for row in counts_q.fetchall()}

    pending = counts.get("PENDING", 0)
    processing = counts.get("PROCESSING", 0)
    success = counts.get("SUCCESS", 0)
    failed = counts.get("FAILED", 0)
    total = pending + processing + success + failed
    success_rate = round(success / total * 100, 1) if total > 0 else 0.0

    # Average API latency from audit log (last 24h, exclude health pings)
    latency_q = await db.execute(
        select(func.avg(AuditLog.duration_ms))
        .where(
            AuditLog.created_at >= datetime.now(timezone.utc) - timedelta(hours=24),
            AuditLog.path.not_in(["/api/v1/health/live", "/api/v1/health/ready"]),
            AuditLog.duration_ms.is_not(None),
        )
    )
    avg_latency: Optional[float] = latency_q.scalar_one_or_none()
    if avg_latency is not None:
        avg_latency = round(avg_latency, 2)

    return PipelineMetrics(
        total_jobs=total,
        pending=pending,
        processing=processing,
        success=success,
        failed=failed,
        success_rate_pct=success_rate,
        avg_latency_ms=avg_latency,
    )


@router.get("/insights", response_model=InsightMetrics)
async def get_insight_metrics(
    days: int = Query(default=7, ge=1, le=30, description="Number of past days to aggregate"),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    """Total insight count and daily breakdown for the last N days."""
    # Total
    total_q = await db.execute(select(func.count(Insight.id)))
    total_insights: int = total_q.scalar_one()

    # Per-day counts
    day_stats: List[InsightDayStat] = []
    for delta in range(days - 1, -1, -1):
        day_start = (datetime.now(timezone.utc) - timedelta(days=delta)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + timedelta(days=1)
        cnt_q = await db.execute(
            select(func.count(Insight.id)).where(
                Insight.created_at >= day_start,
                Insight.created_at < day_end,
            )
        )
        day_stats.append(InsightDayStat(
            date=day_start.strftime("%Y-%m-%d"),
            count=cnt_q.scalar_one(),
        ))

    return InsightMetrics(total_insights=total_insights, days=day_stats)


@router.get("/audit", response_model=AuditMetrics)
async def get_audit_metrics(
    limit: int = Query(default=50, ge=1, le=200),
    role_filter: Optional[str] = Query(default=None, alias="role"),
    path_filter: Optional[str] = Query(default=None, alias="path"),
    db: AsyncSession = Depends(get_db),
    caller_role: UserRole = Depends(get_current_user_role),
):
    """Recent audit log entries. Supports optional role and path filters.

    Access note: in a production deployment this endpoint would be restricted
    to TREASURER / ANALYST roles via RBAC; here it is open for demo purposes.
    """
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if role_filter:
        stmt = stmt.where(AuditLog.user_role == role_filter.upper())
    if path_filter:
        stmt = stmt.where(AuditLog.path.contains(path_filter))

    result = await db.execute(stmt)
    rows = result.scalars().all()

    # Total count (unfiltered)
    total_q = await db.execute(select(func.count(AuditLog.id)))
    total_records: int = total_q.scalar_one()

    entries = [
        AuditEntry(
            id=row.id,
            user_role=row.user_role,
            method=row.method,
            path=row.path,
            status_code=row.status_code,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return AuditMetrics(total_records=total_records, entries=entries)


@router.get("/system", response_model=SystemMetricsResponse)
async def get_system_metrics(
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Overall system health and entity counters."""
    comp_count = await db.scalar(select(func.count()).select_from(Company)) or 0
    news_count = await db.scalar(select(func.count()).select_from(NewsArticle)) or 0
    insight_count = await db.scalar(select(func.count()).select_from(Insight)) or 0
    active_jobs = await db.scalar(
        select(func.count()).select_from(JobRecord).where(JobRecord.status.in_(["PENDING", "PROCESSING"]))
    ) or 0

    return SystemMetricsResponse(
        status="HEALTHY",
        uptime_seconds=round(time.time() - SERVER_START_TIME, 1),
        total_companies=comp_count,
        total_news=news_count,
        total_insights_generated=insight_count,
        active_jobs=active_jobs
    )


@router.get("/budget", response_model=BudgetMetricsResponse)
async def get_budget_metrics(
    role: UserRole = Depends(get_current_user_role)
):
    """Budget Guardian monitoring (Slide 12: 500 RUB daily token limit)."""
    return get_budget_store().get_metrics()
