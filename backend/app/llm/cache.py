"""Opt-in process-local cache for already verified final analysis JSON only.

This cache is not an authorization boundary or a second verifier. Only trusted
application code may supply verified results and keys. The caller must derive
tenant/user/company scope from its authorization context for both put and get.
No transport, agent or application lifecycle consults this cache automatically.
"""

import copy
import hashlib
import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .client import ChatCompletionsConfig


def _json_text(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Cache values must be finite JSON data.") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _scope_text(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= 512


@dataclass(frozen=True, slots=True)
class VerifiedCacheKey:
    """Opaque digest: no raw prompt, authorization value or company context."""

    digest: str
    company_id: int

    def __post_init__(self):
        if (type(self.digest) is not str or len(self.digest) != 64
                or any(char not in "0123456789abcdef" for char in self.digest)
                or type(self.company_id) is not int or self.company_id <= 0):
            raise ValueError("Invalid verified cache key.")


def make_verified_cache_key(
    *, tenant_id: str, user_id: str, company_id: int,
    config: ChatCompletionsConfig, prompt_version: str, prompt: str,
    company_context: object, sources_version: str,
) -> VerifiedCacheKey:
    """Hash complete scope and analysis inputs; never infer a source revision.

    ``sources_version`` must change when the visible source corpus/provenance
    changes. ``company_context`` must include the complete current business and
    payment context. Caller-supplied IDs alone do not authorize cache access.
    """
    if not all(_scope_text(item) for item in (tenant_id, user_id, prompt_version, sources_version)):
        raise ValueError("Explicit tenant, user, prompt and source versions are required.")
    if type(company_id) is not int or company_id <= 0:
        raise ValueError("A positive company identifier is required.")
    if not isinstance(config, ChatCompletionsConfig) or type(prompt) is not str or not prompt:
        raise ValueError("Validated generation configuration and exact prompt are required.")
    descriptor = {
        "version": 1, "tenant": tenant_id, "user": user_id, "company": company_id,
        "endpoint": config.api_url, "model": config.model,
        "generation": {
            "max_output_tokens": config.max_output_tokens,
            "token_limit_field": config.token_limit_field,
            "max_http_response_bytes": config.max_http_response_bytes,
            "timeout_seconds": config.timeout_seconds,
        },
        "prompt_version": prompt_version, "prompt_hash": _digest(prompt),
        "company_context_hash": _digest(company_context), "sources_version": sources_version,
    }
    return VerifiedCacheKey(digest=_digest(descriptor), company_id=company_id)


class VerifiedJsonCache:
    """Bounded TTL/LRU cache of detached final JSON with the run report removed.

    ``get`` does not return an AgentAnalysisResult: the original usage/report is
    deliberately omitted. The application supplies new cache-hit diagnostics.
    Stored verification is trusted application metadata, not proof of authenticity.
    This synchronous cache is intended for a single event-loop process; it does
    not coalesce concurrent generation or enforce a distributed budget.
    """

    def __init__(
        self, *, max_entries: int = 128, ttl_seconds: float = 300,
        max_payload_bytes: int = 256_000, clock: Callable[[], float] = time.monotonic,
    ):
        if type(max_entries) is not int or not 1 <= max_entries <= 4096:
            raise ValueError("max_entries must be between one and 4096.")
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= 3600):
            raise ValueError("ttl_seconds must be finite, positive and at most 3600.")
        if type(max_payload_bytes) is not int or not 1024 <= max_payload_bytes <= 4_000_000:
            raise ValueError("max_payload_bytes must be between 1024 and 4000000.")
        if not callable(clock):
            raise ValueError("A monotonic clock callable is required.")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_payload_bytes = max_payload_bytes
        self._clock = clock
        self._entries: OrderedDict[VerifiedCacheKey, tuple[float, dict]] = OrderedDict()

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("The cache clock must return a finite number.")
        return value

    def _expire(self, now: float) -> None:
        for key in [key for key, (deadline, _) in self._entries.items() if deadline <= now]:
            del self._entries[key]

    def put_verified(self, key: VerifiedCacheKey, payload: dict) -> None:
        """Accept a passing full companion JSON and discard its original report."""
        if not isinstance(key, VerifiedCacheKey) or type(payload) is not dict:
            raise ValueError("A scoped key and verified final companion JSON are required.")
        verification = payload.get("verification")
        required = {"company_id", "event_id", "insight", "analysis", "business_context",
                    "sources", "verification", "report"}
        if (type(verification) is not dict or verification.get("passed") is not True
                or verification.get("issues") != []
                or not required <= payload.keys()
                or type(payload.get("company_id")) is not int
                or payload["company_id"] != key.company_id
                or type(payload.get("event_id")) is not int
                or type(payload.get("insight")) is not dict
                or type(payload.get("analysis")) is not dict
                or type(payload.get("business_context")) is not dict
                or type(payload.get("sources")) is not list
                or type(payload.get("report")) is not dict
                or any(field in payload for field in ("error", "exception", "error_message"))
                or payload.get("status") in ("error", "failed", "cancelled")):
            raise ValueError("Only passing verified final analysis JSON may be cached.")
        analysis = payload["analysis"]
        if (analysis.get("action") != "draft"
                or analysis.get("applicability") not in ("conditional", "insufficient")
                or type(analysis.get("facts")) is not list or not analysis["facts"]):
            raise ValueError("A final grounded analysis is required.")
        # Serialization both validates finite JSON and breaks references to the
        # caller. No original provider usage is retained or replayed on a hit.
        detached = {field: value for field, value in payload.items() if field != "report"}
        serialized = _json_text(detached)
        if len(serialized.encode("utf-8")) > self.max_payload_bytes:
            raise ValueError("Verified cache payload exceeds its byte limit.")
        detached = json.loads(serialized)
        now = self._now()
        self._expire(now)
        self._entries[key] = (now + self.ttl_seconds, detached)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def get(self, key: VerifiedCacheKey) -> dict | None:
        """Return an isolated verified payload sans report, or a cache miss."""
        if not isinstance(key, VerifiedCacheKey):
            raise ValueError("A scoped verified cache key is required.")
        self._expire(self._now())
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return copy.deepcopy(entry[1])

    def clear(self) -> None:
        self._entries.clear()
