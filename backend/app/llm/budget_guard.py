"""Fail-closed billing boundary with an injected atomic, shared ledger.

No database, Redis connection, environment lookup or per-process ledger exists
here. Backend must implement BudgetStore across ALL workers/callers, supply
confirmed RUB rates, provider token bounds and the confirmed budget window.
"""

import asyncio
import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

import httpx

from .client import ChatCompletionsClient, ChatCompletionsConfig


class BudgetError(RuntimeError):
    """Safe messages only; no provider text, prompt, credentials or store URL."""


class BudgetExceededError(BudgetError):
    pass


class BudgetConfigurationError(BudgetError):
    pass


class BudgetStoreError(BudgetError):
    pass


def _tokens(value):
    return value if type(value) is int and value >= 0 else None


def _money(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise BudgetConfigurationError("Rates and amounts require finite nonnegative Decimal RUB values")
    return value


@dataclass(frozen=True)
class TokenUsage:
    """completion_tokens includes reasoning, following the gateway usage contract."""

    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None = None

    def __post_init__(self):
        for value in (self.prompt_tokens, self.completion_tokens, self.reasoning_tokens):
            if value is not None and _tokens(value) is None:
                raise BudgetConfigurationError("Token counts must be nonnegative integers or unknown")
        if (self.reasoning_tokens is not None and self.completion_tokens is not None
                and self.reasoning_tokens > self.completion_tokens):
            raise BudgetConfigurationError("Reasoning must be a subset of completion tokens")

    @property
    def complete(self):
        return self.prompt_tokens is not None and self.completion_tokens is not None

    @classmethod
    def from_provider(cls, raw):
        if type(raw) is not dict:
            return cls(None, None)
        prompt, completion = _tokens(raw.get("prompt_tokens")), _tokens(raw.get("completion_tokens"))
        details = raw.get("completion_tokens_details")
        reasoning = _tokens(details.get("reasoning_tokens")) if type(details) is dict else None
        total = _tokens(raw.get("total_tokens"))
        if (reasoning is not None and completion is not None and reasoning > completion
                or total is not None and prompt is not None and completion is not None
                and total != prompt + completion):
            # Inconsistent metadata cannot settle a reservation as a lower bill.
            return cls(None, None)
        return cls(prompt, completion, reasoning)


@dataclass(frozen=True)
class RateCard:
    model: str
    input_rub_per_million: Decimal
    output_rub_per_million: Decimal
    reference: str
    reasoning_rub_per_million: Decimal | None = None

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip() or not isinstance(self.reference, str) or not self.reference.strip():
            raise BudgetConfigurationError("A confirmed model and tariff reference are required")
        for value in (self.input_rub_per_million, self.output_rub_per_million):
            _money(value)
        if self.reasoning_rub_per_million is not None:
            _money(self.reasoning_rub_per_million)

    def cost(self, usage: TokenUsage) -> Decimal | None:
        if not usage.complete:
            return None
        output_cost = usage.completion_tokens * self.output_rub_per_million
        if self.reasoning_rub_per_million is not None:
            if usage.reasoning_tokens is None:
                return None
            # Replace the reasoning subset's rate; do not bill those tokens twice.
            output_cost += usage.reasoning_tokens * (self.reasoning_rub_per_million - self.output_rub_per_million)
        return (usage.prompt_tokens * self.input_rub_per_million + output_cost) / Decimal(1_000_000)

    def reserve_cost(self, max_input_tokens: int, max_completion_tokens: int) -> Decimal:
        if any(_tokens(value) is None for value in (max_input_tokens, max_completion_tokens)):
            raise BudgetConfigurationError("Reservation requires explicit nonnegative token bounds")
        output_rate = max(self.output_rub_per_million, self.reasoning_rub_per_million or Decimal(0))
        return (max_input_tokens * self.input_rub_per_million + max_completion_tokens * output_rate) / Decimal(1_000_000)


@dataclass(frozen=True)
class BudgetWindow:
    """Explicit organizer-confirmed accounting boundaries; no inferred reset rule."""
    id: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip() or len(self.id) > 128:
            raise BudgetConfigurationError("An explicit budget window ID is required")
        if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
               for value in (self.starts_at, self.ends_at)) or self.starts_at >= self.ends_at:
            raise BudgetConfigurationError("Budget windows require ordered timezone-aware boundaries")


@dataclass(frozen=True)
class BudgetReservation:
    id: str
    team_id: str
    window_id: str
    amount_rub: Decimal
    rate: RateCard


class BudgetStore(Protocol):
    """Required durable, atomic implementation shared by every application worker.

    reserve: atomically enforce spent + ALL outstanding reservations + amount <=
    limit for team/window. Same ID is idempotent only for identical arguments.
    Reject inconsistent window boundaries/limits for an existing team/window ID.
    settle: replace reservation with known actual cost atomically/idempotently;
    actual > reserved must still be recorded (and block subsequent reservations).
    None marks pending reconciliation and RETAINS the entire reserved amount.
    Unknown reservations must NOT expire/release automatically at window reset.
    Settlement of unknown IDs or a different repeated known bill must fail.
    A finalized settlement must never be reverted to pending by a late None.
    """
    async def reserve(self, *, team_id: str, window: BudgetWindow, limit_rub: Decimal,
                      reservation_id: str, amount_rub: Decimal) -> bool: ...

    async def settle(self, *, team_id: str, window_id: str, reservation_id: str,
                     actual_rub: Decimal | None) -> None: ...


class BudgetGuard:
    def __init__(self, *, store: BudgetStore, team_id: str,
                 window_provider: Callable[[], BudgetWindow], rate_cards: Mapping[str, RateCard],
                 daily_limit_rub: Decimal = Decimal("500"), clock: Callable[[], datetime] | None = None,
                 store_timeout_seconds: float = 5):
        if not isinstance(team_id, str) or not team_id.strip() or not callable(window_provider):
            raise BudgetConfigurationError("Explicit team and accounting window provider are required")
        if any(not inspect.iscoroutinefunction(getattr(store, method, None)) for method in ("reserve", "settle")):
            raise BudgetConfigurationError("An explicit asynchronous shared budget store is required")
        if not isinstance(rate_cards, Mapping) or any(not isinstance(card, RateCard) or key != card.model for key, card in rate_cards.items()):
            raise BudgetConfigurationError("Rate cards must match exact model IDs")
        if not Decimal(0) < _money(daily_limit_rub) <= Decimal("500"):
            raise BudgetConfigurationError("Team daily limit must be positive and at most 500 RUB")
        if (type(store_timeout_seconds) not in (int, float) or not math.isfinite(store_timeout_seconds)
                or not 0 < store_timeout_seconds <= 60):
            raise BudgetConfigurationError("Budget store deadline must be finite, positive and at most 60 seconds")
        self.store, self.team_id = store, team_id
        self.window_provider, self.rate_cards = window_provider, dict(rate_cards)
        self.daily_limit_rub = daily_limit_rub
        self.store_timeout_seconds = store_timeout_seconds
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    async def reserve(self, model: str, *, max_input_tokens: int, max_completion_tokens: int) -> BudgetReservation:
        rate = self.rate_cards.get(model)
        if rate is None:
            raise BudgetConfigurationError("No confirmed RUB tariff for selected model")
        try:
            window, now = self.window_provider(), self.clock()
            valid = isinstance(window, BudgetWindow) and now.tzinfo is not None and window.starts_at <= now < window.ends_at
        except Exception:
            raise BudgetConfigurationError("Budget window unavailable or invalid") from None
        if not valid:
            raise BudgetConfigurationError("No active confirmed budget window")
        ticket = BudgetReservation(uuid4().hex, self.team_id, window.id, rate.reserve_cost(max_input_tokens, max_completion_tokens), rate)
        try:
            async with asyncio.timeout(self.store_timeout_seconds):
                allowed = await self.store.reserve(team_id=self.team_id, window=window, limit_rub=self.daily_limit_rub,
                                                   reservation_id=ticket.id, amount_rub=ticket.amount_rub)
        except Exception:
            raise BudgetStoreError("Shared budget reservation unavailable") from None
        if allowed is not True:
            if allowed is not False:
                raise BudgetStoreError("Invalid shared budget store response")
            raise BudgetExceededError("Team budget cannot cover this reservation")
        return ticket

    async def settle(self, reservation: BudgetReservation, usage: TokenUsage | None) -> None:
        if not isinstance(reservation, BudgetReservation) or reservation.team_id != self.team_id:
            raise BudgetConfigurationError("Reservation belongs to another team")
        if usage is not None and not isinstance(usage, TokenUsage):
            raise BudgetConfigurationError("Settlement requires normalized token usage")
        actual = reservation.rate.cost(usage) if usage is not None else None
        try:
            async with asyncio.timeout(self.store_timeout_seconds):
                await self.store.settle(team_id=self.team_id, window_id=reservation.window_id,
                                        reservation_id=reservation.id, actual_rub=actual)
        except Exception:
            raise BudgetStoreError("Shared budget settlement unavailable; reservation must remain held") from None


class BudgetedChatCompletionsClient(ChatCompletionsClient):
    """Same transport, with one reservation before its single HTTP attempt.

    input_token_bound must be a trustworthy upper bound for this exact provider
    prompt, not a character-to-token guess. Failure/cancellation never assumes
    a free request. Unknown usage retains funds until external reconciliation.
    """
    def __init__(self, config: ChatCompletionsConfig, *, budget_guard: BudgetGuard,
                 input_token_bound: Callable[[str], int], transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(budget_guard, BudgetGuard) or not callable(input_token_bound):
            raise BudgetConfigurationError("Budget guard and explicit input token bound are required")
        super().__init__(config, transport=transport)
        self.budget_guard, self.input_token_bound = budget_guard, input_token_bound
        self.last_reservation_id: str | None = None
        self._billing_lock = asyncio.Lock()

    async def generate(self, prompt: str) -> str:
        # Metadata and its corresponding reservation belong to the same call,
        # even if a caller accidentally shares this client between jobs.
        async with self._billing_lock:
            return await self._generate_budgeted(prompt)

    async def _generate_budgeted(self, prompt: str) -> str:
        self.last_usage = self.last_finish_reason = self.last_content_chars = None
        self.last_reservation_id = None
        try:
            bound = self.input_token_bound(prompt)
        except Exception:
            raise BudgetConfigurationError("Input token bound unavailable") from None
        if type(bound) is not int or bound <= 0:
            raise BudgetConfigurationError("The complete provider request requires a positive input token bound")
        ticket = await self.budget_guard.reserve(self.config.model, max_input_tokens=bound,
                                                  max_completion_tokens=self.config.max_output_tokens)
        self.last_reservation_id = ticket.id
        cancelled = False
        try:
            return await super().generate(prompt)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # A cancelled HTTP call keeps its existing reservation for external
            # reconciliation. Never delay cancellation on a second store call.
            # Cancellation during settlement is also safe: the store must apply
            # that transition atomically, with the reservation retained on failure.
            if not cancelled:
                await self.budget_guard.settle(ticket, TokenUsage.from_provider(self.last_usage))
