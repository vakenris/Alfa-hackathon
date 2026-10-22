import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from backend.app.agent import AgentConfig, AnalyticsDocument, MockAnalyticsSource
from backend.app.agent.models import ToolAction
from backend.app.agent.tools import execute_tool
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.company = CompanyResponse(id=1, synthetic_u="TEST", name="Завод")
        self.event = NewsArticleResponse(
            id=1, title="Событие", source="Тест", category="Экономика", content="Факт",
            published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )
        self.document = AnalyticsDocument(article=self.event.model_copy(update={"is_bank_analytics": True}))

    async def execute(self, name="get_bank_analytics", *, arguments=None, source=None, counterparties=None):
        return await execute_tool(
            ToolAction(action="tool", tool=name, arguments=arguments or {}),
            source=source if source is not None else MockAnalyticsSource(),
            company=self.company, event=self.event, counterparties=counterparties or [], config=AgentConfig(),
        )

    async def test_invalid_and_non_bank_source_results_are_recoverable_errors(self):
        for returned in (None, {"documents": []}, [None], [{"article": {"title": "Bad"}}],
                         [AnalyticsDocument(article=self.event)]):
            with self.subTest(returned=returned):
                source = AsyncMock()
                source.search.return_value = returned
                observation = await self.execute(source=source)
                self.assertEqual(observation.status, "error")
                self.assertEqual(observation.error, "INVALID_TOOL_RESULT")
                self.assertIsNone(observation.data)

    async def test_strict_arguments_reject_coercion_and_unexpected_scope(self):
        for arguments in ({"limit": "1"}, {"limit": True}, {"limit": 0}, {"query": "x" * 257},
                          {"company_id": 2}, {"url": "https://untrusted.test"}):
            with self.subTest(arguments=arguments):
                source = AsyncMock()
                observation = await self.execute(arguments=arguments, source=source)
                self.assertEqual(observation.error, "INVALID_ARGUMENTS")
                source.search.assert_not_awaited()

    async def test_provider_cannot_exceed_requested_document_limit(self):
        source = AsyncMock()
        source.search.return_value = [self.document, None]
        observation = await self.execute(source=source, arguments={"limit": 1})
        self.assertEqual(observation.status, "ok")
        self.assertEqual(len(observation.data["documents"]), 1)

    async def test_empty_analytics_result_is_successful_not_a_failure(self):
        observation = await self.execute()
        self.assertEqual(observation.status, "ok")
        self.assertEqual(observation.data, {"documents": []})

    async def test_summary_keeps_incoming_and_outgoing_payments_separate(self):
        items = [CounterpartyItem(synthetic_u=key, direction=direction, total_amount=amount, transaction_count=1)
                 for key, direction, amount in (
                     ("A", "SUPPLIER", 500), ("B", "SUPPLIER", 300), ("C", "CUSTOMER", 1000),
                 )]
        observation = await self.execute("summarize_counterparties", counterparties=items)
        self.assertEqual(observation.data["directions"]["SUPPLIER"]["total_amount"], 800)
        self.assertEqual(observation.data["directions"]["CUSTOMER"]["total_amount"], 1000)
        self.assertIn("период", observation.data["scope"])

    async def test_summary_does_not_turn_empty_sample_into_absence_of_connections(self):
        observation = await self.execute("summarize_counterparties")
        self.assertEqual(observation.data["sample_size"], 0)
        self.assertIn("полнота сети", observation.data["scope"])

    async def test_summary_rejects_nonfinite_amounts(self):
        item = CounterpartyItem(synthetic_u="A", direction="SUPPLIER", total_amount=float("inf"), transaction_count=1)
        observation = await self.execute("summarize_counterparties", counterparties=[item])
        self.assertEqual(observation.error, "INVALID_TOOL_RESULT")

    async def test_mock_returns_copies_without_mutating_fixtures(self):
        source = MockAnalyticsSource([self.document])
        first = await source.search("", company=self.company, event=self.event, limit=1)
        first[0].article.content = "Changed"
        second = await source.search("", company=self.company, event=self.event, limit=1)
        self.assertEqual(second[0].article.content, "Факт")


if __name__ == "__main__":
    unittest.main()
