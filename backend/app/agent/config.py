"""Explicit API settings, independent of the backend's placeholder settings."""

import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class AgentConfig(BaseModel):
    """Per-run budgets; independent of provider and backend configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class ChatCompletionsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    api_url: str
    model: str
    auth_header: str | None = None
    auth_value: SecretStr | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    max_output_tokens: int = Field(default=1024, gt=0)
    token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

    @field_validator("api_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError("api_url must be an absolute HTTP(S) URL without credentials or fragment")
        # Accessing port also validates an invalid or out-of-range port.
        _ = parts.port
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_auth(self) -> "ChatCompletionsConfig":
        if (self.auth_header is None) != (self.auth_value is None):
            raise ValueError("auth_header and auth_value must be provided together")
        if self.auth_header is not None:
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.auth_header):
                raise ValueError("auth_header must be an HTTP header name")
            value = self.auth_value.get_secret_value()
            if not value.strip() or any(ord(char) < 32 or ord(char) > 126 for char in value):
                raise ValueError("auth_value must be a nonempty printable ASCII header value")
        return self

    @classmethod
    def from_env(cls) -> "ChatCompletionsConfig":
        """Read process environment only; never load .env or infer real API details."""
        fields = {
            "api_url": "AGENT_LLM_API_URL",
            "model": "AGENT_LLM_MODEL",
            "auth_header": "AGENT_LLM_AUTH_HEADER",
            "auth_value": "AGENT_LLM_AUTH_VALUE",
            "timeout_seconds": "AGENT_LLM_TIMEOUT_SECONDS",
            "max_output_tokens": "AGENT_LLM_MAX_OUTPUT_TOKENS",
            "token_limit_field": "AGENT_LLM_TOKEN_LIMIT_FIELD",
        }
        return cls(**{field: os.environ[name] for field, name in fields.items() if name in os.environ})
