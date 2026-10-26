"""Explicit local bounds. The shared client owns transport/output limits."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.app.llm.client import ChatCompletionsConfig


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    max_llm_calls: int = Field(default=2, ge=1, le=2, strict=True)
    max_recovery_attempts: int = Field(default=1, ge=0, le=1, strict=True)
    llm_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    source_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    trace_timeout_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    max_input_counterparties: int = Field(default=1000, ge=1, le=10000, strict=True)
    max_bank_documents: int = Field(default=3, ge=0, le=5, strict=True)
    max_article_chars: int = Field(default=1500, ge=200, le=6000, strict=True)
    max_context_chars: int = Field(default=16000, ge=2000, le=32000, strict=True)
    max_prompt_chars: int = Field(default=24000, ge=4000, le=48000, strict=True)
    max_response_chars: int = Field(default=12000, ge=1000, le=24000, strict=True)
