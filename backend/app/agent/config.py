"""Explicit API settings, independent of the backend's placeholder settings."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.llm.client import ChatCompletionsConfig


class AgentConfig(BaseModel):
    """Per-run budgets; independent of provider and backend configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analysis_mode: Literal["grounded", "compatible"] = "grounded"
    max_steps: int = Field(default=4, ge=1, le=16, strict=True)
    max_llm_calls: int = Field(default=4, ge=1, le=16, strict=True)
    max_tool_calls: int = Field(default=3, ge=0, le=16, strict=True)
    tool_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    llm_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    max_context_chars: int = Field(default=24_000, ge=2048, strict=True)
    max_tool_result_chars: int = Field(default=8_000, ge=512, strict=True)
    max_response_chars: int = Field(default=24_000, ge=512, strict=True)
    max_article_chars: int = Field(default=4_000, ge=64, strict=True)
    max_counterparties: int = Field(default=50, ge=1, le=100, strict=True)
    max_prompt_chars: int = Field(default=64_000, ge=4096, strict=True)
    max_recovery_attempts: int = Field(default=1, ge=0, le=1, strict=True)
    trace_timeout_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
