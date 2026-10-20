from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.services.company_service import CompanyService
from backend.app.schemas.company import CompanyResponse, CounterpartyGraphResponse

router = APIRouter(prefix="/companies", tags=["Companies"])


@router.get("", response_model=List[CompanyResponse])
async def list_companies(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Retrieve list of corporate companies."""
    service = CompanyService(db)
    return await service.list_companies(limit=limit, offset=offset)


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company_profile(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get single company profile with financial indicators and active bank products."""
    service = CompanyService(db)
    company = await service.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/{company_id}/counterparties", response_model=CounterpartyGraphResponse)
async def get_counterparties_graph(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Get counterparty network (top suppliers and customers) based on transaction data."""
    service = CompanyService(db)
    graph = await service.get_counterparties_graph(company_id)
    if not graph:
        raise HTTPException(status_code=404, detail="Company not found")
    return graph
