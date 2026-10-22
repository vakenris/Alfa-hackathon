from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict


class AgentTraceStepSchema(BaseModel):
    step_number: int
    step_name: str
    description: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    company_id: int
    event_id: int
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    trace_steps: List[AgentTraceStepSchema] = []
    insight_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)
