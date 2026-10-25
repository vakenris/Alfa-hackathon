"""Technical evaluation tests never contact a provider or use backend storage."""

from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.agent.config import AgentConfig
from backend.app.agent.evaluation import (
    EVALUATION_VERSION, HUMAN_RUBRIC, VERIFICATION_LIMITS, build_evaluation_cases,
    evaluate_analysis, main, run_offline_evaluation,
)
from backend.app.agent.llm import FakeLLMClient
from backend.app.agent.runtime import RuntimeAgentEngine
from backend.app.agent.tools import MockAnalyticsSource
from backend.app.agent.tracer import RecordingTracer


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    async def run_case(self, case=None):
        selected = case or build_evaluation_cases()[0]
        engine = RuntimeAgentEngine(
            client_factory=lambda: FakeLLMClient(selected.draft.model_dump_json()),
            source_factory=MockAnalyticsSource,
            config=AgentConfig(max_steps=1, max_llm_calls=1, max_tool_calls=0),
            allow_test_dependencies=True,
        )
        return await engine.generate_analysis(selected.company, selected.event, [], RecordingTracer())

    async def test_six_distinct_fixtures_have_explicit_expectations_and_human_questions(self):
        cases = build_evaluation_cases()
        self.assertEqual([case.name for case in cases], ["rate", "logistics", "equipment", "fx", "regulation", "irrelevant"])
        self.assertEqual(len({case.event.content for case in cases}), 6)
        self.assertEqual({channel for case in cases for channel in case.expectation.channels},
                         {"financing", "logistics", "investment", "fx", "regulation"})
        for case in cases:
            with self.subTest(case=case.name):
                self.assertTrue(case.expectation.human_focus)
                self.assertEqual(case.draft.facts[0].quote, case.event.content)
                result = await self.run_case(case)
                report = evaluate_analysis(result, expected=case.expectation)
                self.assertTrue(all(value is True for value in report.technical_checks.values()))
                self.assertTrue(all(report.fixture_expectations.values()))
                self.assertEqual(report.human_review, {label: "required" for label in HUMAN_RUBRIC})
                self.assertFalse(report.llm_intelligence_measured)

    async def test_technical_counts_are_counts_not_a_quality_grade(self):
        result = await self.run_case()
        report = evaluate_analysis(result)
        self.assertEqual(report.counts["facts"], 1)
        self.assertEqual(report.counts["impacts"], 1)
        self.assertEqual(report.counts["recorded_calls"], 1)
        self.assertEqual(report.counts["bank_statement_facts"], 0)
        self.assertEqual(report.fixture_expectations, {})
        self.assertEqual(report.version, EVALUATION_VERSION)
        self.assertEqual(report.limitations, list(VERIFICATION_LIMITS))
        self.assertNotIn("quality_score", report.model_dump())
        self.assertNotIn("accuracy", report.model_dump())

    async def test_unknown_telemetry_stays_unknown(self):
        result = await self.run_case()
        result.report = {}
        report = evaluate_analysis(result)
        self.assertIsNone(report.technical_checks["per_run_call_limit_respected"])
        self.assertIsNone(report.counts["recorded_calls"])

    async def test_reported_call_budget_violation_is_not_hidden(self):
        result = await self.run_case()
        result.report["max_calls"] = 1
        result.report["calls"] = [{}, {}]
        report = evaluate_analysis(result)
        self.assertFalse(report.technical_checks["per_run_call_limit_respected"])
        self.assertEqual(report.counts["recorded_calls"], 2)

    async def test_unknown_references_are_detected_without_recalling_llm(self):
        result = await self.run_case()
        result.analysis.facts[0].source_id = "news:unknown"
        result.analysis.impacts[0].fact_ids = ["unknown"]
        result.analysis.impacts[0].company_features = ["unknown_feature"]
        result.analysis.actions[0].impact_ids = ["unknown_impact"]
        report = evaluate_analysis(result)
        for check in ("fact_sources_exist", "event_anchor_present", "impact_fact_references_exist",
                      "impact_features_are_known", "action_impact_references_exist"):
            self.assertFalse(report.technical_checks[check])

    async def test_unverified_metadata_cannot_be_reported_as_valid_bank_provenance(self):
        result = await self.run_case()
        result.analysis.facts[0].kind = "bank_statement"
        report = evaluate_analysis(result)
        self.assertFalse(report.technical_checks["bank_fact_provenance_metadata_present"])
        self.assertFalse(report.technical_checks["bank_position_projection_consistent"])

    async def test_missing_disclaimer_and_invented_product_are_detected(self):
        result = await self.run_case()
        result.insight.recommended_action = "Без дисклеймера"
        result.insight.relevant_bank_product = "Непроверенный продукт"
        report = evaluate_analysis(result)
        self.assertFalse(report.technical_checks["disclaimer_present"])
        self.assertFalse(report.technical_checks["product_not_invented"])

    async def test_fixture_mismatch_is_separate_from_expert_review(self):
        result = await self.run_case()
        expected = build_evaluation_cases()[-1].expectation
        report = evaluate_analysis(result, expected=expected)
        self.assertFalse(report.fixture_expectations["applicability_matches_fixture"])
        self.assertFalse(report.fixture_expectations["channels_match_fixture"])
        self.assertTrue(all(value == "required" for value in report.human_review.values()))

    async def test_quotation_entailment_is_explicitly_not_rechecked_from_result_metadata(self):
        result = await self.run_case()
        result.analysis.facts[0].quote = "Текст отсутствует в исходном документе."
        report = evaluate_analysis(result)
        self.assertIn("RESULT_METADATA_CANNOT_RECHECK_QUOTES_AGAINST_SOURCE_TEXT", report.limitations)
        self.assertIn("LEXICAL_RULES_DO_NOT_PROVE_CAUSAL_ENTAILMENT", report.limitations)
        self.assertNotIn("quotes_reverified", report.technical_checks)
        self.assertEqual(report.human_review["faithful_source_interpretation_and_temporal_scope"], "required")

    async def test_offline_summary_uses_only_scripted_clients_and_safe_counts(self):
        with patch("backend.app.agent.config.ChatCompletionsConfig.from_env",
                   side_effect=AssertionError("Environment access forbidden")), \
                patch("backend.app.agent.api_client.ChatCompletionsClient.generate",
                      side_effect=AssertionError("Provider calls forbidden")):
            summary = await run_offline_evaluation()
        self.assertEqual(summary["cases"], 6)
        self.assertEqual(summary["technical_passed_cases"], 6)
        self.assertEqual(summary["fixture_expectations_matched_cases"], 6)
        self.assertEqual(summary["provider_calls"], 0)
        self.assertEqual(summary["scripted_client_calls"], 6)
        self.assertEqual(summary["human_review_required_cases"], 6)
        self.assertFalse(summary["llm_intelligence_measured"])
        serialized = json.dumps(summary)
        for case in build_evaluation_cases():
            self.assertNotIn(case.event.content, serialized)
        for forbidden in ("prompt", "insight", "authorization", "company_id", "source_url"):
            self.assertNotIn(forbidden, summary)


class EvaluationCLITests(unittest.TestCase):
    def test_cli_prints_only_safe_summary(self):
        stream = io.StringIO()
        # Windows' event loop uses a local socket pair during construction;
        # block the provider boundary without disabling that internal wakeup.
        with patch("backend.app.agent.api_client.ChatCompletionsClient.generate",
                   side_effect=AssertionError("Provider calls forbidden")), redirect_stdout(stream):
            exit_code = main()
        summary = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["version"], EVALUATION_VERSION)
        self.assertEqual(summary["cases"], 6)
        self.assertIn("verification_limits", summary)

    def test_cli_error_never_prints_exception_source_or_financial_content(self):
        stream = io.StringIO()
        with patch("backend.app.agent.evaluation.run_offline_evaluation",
                   new=AsyncMock(side_effect=RuntimeError("DO_NOT_PRINT_PRIVATE_CONTENT"))), redirect_stdout(stream):
            exit_code = main()
        self.assertEqual(exit_code, 1)
        self.assertNotIn("DO_NOT_PRINT_PRIVATE_CONTENT", stream.getvalue())
        self.assertEqual(json.loads(stream.getvalue())["error_code"], "OFFLINE_EVALUATION_FAILED")


if __name__ == "__main__":
    unittest.main()
