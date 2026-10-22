"""A minimal non-streaming Chat Completions adapter, not a provider registry."""

import asyncio
from typing import Any

import httpx

from .config import ChatCompletionsConfig


class LLMAPIError(RuntimeError):
    """Base API failure; messages exclude prompts, credentials and response bodies."""


class LLMTimeoutError(LLMAPIError):
    pass


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

    async def generate(self, prompt: str) -> str:
        self.last_usage = None
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
            async with asyncio.timeout(self.config.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client:
                    response = await client.post(self.config.api_url, headers=headers, json=payload)
        except (httpx.TimeoutException, TimeoutError):
            raise LLMTimeoutError("LLM API request timed out") from None
        except httpx.RequestError:
            raise LLMTransportError("LLM API transport failed") from None

        if not response.is_success:
            raise LLMHTTPError(response.status_code)
        try:
            data = response.json()
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
        if finish_reason == "length":
            raise LLMOutputLimitError("LLM output reached the token limit")
        if finish_reason != "stop":
            raise LLMResponseError("LLM completion did not finish normally")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("refusal"):
            raise LLMResponseError("LLM API response has no usable assistant message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("LLM API response has no nonempty text content")
        return content
