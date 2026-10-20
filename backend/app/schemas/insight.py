from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict


class EvidenceItem(BaseModel):
    claim: str
    source_title: str
    source_url: Optional[str] = None
    company_feature: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class InsightCreateRequest(BaseModel):
    company_id: int
    event_id: int


class InsightResponse(BaseModel):
    id: int
    job_id: str
    company_id: int
    event_id: int
    what_happened: str
    why_important: str
    consequences: str
    bank_view: str
    ai_view: str
    recommended_action: str
    relevant_bank_product: Optional[str] = None
    created_at: datetime
    evidences: List[EvidenceItem] = []

    model_config = ConfigDict(from_attributes=True)
