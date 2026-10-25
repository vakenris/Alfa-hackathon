"""Shared explicit LLM transport, routing and optional verified-result caching.

Importing this package performs no configuration loading, API request or backend
startup. A caller must explicitly construct and connect each dependency.
"""

from .client import (
    ChatCompletionsClient, ChatCompletionsConfig, LLMAPIError, LLMHTTPError,
    LLMOutputLimitError, LLMResponseError, LLMTimeoutError, LLMTransportError,
)
from .router import KNOWN_LIVE_MODEL_ID, ModelRouter
from .cache import VerifiedCacheKey, VerifiedJsonCache, make_verified_cache_key

__all__ = [
    "ChatCompletionsClient", "ChatCompletionsConfig", "LLMAPIError", "LLMHTTPError",
    "LLMOutputLimitError", "LLMResponseError", "LLMTimeoutError", "LLMTransportError",
    "KNOWN_LIVE_MODEL_ID", "ModelRouter", "VerifiedCacheKey", "VerifiedJsonCache",
    "make_verified_cache_key",
]
