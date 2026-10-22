from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.company import Company
from backend.app.db.models.transaction import CounterpartyTransaction
from backend.app.schemas.company import CompanyResponse, CounterpartyGraphResponse, CounterpartyItem
from backend.app.core.security import mask_financial_amount

router = APIRouter(prefix="/companies", tags=["Companies"])


@router.get("", response_model=List[CompanyResponse])
async def list_companies(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Retrieve list of corporate companies (> 15 billion RUB)."""
    stmt = select(Company).order_by(Company.id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    companies = result.scalars().all()
    
    responses: List[CompanyResponse] = []
    for comp in companies:
        resp = CompanyResponse.model_validate(comp)
        resp.revenue_company = mask_financial_amount(resp.revenue_company, role)
        responses.append(resp)
    return responses


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company_profile(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get single company profile with financial indicators and active bank products."""
    stmt = select(Company).where(Company.id == company_id)
    result = await db.execute(stmt)
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=404, detail="Company not found")
    
    resp = CompanyResponse.model_validate(comp)
    resp.revenue_company = mask_financial_amount(resp.revenue_company, role)
    return resp


@router.get("/{company_id}/counterparties", response_model=CounterpartyGraphResponse)
async def get_counterparties_graph(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get counterparty network (top suppliers and customers) based on payment transactions."""
    stmt = select(Company).where(Company.id == company_id)
    result = await db.execute(stmt)
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(status_code=404, detail="Company not found")

    items: List[CounterpartyItem] = []

    # Outgoing: company -> suppliers
    stmt_out = select(
        CounterpartyTransaction.beneficiary_u,
        CounterpartyTransaction.pay_amt,
        CounterpartyTransaction.pay_cnt
    ).where(CounterpartyTransaction.payer_u == comp.synthetic_u).limit(10)
    res_out = await db.execute(stmt_out)
    for row in res_out:
        items.append(CounterpartyItem(
            synthetic_u=row[0],
            direction="SUPPLIER",
            total_amount=mask_financial_amount(row[1], role) or 0.0,
            transaction_count=row[2]
        ))

    # Incoming: customers -> company
    stmt_in = select(
        CounterpartyTransaction.payer_u,
        CounterpartyTransaction.pay_amt,
        CounterpartyTransaction.pay_cnt
    ).where(CounterpartyTransaction.beneficiary_u == comp.synthetic_u).limit(10)
    res_in = await db.execute(stmt_in)
    for row in res_in:
        items.append(CounterpartyItem(
            synthetic_u=row[0],
            direction="CUSTOMER",
            total_amount=mask_financial_amount(row[1], role) or 0.0,
            transaction_count=row[2]
        ))

    return CounterpartyGraphResponse(
        company_synthetic_u=comp.synthetic_u,
        top_counterparties=items
    )
