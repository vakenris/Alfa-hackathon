"""Shared bounded Chat Completions transport; no application startup dependencies."""

import asyncio
import os
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class ChatCompletionsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    api_url: str
    model: str
    auth_header: str | None = None
    auth_value: SecretStr | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    max_output_tokens: int = Field(default=1024, gt=0, le=16384)
    max_http_response_bytes: int = Field(default=256_000, ge=1024, le=2_000_000, strict=True)
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


class LLMAPIError(RuntimeError):
    """Base API failure; messages exclude prompts, credentials and response bodies."""


class LLMTimeoutError(LLMAPIError):
    def __init__(self, message: str, *, timeout_kind: str = "unknown_timeout"):
        self.timeout_kind = timeout_kind
        super().__init__(message)


class LLMTransportError(LLMAPIError):
    pass


class LLMHTTPError(LLMAPIError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"LLM API returned HTTP {status_code}")


class LLMResponseError(LLMAPIError):
    pass


class LLMOutputLimitError(LLMResponseError):
    pass


class ChatCompletionsClient:
    """One request per generate(); use a separate instance for each concurrent job.

    last_usage is the raw usage object of the latest call, including a truncated
    response. None means it was not available, not that zero tokens were consumed.
    last_finish_reason and last_content_chars are safe metadata only, captured
    before rejecting truncation. They never contain response or reasoning text.
    """

    def __init__(
        self,
        config: ChatCompletionsConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self._transport = transport
        self.last_usage: dict[str, Any] | None = None
        self.last_finish_reason: Literal["stop", "length", "other"] | None = None
        self.last_content_chars: int | None = None

    async def generate(self, prompt: str) -> str:
        self.last_usage = None
        self.last_finish_reason = None
        self.last_content_chars = None
        headers = {}
        if self.config.auth_header is not None:
            headers[self.config.auth_header] = self.config.auth_value.get_secret_value()
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            self.config.token_limit_field: self.config.max_output_tokens,
        }

        try:
            # The outer deadline also bounds slow responses that arrive in chunks.
            async with asyncio.timeout(self.config.timeout_seconds) as deadline:
                async with httpx.AsyncClient(
                    timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream("POST", self.config.api_url, headers=headers, json=payload) as response:
                        if not response.is_success:
                            raise LLMHTTPError(response.status_code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self.config.max_http_response_bytes:
                                raise LLMResponseError("LLM API response exceeds the byte limit")
                            body.extend(chunk)
                        # Parse a bounded body; a provider cannot force an unbounded
                        # read before the orchestrator's character limit is checked.
                        bounded_response = httpx.Response(response.status_code, content=bytes(body))
        except httpx.TimeoutException as error:
            # Classify only known local types; never use provider/exception text.
            timeout_kind = "http_timeout"
            for error_type, kind in (
                (httpx.ConnectTimeout, "http_connect"),
                (httpx.ReadTimeout, "http_read"),
                (httpx.WriteTimeout, "http_write"),
                (httpx.PoolTimeout, "http_pool"),
            ):
                if isinstance(error, error_type):
                    timeout_kind = kind
                    break
            raise LLMTimeoutError("LLM API request timed out", timeout_kind=timeout_kind) from None
        except TimeoutError:
            timeout_kind = "http_deadline" if deadline.expired() else "unknown_timeout"
            raise LLMTimeoutError("LLM API request timed out", timeout_kind=timeout_kind) from None
        except httpx.RequestError:
            raise LLMTransportError("LLM API transport failed") from None

        try:
            data = bounded_response.json()
        except ValueError:
            raise LLMResponseError("LLM API response is not JSON") from None
        if not isinstance(data, dict):
            raise LLMResponseError("LLM API response must be a JSON object")

        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise LLMResponseError("LLM API usage must be an object or null")
        self.last_usage = usage

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMResponseError("LLM API response has no completion choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        # Do not expose arbitrary provider strings through diagnostic reports.
        if isinstance(finish_reason, str):
            self.last_finish_reason = finish_reason if finish_reason in ("stop", "length") else "other"
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        self.last_content_chars = len(content) if isinstance(content, str) else None
        if finish_reason == "length":
            raise LLMOutputLimitError("LLM output reached the token limit")
        if finish_reason != "stop":
            raise LLMResponseError("LLM completion did not finish normally")
        if not isinstance(message, dict) or message.get("refusal"):
            raise LLMResponseError("LLM API response has no usable assistant message")
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("LLM API response has no nonempty text content")
        return content
