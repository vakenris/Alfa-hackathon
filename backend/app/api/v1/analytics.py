"""Analytics API — aggregated company-level analytics and insight statistics.

Endpoints:
    GET /companies/{company_id}/analytics/summary
        — Snapshot: insight count, categories, active products, group metrics.

    GET /companies/{company_id}/analytics/insights-history
        — List of all insights generated for this company (lightweight).

    GET /companies/{company_id}/analytics/counterparty-exposure
        — Aggregated payment flow summary for the company's counterparty network.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.company import Company
from backend.app.db.models.transaction import CounterpartyTransaction
from backend.app.db.models.insight import Insight
from backend.app.db.models.news import NewsArticle
from backend.app.db.repositories.company_repo import CompanyRepository

router = APIRouter(prefix="/companies", tags=["Analytics"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class ActiveProductsSnapshot(BaseModel):
    rko: bool
    guarantees: bool
    loans: bool
    leasing: bool
    deposits: bool
    salary_project: bool
    factoring: bool

    model_config = ConfigDict(from_attributes=True)


class InsightHistoryItem(BaseModel):
    insight_id: int
    event_id: int
    what_happened: str
    recommended_action: str
    relevant_bank_product: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CompanyAnalyticsSummary(BaseModel):
    company_id: int
    company_name: str
    group_id: Optional[str]
    total_insights: int
    insights_last_7_days: int
    top_news_categories: List[str]
    active_products: ActiveProductsSnapshot
    fts_active: bool
    fts_countries: Optional[str]
    revenue_rub: Optional[float]

    model_config = ConfigDict(from_attributes=True)


class CounterpartyExposureItem(BaseModel):
    counterparty_uid: str
    direction: str          # "outgoing" | "incoming"
    total_amount_rub: float
    operations_count: int


class CounterpartyExposureSummary(BaseModel):
    company_id: int
    total_outgoing_rub: float
    total_incoming_rub: float
    net_flow_rub: float     # incoming - outgoing
    counterparties: List[CounterpartyExposureItem]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{company_id}/analytics/summary", response_model=CompanyAnalyticsSummary)
async def get_company_analytics_summary(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    """Aggregated analytics snapshot for a company — insights count, active products, revenue."""
    # Fetch company
    company_repo = CompanyRepository(db)
    company = await company_repo.get_by_id(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Count insights
    total_q = await db.execute(
        select(func.count(Insight.id)).where(Insight.company_id == company_id)
    )
    total_insights: int = total_q.scalar_one()

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recent_q = await db.execute(
        select(func.count(Insight.id)).where(
            Insight.company_id == company_id,
            Insight.created_at >= cutoff,
        )
    )
    insights_last_7_days: int = recent_q.scalar_one()

    # Top news categories (from insight-linked events)
    cats_q = await db.execute(
        select(NewsArticle.category, func.count(NewsArticle.id).label("cnt"))
        .join(Insight, Insight.event_id == NewsArticle.id)
        .where(Insight.company_id == company_id)
        .group_by(NewsArticle.category)
        .order_by(func.count(NewsArticle.id).desc())
        .limit(5)
    )
    top_news_categories = [row[0] for row in cats_q.fetchall() if row[0]]

    active_products = ActiveProductsSnapshot(
        rko=bool(company.cl_product_rko_mark),
        guarantees=bool(company.cl_product_gar_mark),
        loans=bool(company.cl_product_loa_mark),
        leasing=bool(company.cl_product_lea_mark),
        deposits=bool(company.cl_product_dep_mark),
        salary_project=bool(company.cl_product_zpp_mark),
        factoring=bool(company.factoring_mark),
    )

    return CompanyAnalyticsSummary(
        company_id=company_id,
        company_name=company.name,
        group_id=company.group_id,
        total_insights=total_insights,
        insights_last_7_days=insights_last_7_days,
        top_news_categories=top_news_categories,
        active_products=active_products,
        fts_active=bool(company.fts_flag),
        fts_countries=company.fts_country_name,
        revenue_rub=company.revenue_company,
    )


@router.get("/{company_id}/analytics/insights-history", response_model=List[InsightHistoryItem])
async def get_insights_history(
    company_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    """Lightweight chronological list of all generated insights for a company."""
    company_repo = CompanyRepository(db)
    company = await company_repo.get_by_id(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    result = await db.execute(
        select(Insight)
        .where(Insight.company_id == company_id)
        .order_by(Insight.created_at.desc())
        .limit(limit)
    )
    insights = result.scalars().all()

    return [
        InsightHistoryItem(
            insight_id=ins.id,
            event_id=ins.event_id,
            what_happened=ins.what_happened[:200],   # truncated preview
            recommended_action=ins.recommended_action,
            relevant_bank_product=ins.relevant_bank_product,
            created_at=ins.created_at,
        )
        for ins in insights
    ]


@router.get("/{company_id}/analytics/counterparty-exposure", response_model=CounterpartyExposureSummary)
async def get_counterparty_exposure(
    company_id: int,
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role),
):
    """Aggregated incoming/outgoing payment flows for the company's counterparty network."""
    company_repo = CompanyRepository(db)
    company = await company_repo.get_by_id(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    uid = company.synthetic_u

    # Outgoing payments (company is payer)
    out_q = await db.execute(
        select(
            CounterpartyTransaction.beneficiary_u,
            func.sum(CounterpartyTransaction.pay_amt).label("total"),
            func.sum(CounterpartyTransaction.pay_cnt).label("cnt"),
        )
        .where(CounterpartyTransaction.payer_u == uid)
        .group_by(CounterpartyTransaction.beneficiary_u)
        .order_by(func.sum(CounterpartyTransaction.pay_amt).desc())
        .limit(limit)
    )
    outgoing_rows = out_q.fetchall()

    # Incoming payments (company is beneficiary)
    in_q = await db.execute(
        select(
            CounterpartyTransaction.payer_u,
            func.sum(CounterpartyTransaction.pay_amt).label("total"),
            func.sum(CounterpartyTransaction.pay_cnt).label("cnt"),
        )
        .where(CounterpartyTransaction.beneficiary_u == uid)
        .group_by(CounterpartyTransaction.payer_u)
        .order_by(func.sum(CounterpartyTransaction.pay_amt).desc())
        .limit(limit)
    )
    incoming_rows = in_q.fetchall()

    total_outgoing = sum(r[1] or 0.0 for r in outgoing_rows)
    total_incoming = sum(r[1] or 0.0 for r in incoming_rows)

    counterparties: List[CounterpartyExposureItem] = []
    for uid_cp, total, cnt in outgoing_rows:
        counterparties.append(CounterpartyExposureItem(
            counterparty_uid=uid_cp,
            direction="outgoing",
            total_amount_rub=total or 0.0,
            operations_count=int(cnt or 0),
        ))
    for uid_cp, total, cnt in incoming_rows:
        counterparties.append(CounterpartyExposureItem(
            counterparty_uid=uid_cp,
            direction="incoming",
            total_amount_rub=total or 0.0,
            operations_count=int(cnt or 0),
        ))

    return CounterpartyExposureSummary(
        company_id=company_id,
        total_outgoing_rub=total_outgoing,
        total_incoming_rub=total_incoming,
        net_flow_rub=total_incoming - total_outgoing,
        counterparties=counterparties,
    )
