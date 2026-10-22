from typing import Optional
from pydantic import BaseModel


class StreamEventSchema(BaseModel):
    event: str  # "step", "status", "completed", "failed"
    job_id: str
    step_number: Optional[int] = None
    step_name: Optional[str] = None
    description: Optional[str] = None
    insight_id: Optional[int] = None
    error: Optional[str] = None
    timestamp: str
