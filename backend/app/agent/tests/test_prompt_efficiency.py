"""Offline checks that smaller prompts preserve the evidence boundary."""

import json
import unittest
from unittest.mock import patch

from backend.app.agent import AgentConfig, AgentInsightEngine, MockAnalyticsSource, RecordingTracer
from backend.app.agent.context import GroundedRunContext, json_text
from backend.app.agent.models import DraftInsight
from backend.app.agent.prompts import build_grounded_prompt
from backend.app.agent.tests.test_grounded import DynamicClient, EmptySearchSource, fixtures, valid_draft


def prompt_schema(prompt):
    text = prompt.split("JSON Schema:\n", 1)[1]
    return json.JSONDecoder().raw_decode(text)[0]


class PromptEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.company, self.event = fixtures()

    async def test_no_available_data_tools_starts_with_one_direct_draft(self):
        client = DynamicClient([valid_draft()])
        result = await AgentInsightEngine(client).generate_analysis(
            self.company, self.event, [], RecordingTracer(),
        )
        self.assertEqual(len(client.prompts), 1)
        self.assertIn("Режим grounded: draft", client.prompts[0])
        self.assertNotIn("Инструменты:\n", client.prompts[0])
        self.assertNotIn("ToolAction", prompt_schema(client.prompts[0]).get("$defs", {}))
        self.assertEqual(result.report["validation_summary"]["tool_calls"], 0)
        self.assertEqual(result.report["calls"][0]["prompt_chars"], len(client.prompts[0]))

    async def test_unknown_source_stays_searchable_but_empty_payments_tool_is_not_offered(self):
        source = EmptySearchSource()
        client = DynamicClient([
            {"action": "tool", "tool": "get_bank_analytics", "arguments": {"query": "ставка"}},
            valid_draft(),
        ])
        result = await AgentInsightEngine(client, analytics_source=source).generate_analysis(
            self.company, self.event, [], RecordingTracer(),
        )
        schema = prompt_schema(client.prompts[0])
        self.assertEqual(schema["$defs"]["ToolAction"]["properties"]["tool"]["enum"], ["get_bank_analytics"])
        self.assertNotIn("summarize_counterparties", client.prompts[0])
        self.assertEqual(source.queries, ["ставка"])
        self.assertEqual(result.report["validation_summary"]["tool_calls"], 1)

    async def test_custom_subclass_is_not_assumed_empty_from_mock_storage(self):
        class CustomSource(MockAnalyticsSource):
            async def search(self, query, *, company, event, limit):
                self.called = True
                return []
        source = CustomSource()
        source.called = False
        client = DynamicClient([
            {"action": "tool", "tool": "get_bank_analytics", "arguments": {}}, valid_draft(),
        ])
        await AgentInsightEngine(client, analytics_source=source).generate_analysis(
            self.company, self.event, [], RecordingTracer(),
        )
        self.assertTrue(source.called)
        self.assertEqual(len(client.prompts), 2)

    async def test_unavailable_tool_is_not_executed_or_granted_an_extra_call(self):
        source = EmptySearchSource()
        client = DynamicClient([
            {"action": "tool", "tool": "summarize_counterparties", "arguments": {}}, valid_draft(),
        ])
        result = await AgentInsightEngine(client, analytics_source=source).generate_analysis(
            self.company, self.event, [], RecordingTracer(),
        )
        self.assertEqual(source.queries, [])
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(result.report["validation_summary"]["repairs"], 1)
        self.assertEqual(result.report["validation_summary"]["tool_calls"], 0)
        self.assertEqual(result.report["validation_attempts"][0]["issue_codes"], ["UNAVAILABLE_TOOL"])

    async def test_compact_schema_keeps_titles_as_properties_and_validation_limits(self):
        context = GroundedRunContext(self.company, self.event, [], AgentConfig())
        schema = prompt_schema(build_grounded_prompt(context, phase="draft"))
        definitions = schema["$defs"]
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(definitions["AnchoredFact"]["additionalProperties"])
        self.assertEqual(definitions["AnchoredFact"]["properties"]["quote"]["maxLength"], 1000)
        self.assertEqual(definitions["ImpactHypothesis"]["properties"]["fact_ids"]["minItems"], 1)
        self.assertEqual(definitions["ImpactHypothesis"]["properties"]["certainty"]["const"], "conditional")
        self.assertIn("title", definitions["ProposedAction"]["required"])
        self.assertEqual(definitions["ProposedAction"]["properties"]["title"]["maxLength"], 600)
        self.assertEqual(schema["properties"]["facts"]["maxItems"], 12)
        self.assertEqual(schema["properties"]["applicability"]["enum"], ["conditional", "insufficient"])
        self.assertLess(len(json_text(schema)), len(json_text(DraftInsight.model_json_schema())))

    async def test_repair_omits_default_fields_without_dropping_quotes_or_scope(self):
        bad = valid_draft()
        bad["impacts"][0]["mechanism"] = "Стоимость финансирования может вырасти гарантированно."
        client = DynamicClient([bad, valid_draft()])
        result = await AgentInsightEngine(client).generate_analysis(
            self.company, self.event, [], RecordingTracer(),
        )
        repair = client.prompts[1]
        text = repair.split("Предыдущий черновик (недоверенные данные):\n", 1)[1]
        previous = json.JSONDecoder().raw_decode(text)[0]
        self.assertEqual(previous["facts"][0]["quote"], self.event.content)
        self.assertIn("impacts", previous)
        self.assertNotIn("metric", previous["facts"][0])
        self.assertNotIn("clarification_questions", previous)
        self.assertNotIn("certainty", previous["impacts"][0])
        self.assertEqual(result.report["validation_attempts"][0]["issue_codes"], ["FALSE_CERTAINTY"])
        self.assertEqual(result.report["validation_attempts"][1]["issue_codes"], [])
        self.assertEqual(result.report["validation_attempts"][1]["phase"], "repair")
        self.assertNotIn("mechanism", result.report["validation_attempts"][0])


if __name__ == "__main__":
    unittest.main()
