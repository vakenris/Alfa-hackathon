"""Offline contracts for atomic budget reservations and transport accounting.

The fake store models the required atomic persistence contract. It is not a
production persistence implementation or evidence of cross-process DB behavior.
All provider traffic uses httpx.MockTransport; no credentials are read.
"""

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx

from backend.app.llm.budget_guard import (
    BudgetConfigurationError, BudgetExceededError, BudgetGuard, BudgetStoreError,
    BudgetWindow, RateCard, TokenUsage, BudgetedChatCompletionsClient,
)
from backend.app.llm.client import (
    ChatCompletionsConfig, LLMHTTPError, LLMOutputLimitError, LLMResponseError,
    LLMTransportError,
)


MODEL = "offline-budget-fixture"
NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


class FakeAtomicStore:
    """One shared ledger, with atomic check-and-reserve and idempotent settle."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.spent = {}
        self.reserved = {}
        self.settled = {}
        self.windows = {}
        self.reserve_calls = []
        self.settle_calls = []

    async def reserve(self, *, team_id, window, limit_rub, reservation_id, amount_rub):
        async with self.lock:
            key = (team_id, window.id)
            self.reserve_calls.append((key, reservation_id, amount_rub))
            snapshot = (window.starts_at, window.ends_at, limit_rub)
            if key in self.windows and self.windows[key] != snapshot:
                raise ValueError("Budget window ID reused with different boundaries or limit")
            self.windows[key] = snapshot
            pending = self.reserved.setdefault(key, {})
            if reservation_id in pending:
                return pending[reservation_id] == amount_rub
            total = self.spent.get(key, Decimal(0)) + sum(pending.values(), Decimal(0))
            if total + amount_rub > limit_rub:
                return False
            pending[reservation_id] = amount_rub
            return True

    async def settle(self, *, team_id, window_id, reservation_id, actual_rub):
        async with self.lock:
            key = (team_id, window_id)
            self.settle_calls.append((key, reservation_id, actual_rub))
            identity = (*key, reservation_id)
            if identity in self.settled:
                if actual_rub is None or self.settled[identity] == actual_rub:
                    return
                raise ValueError("A finalized bill cannot change")
            if reservation_id not in self.reserved.get(key, {}):
                raise AssertionError("Cannot settle an unknown reservation")
            if actual_rub is None:
                return
            self.reserved[key].pop(reservation_id)
            self.spent[key] = self.spent.get(key, Decimal(0)) + actual_rub
            self.settled[identity] = actual_rub

    def held(self, *, team_id="team-test", window_id="utc-day"):
        return sum(self.reserved.get((team_id, window_id), {}).values(), Decimal(0))


def window():
    return BudgetWindow(id="utc-day", starts_at=NOW - timedelta(hours=12), ends_at=NOW + timedelta(hours=12))


def rate(**updates):
    # Deliberately synthetic: one ruble per token, not a claimed market tariff.
    values = {
        "model": MODEL, "input_rub_per_million": Decimal("1000000"),
        "output_rub_per_million": Decimal("1000000"), "reference": "synthetic-test-rate",
    }
    values.update(updates)
    return RateCard(**values)


def guard(store, **updates):
    values = {
        "store": store, "team_id": "team-test", "window_provider": window,
        "rate_cards": {MODEL: rate()}, "daily_limit_rub": Decimal("500"), "clock": lambda: NOW,
    }
    values.update(updates)
    return BudgetGuard(**values)


class TokenAccountingTests(unittest.TestCase):
    def test_provider_reasoning_is_a_subset_of_completion(self):
        usage = TokenUsage.from_provider({
            "prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 150},
        })
        self.assertTrue(usage.complete)
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.reasoning_tokens), (100, 200, 150))
        self.assertEqual(rate().cost(usage), Decimal("300"))

    def test_separate_reasoning_rate_replaces_subset_instead_of_adding_it(self):
        card = rate(
            input_rub_per_million=Decimal("2"), output_rub_per_million=Decimal("8"),
            reasoning_rub_per_million=Decimal("3"),
        )
        usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000, reasoning_tokens=400_000)
        self.assertEqual(card.cost(usage), Decimal("8"))
        self.assertIsNone(card.cost(TokenUsage(prompt_tokens=10, completion_tokens=20)))

    def test_reservation_uses_the_most_expensive_completion_subset(self):
        card = rate(
            input_rub_per_million=Decimal("2"), output_rub_per_million=Decimal("8"),
            reasoning_rub_per_million=Decimal("12"),
        )
        self.assertEqual(card.reserve_cost(1_000_000, 1_000_000), Decimal("14"))

    def test_missing_usage_is_unknown_and_never_zero(self):
        for raw in (None, {}, {"prompt_tokens": 10}, {"completion_tokens": 20}):
            with self.subTest(raw=raw):
                usage = TokenUsage.from_provider(raw)
                self.assertFalse(usage.complete)
                self.assertIsNone(rate().cost(usage))

    def test_invalid_provider_counts_cannot_be_complete(self):
        for raw in (
            {"prompt_tokens": True, "completion_tokens": 20},
            {"prompt_tokens": -1, "completion_tokens": 20},
            {"prompt_tokens": 10, "completion_tokens": False},
            {"prompt_tokens": 10, "completion_tokens": -1},
            {"prompt_tokens": "10", "completion_tokens": 20},
            {"prompt_tokens": 10, "completion_tokens": 20.0},
        ):
            with self.subTest(raw=raw):
                try:
                    usage = TokenUsage.from_provider(raw)
                except (BudgetConfigurationError, ValueError, TypeError):
                    continue
                self.assertFalse(usage.complete)
                self.assertIsNone(rate().cost(usage))

    def test_invalid_reasoning_cannot_create_an_undercharge(self):
        card = rate(reasoning_rub_per_million=Decimal("500000"))
        for count in (True, -1, 21, "10"):
            with self.subTest(count=count):
                try:
                    usage = TokenUsage.from_provider({
                        "prompt_tokens": 10, "completion_tokens": 20,
                        "completion_tokens_details": {"reasoning_tokens": count},
                    })
                except (BudgetConfigurationError, ValueError, TypeError):
                    continue
                self.assertIsNone(card.cost(usage))

    def test_inconsistent_provider_total_does_not_release_a_reservation(self):
        usage = TokenUsage.from_provider({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 25})
        self.assertFalse(usage.complete)
        self.assertIsNone(rate().cost(usage))

    def test_invalid_counts_and_rates_are_rejected_at_construction(self):
        for values in (
            {"prompt_tokens": True, "completion_tokens": 10},
            {"prompt_tokens": 10, "completion_tokens": -1},
            {"prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 21},
        ):
            with self.subTest(values=values), self.assertRaises(BudgetConfigurationError):
                TokenUsage(**values)
        for amount in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), 1.0):
            with self.subTest(amount=amount), self.assertRaises(BudgetConfigurationError):
                rate(input_rub_per_million=amount)

    def test_budget_window_requires_ordered_timezone_aware_boundaries(self):
        for start, end in (
            (NOW.replace(tzinfo=None), NOW + timedelta(days=1)),
            (NOW, NOW),
            (NOW, NOW - timedelta(seconds=1)),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(BudgetConfigurationError):
                BudgetWindow(id="invalid", starts_at=start, ends_at=end)


class BudgetGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeAtomicStore()
        self.guard = guard(self.store)

    async def test_two_guards_cannot_concurrently_reserve_more_than_the_shared_limit(self):
        second = guard(self.store)
        results = await asyncio.gather(
            self.guard.reserve(MODEL, max_input_tokens=100, max_completion_tokens=200),
            second.reserve(MODEL, max_input_tokens=100, max_completion_tokens=200),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(item, BaseException) for item in results), 1)
        self.assertEqual(sum(isinstance(item, BudgetExceededError) for item in results), 1)
        self.assertEqual(self.store.held(), Decimal("300"))
        self.assertEqual(len(self.store.reserve_calls), 2)

    async def test_exact_daily_boundary_is_allowed_then_fails_closed(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=200, max_completion_tokens=300)
        self.assertEqual(reservation.amount_rub, Decimal("500"))
        with self.assertRaises(BudgetExceededError):
            await self.guard.reserve(MODEL, max_input_tokens=1, max_completion_tokens=1)
        self.assertEqual(self.store.held(), Decimal("500"))

    async def test_known_usage_releases_excess_reservation_and_settlement_is_idempotent(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=100, max_completion_tokens=200)
        usage = TokenUsage(prompt_tokens=30, completion_tokens=20)
        await self.guard.settle(reservation, usage)
        await self.guard.settle(reservation, usage)
        self.assertEqual(self.store.held(), Decimal(0))
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("50"))
        await self.guard.reserve(MODEL, max_input_tokens=200, max_completion_tokens=250)
        with self.assertRaises(BudgetExceededError):
            await self.guard.reserve(MODEL, max_input_tokens=1, max_completion_tokens=1)

    async def test_unknown_usage_holds_funds_until_explicit_reconciliation(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=100, max_completion_tokens=200)
        await self.guard.settle(reservation, None)
        await self.guard.settle(reservation, TokenUsage(prompt_tokens=10, completion_tokens=None))
        self.assertEqual(self.store.held(), Decimal("300"))
        self.assertEqual(self.store.spent.get(("team-test", "utc-day"), Decimal(0)), Decimal(0))
        with self.assertRaises(BudgetExceededError):
            await guard(self.store).reserve(MODEL, max_input_tokens=100, max_completion_tokens=200)
        await self.guard.settle(reservation, TokenUsage(prompt_tokens=10, completion_tokens=20))
        self.assertEqual(self.store.held(), Decimal(0))
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("30"))

    async def test_bill_above_reservation_is_recorded_and_blocks_later_calls(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=100, max_completion_tokens=100)
        await self.guard.settle(reservation, TokenUsage(prompt_tokens=300, completion_tokens=300))
        self.assertEqual(self.store.held(), Decimal(0))
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("600"))
        with self.assertRaises(BudgetExceededError):
            await self.guard.reserve(MODEL, max_input_tokens=1, max_completion_tokens=1)

    async def test_daily_limit_above_500_is_not_accepted(self):
        with self.assertRaises(BudgetConfigurationError):
            guard(self.store, daily_limit_rub=Decimal("500.01"))

    async def test_team_cannot_settle_another_teams_reservation(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=10, max_completion_tokens=20)
        with self.assertRaises(BudgetConfigurationError):
            await guard(self.store, team_id="other-team").settle(reservation, TokenUsage(1, 1))
        self.assertEqual(self.store.settle_calls, [])
        self.assertEqual(self.store.held(), Decimal("30"))

    async def test_repeated_known_bill_cannot_change_the_finalized_cost(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=100, max_completion_tokens=200)
        await self.guard.settle(reservation, TokenUsage(10, 20))
        with self.assertRaises(BudgetStoreError):
            await self.guard.settle(reservation, TokenUsage(10, 30))
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("30"))
        self.assertEqual(self.store.held(), Decimal(0))

    async def test_late_unknown_usage_does_not_reopen_a_finalized_bill(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=100, max_completion_tokens=200)
        await self.guard.settle(reservation, TokenUsage(10, 20))
        await self.guard.settle(reservation, None)
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("30"))
        self.assertEqual(self.store.held(), Decimal(0))

    async def test_unknown_reservation_cannot_be_settled_even_without_usage(self):
        reservation = await self.guard.reserve(MODEL, max_input_tokens=10, max_completion_tokens=20)
        with self.assertRaises(BudgetStoreError):
            await self.guard.settle(replace(reservation, id="unknown-reservation"), None)
        self.assertEqual(self.store.held(), Decimal("30"))

    async def test_window_id_reuse_with_changed_boundaries_or_limit_is_rejected(self):
        await self.guard.reserve(MODEL, max_input_tokens=10, max_completion_tokens=20)
        shifted = BudgetWindow(
            id="utc-day", starts_at=NOW - timedelta(hours=11), ends_at=NOW + timedelta(hours=12),
        )
        for updates in ({"window_provider": lambda: shifted}, {"daily_limit_rub": Decimal("400")}):
            with self.subTest(updates=list(updates)), self.assertRaises(BudgetStoreError):
                await guard(self.store, **updates).reserve(MODEL, max_input_tokens=10, max_completion_tokens=20)
        self.assertEqual(self.store.held(), Decimal("30"))

    async def test_store_deadline_requires_finite_positive_bounded_seconds(self):
        for timeout in (True, 0, -1, float("inf"), float("nan"), 60.01, "5"):
            with self.subTest(timeout=timeout), self.assertRaises(BudgetConfigurationError):
                guard(self.store, store_timeout_seconds=timeout)

    async def test_missing_tariff_does_not_create_a_reservation(self):
        with self.assertRaises(BudgetConfigurationError):
            await self.guard.reserve("unknown-model", max_input_tokens=10, max_completion_tokens=20)
        self.assertEqual(self.store.reserve_calls, [])

    async def test_expired_or_future_window_is_rejected_before_the_store(self):
        for current in (
            BudgetWindow(id="expired", starts_at=NOW - timedelta(days=1), ends_at=NOW),
            BudgetWindow(id="future", starts_at=NOW + timedelta(seconds=1), ends_at=NOW + timedelta(days=1)),
        ):
            with self.subTest(window=current.id):
                with self.assertRaises(BudgetConfigurationError):
                    await guard(self.store, window_provider=lambda: current).reserve(
                        MODEL, max_input_tokens=10, max_completion_tokens=20,
                    )
        self.assertEqual(self.store.reserve_calls, [])

    async def test_missing_shared_store_is_a_configuration_error(self):
        with self.assertRaises(BudgetConfigurationError):
            missing = guard(None)
            await missing.reserve(MODEL, max_input_tokens=10, max_completion_tokens=20)

    async def test_store_failure_prevents_an_unaccounted_request(self):
        class FailingStore(FakeAtomicStore):
            async def reserve(self, **kwargs):
                raise RuntimeError("private storage error")
        with self.assertRaises(BudgetStoreError) as caught:
            await guard(FailingStore()).reserve(MODEL, max_input_tokens=10, max_completion_tokens=20)
        self.assertNotIn("private storage error", str(caught.exception))


class BudgetedClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.store = FakeAtomicStore()
        self.guard = guard(self.store)
        self.calls = 0

    def client(self, handler, *, budget=None, input_bound=None):
        return BudgetedChatCompletionsClient(
            ChatCompletionsConfig(api_url="https://budget.example.invalid/chat", model=MODEL, max_output_tokens=200),
            budget_guard=budget or self.guard, input_token_bound=input_bound or (lambda prompt: 100),
            transport=httpx.MockTransport(handler),
        )

    def response(self, *, usage=None, finish="stop", content="offline answer", refusal=None):
        return httpx.Response(200, json={
            "usage": usage,
            "choices": [{"finish_reason": finish, "message": {"content": content, "refusal": refusal}}],
        })

    async def test_success_uses_one_http_call_and_accounts_for_known_usage(self):
        def handler(request):
            self.calls += 1
            return self.response(usage={"prompt_tokens": 30, "completion_tokens": 20})
        client = self.client(handler)
        self.assertEqual(await client.generate("offline prompt"), "offline answer")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.store.held(), Decimal(0))
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("50"))

    async def test_shared_client_does_not_apply_another_calls_usage_to_an_http_failure(self):
        active = 0
        peak_active = 0
        async def handler(request):
            nonlocal active, peak_active
            self.calls += 1
            number = self.calls
            active += 1
            peak_active = max(peak_active, active)
            try:
                if number == 1:
                    # Without per-instance serialization, the successful call
                    # can overwrite mutable last_usage before this error ends.
                    await asyncio.sleep(0.01)
                    return httpx.Response(503)
                await asyncio.sleep(0)
                return self.response(usage={"prompt_tokens": 1, "completion_tokens": 1})
            finally:
                active -= 1
        client = self.client(handler, input_bound=lambda prompt: 10)
        results = await asyncio.gather(
            client.generate("first offline prompt"),
            client.generate("second offline prompt"),
            return_exceptions=True,
        )
        self.assertIsInstance(results[0], LLMHTTPError)
        self.assertEqual(results[1], "offline answer")
        self.assertEqual(self.calls, 2)
        self.assertEqual(peak_active, 1)
        self.assertEqual(self.store.spent[("team-test", "utc-day")], Decimal("2"))
        self.assertEqual(self.store.held(), Decimal("210"))
        self.assertEqual([call[2] for call in self.store.settle_calls], [None, Decimal("2")])

    async def test_truncation_and_refusal_still_settle_known_usage_without_retry(self):
        for options, expected in (
            ({"finish": "length"}, LLMOutputLimitError),
            ({"refusal": "synthetic refusal"}, LLMResponseError),
        ):
            with self.subTest(options=options):
                store = FakeAtomicStore()
                def handler(request):
                    self.calls += 1
                    return self.response(usage={"prompt_tokens": 30, "completion_tokens": 20}, **options)
                with self.assertRaises(expected):
                    await self.client(handler, budget=guard(store)).generate("offline prompt")
                self.assertEqual(store.held(), Decimal(0))
                self.assertEqual(store.spent[("team-test", "utc-day")], Decimal("50"))
        self.assertEqual(self.calls, 2)

    async def test_http_and_transport_errors_hold_unknown_usage_without_retry(self):
        for kind in ("http", "transport"):
            with self.subTest(kind=kind):
                store = FakeAtomicStore()
                def handler(request):
                    self.calls += 1
                    if kind == "transport":
                        raise httpx.ConnectError("offline fixture", request=request)
                    return httpx.Response(503)
                with self.assertRaises(LLMHTTPError if kind == "http" else LLMTransportError):
                    await self.client(handler, budget=guard(store)).generate("offline prompt")
                self.assertEqual(store.held(), Decimal("300"))
                self.assertEqual(store.spent.get(("team-test", "utc-day"), Decimal(0)), Decimal(0))
        self.assertEqual(self.calls, 2)

    async def test_missing_usage_on_success_remains_reserved(self):
        def handler(request):
            self.calls += 1
            return self.response()
        self.assertEqual(await self.client(handler).generate("offline prompt"), "offline answer")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.store.held(), Decimal("300"))

    async def test_external_cancellation_keeps_the_reservation_and_propagates(self):
        entered = asyncio.Event()
        async def handler(request):
            self.calls += 1
            entered.set()
            await asyncio.Future()
        task = asyncio.create_task(self.client(handler).generate("offline prompt"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.store.held(), Decimal("300"))

    async def test_insufficient_budget_or_missing_rate_blocks_http_entirely(self):
        await self.guard.reserve(MODEL, max_input_tokens=200, max_completion_tokens=300)
        def handler(request):
            self.calls += 1
            return self.response()
        with self.assertRaises(BudgetExceededError):
            await self.client(handler).generate("offline prompt")
        no_tariff = guard(FakeAtomicStore(), rate_cards={})
        with self.assertRaises(BudgetConfigurationError):
            await self.client(handler, budget=no_tariff).generate("offline prompt")
        self.assertEqual(self.calls, 0)

    async def test_invalid_token_bound_blocks_http_before_reserving(self):
        for bound in (True, 0, -1, "100", 10.5):
            with self.subTest(bound=bound):
                def handler(request):
                    self.calls += 1
                    return self.response()
                with self.assertRaises(BudgetConfigurationError):
                    await self.client(handler, input_bound=lambda prompt: bound).generate("offline prompt")
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.reserve_calls, [])

    async def test_settlement_failure_is_safe_and_retains_the_existing_reservation(self):
        class FailingSettlement(FakeAtomicStore):
            async def settle(self, **kwargs):
                raise RuntimeError("private settlement detail")
        store = FailingSettlement()
        def handler(request):
            self.calls += 1
            return self.response(usage={"prompt_tokens": 30, "completion_tokens": 20})
        with self.assertRaises(BudgetStoreError) as caught:
            await self.client(handler, budget=guard(store)).generate("offline prompt")
        self.assertNotIn("private settlement detail", str(caught.exception))
        self.assertEqual(self.calls, 1)
        self.assertEqual(store.held(), Decimal("300"))

    async def test_cancellation_is_not_replaced_by_a_settlement_store_failure(self):
        class FailingSettlement(FakeAtomicStore):
            async def settle(self, **kwargs):
                raise RuntimeError("private settlement detail")
        store = FailingSettlement()
        entered = asyncio.Event()
        async def handler(request):
            self.calls += 1
            entered.set()
            await asyncio.Future()
        task = asyncio.create_task(self.client(handler, budget=guard(store)).generate("offline prompt"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.calls, 1)
        self.assertEqual(store.held(), Decimal("300"))

    async def test_cancellation_does_not_wait_for_a_slow_second_store_call(self):
        class SlowSettlement(FakeAtomicStore):
            def __init__(self):
                super().__init__()
                self.settlement_attempts = 0

            async def settle(self, **kwargs):
                self.settlement_attempts += 1
                await asyncio.Future()
        store = SlowSettlement()
        entered = asyncio.Event()
        async def handler(request):
            self.calls += 1
            entered.set()
            await asyncio.Future()
        client = self.client(handler, budget=guard(store, store_timeout_seconds=5))
        task = asyncio.create_task(client.generate("offline prompt"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        self.assertEqual(store.settlement_attempts, 0)
        self.assertEqual(store.held(), Decimal("300"))
        self.assertEqual(self.calls, 1)

    async def test_reservation_timeout_prevents_http_and_keeps_a_committed_reservation(self):
        class SlowAcknowledgement(FakeAtomicStore):
            async def reserve(self, **kwargs):
                await super().reserve(**kwargs)
                await asyncio.Future()
        store = SlowAcknowledgement()
        def handler(request):
            self.calls += 1
            return self.response()
        with self.assertRaises(BudgetStoreError):
            await self.client(handler, budget=guard(store, store_timeout_seconds=0.01)).generate("offline prompt")
        self.assertEqual(self.calls, 0)
        self.assertEqual(store.held(), Decimal("300"))

    async def test_settlement_timeout_keeps_the_full_reservation_after_known_usage(self):
        class SlowSettlement(FakeAtomicStore):
            async def settle(self, **kwargs):
                await asyncio.Future()
        store = SlowSettlement()
        def handler(request):
            self.calls += 1
            return self.response(usage={"prompt_tokens": 30, "completion_tokens": 20})
        with self.assertRaises(BudgetStoreError):
            await self.client(handler, budget=guard(store, store_timeout_seconds=0.01)).generate("offline prompt")
        self.assertEqual(self.calls, 1)
        self.assertEqual(store.held(), Decimal("300"))
        self.assertEqual(store.spent.get(("team-test", "utc-day"), Decimal(0)), Decimal(0))


if __name__ == "__main__":
    unittest.main()
