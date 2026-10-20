from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class CompanyBase(BaseModel):
    synthetic_u: str
    name: str
    org_type: Optional[str] = None
    okfs_name: Optional[str] = None
    company_age_years: Optional[str] = None
    eco_mark_label: Optional[str] = None
    mainokved_code: Optional[str] = None
    mainokved_name: Optional[str] = None
    negative_mark: bool = False
    amount_workers_fns: Optional[str] = None
    amount_workers_spark: Optional[str] = None
    cl_type: Optional[str] = None
    
    cl_product_rko_mark: bool = False
    cl_product_gar_mark: bool = False
    cl_product_loa_mark: bool = False
    cl_product_lea_mark: bool = False
    cl_product_dep_mark: bool = False
    cl_product_zpp_mark: bool = False
    factoring_mark: bool = False
    
    fts_flag: bool = False
    fts_country_name: Optional[str] = None
    revenue_company: Optional[float] = None
    group_id: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None


class CompanyResponse(CompanyBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class CounterpartyItem(BaseModel):
    synthetic_u: str
    name: Optional[str] = None
    direction: str  # "PAYER" (customer) or "BENEFICIARY" (supplier)
    total_amount: float
    transaction_count: int


class CounterpartyGraphResponse(BaseModel):
    company_synthetic_u: str
    top_counterparties: List[CounterpartyItem]
