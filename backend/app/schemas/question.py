from typing import Optional
from pydantic import BaseModel


class QuestionAskRequest(BaseModel):
    insight_id: int
    question: str


class QuestionAnswerResponse(BaseModel):
    insight_id: int
    question: str
    answer: str
    relevant_source: Optional[str] = None
