import json
import unittest
from datetime import datetime, timezone

from backend.app.agent import CompatibilityInsightEngine as AgentInsightEngine, FakeLLMClient, RecordingTracer
from backend.app.agent.models import ACTION_ADAPTER, AgentResponseError
from backend.app.ml.interface import (
    AgentTracerProtocol,
    MLInsightEngineProtocol,
    MLInsightResult,
)
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse


class AgentInsightEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.company = CompanyResponse(
            id=1,
            synthetic_u="TEST_COMPANY",
            name="Тестовый завод",
            mainokved_code="24.10",
            revenue_company=20_000_000_000,
            cl_product_loa_mark=True,
        )
        self.event = NewsArticleResponse(
            id=10,
            title="Тестовая новость о финансировании",
            source="Тестовый источник",
            published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
            content="Тестовый материал: условия финансирования не изменились.",
            category="Тестовая экономика",
        )
        self.response = {
            "what_happened": "В тестовом материале условия финансирования не изменились.",
            "why_important": "В профиле компании отмечены кредитные продукты.",
            "consequences": "Влияние на расходы без условий кредитов определить нельзя.",
            "bank_view": "Позиция Альфа-Банка не предоставлена.",
            "ai_view": "Условия действующих кредитов требуют отдельной проверки.",
            "recommended_action": "Проверить условия кредитных договоров.",
            "relevant_bank_product": None,
            "evidences": [
                {
                    "claim": "В профиле отмечены кредитные продукты.",
                    "source_title": "Профиль компании",
                    "source_url": None,
                    "company_feature": "cl_product_loa_mark=true",
                }
            ],
        }
        self.tracer = RecordingTracer()

    async def test_successful_response_and_prompt(self):
        client = FakeLLMClient(json.dumps(self.response, ensure_ascii=False))
        engine = AgentInsightEngine(client)
        counterparties = [
            CounterpartyItem(
                synthetic_u="TEST_SUPPLIER",
                direction="SUPPLIER",
                total_amount=1000,
                transaction_count=2,
            )
        ]

        result = await engine.generate_insight(
            self.company, self.event, counterparties, self.tracer
        )

        self.assertIsInstance(engine, MLInsightEngineProtocol)
        self.assertIsInstance(self.tracer, AgentTracerProtocol)
        self.assertIsInstance(result, MLInsightResult)
        self.assertEqual(result.model_dump(), self.response)
        self.assertEqual(len(client.prompts), 1)
        prompt = client.prompts[0]
        context = json.loads(prompt.split("Входные данные (JSON):\n", 1)[1])
        self.assertEqual(context["company"]["name"], self.company.name)
        self.assertEqual(context["company"]["revenue_company"], 20_000_000_000)
        self.assertEqual(context["event"]["content"], self.event.content)
        self.assertEqual(context["event"]["published_at"], "2026-10-21T00:00:00Z")
        self.assertEqual(context["counterparties"][0]["synthetic_u"], "TEST_SUPPLIER")
        schema_text = prompt.split("Схема действия (JSON Schema):\n", 1)[1]
        schema = json.loads(schema_text.split("\nВходные данные (JSON):\n", 1)[0])
        self.assertEqual(schema, ACTION_ADAPTER.json_schema())
        self.assertTrue(self.tracer.steps)
        self.assertEqual(self.tracer.steps[-1]["step_name"], "Проверка формата")

    async def test_invalid_json_is_reported_without_retry(self):
        client = FakeLLMClient('{"what_happened":')

        with self.assertRaises(AgentResponseError):
            await AgentInsightEngine(client).generate_insight(
                self.company, self.event, [], self.tracer
            )

        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(self.tracer.steps[-1]["step_name"], "Ошибка валидации")

    async def test_valid_json_with_invalid_schema_is_rejected(self):
        for payload in ({"what_happened": "Недостаточно полей"}, [], None):
            with self.subTest(payload=payload):
                client = FakeLLMClient(json.dumps(payload))
                with self.assertRaises(AgentResponseError):
                    await AgentInsightEngine(client).generate_insight(
                        self.company, self.event, [], RecordingTracer()
                    )
                self.assertEqual(len(client.prompts), 1)

    async def test_missing_optional_input_data(self):
        company = CompanyResponse(
            id=2,
            synthetic_u="MINIMAL_COMPANY",
            name="Компания без дополнительных данных",
            revenue_company=None,
        )
        response = {**self.response, "evidences": []}
        client = FakeLLMClient(json.dumps(response, ensure_ascii=False))

        result = await AgentInsightEngine(client).generate_insight(
            company, self.event, [], self.tracer
        )

        self.assertIsInstance(result, MLInsightResult)
        context = json.loads(client.prompts[0].split("Входные данные (JSON):\n", 1)[1])
        self.assertIsNone(context["company"]["revenue_company"])
        self.assertNotIn("mainokved_code", context["company"])
        self.assertNotIn("cl_product_loa_mark", context["company"])
        self.assertNotIn("summary", context["event"])
        self.assertEqual(context["counterparties"], [])

    async def test_optional_result_fields_use_backend_defaults(self):
        payload = self.response.copy()
        del payload["relevant_bank_product"]
        del payload["evidences"]
        client = FakeLLMClient(json.dumps(payload, ensure_ascii=False))

        result = await AgentInsightEngine(client).generate_insight(
            self.company, self.event, [], self.tracer
        )

        self.assertIsNone(result.relevant_bank_product)
        self.assertEqual(result.evidences, [])

    async def test_default_client_returns_an_explicit_demo_response(self):
        result = await AgentInsightEngine().generate_insight(
            self.company, self.event, [], self.tracer
        )

        self.assertIn("Демонстрационный ответ", result.what_happened)
        self.assertIsNone(result.relevant_bank_product)
        self.assertEqual(result.evidences, [])

    async def test_tracer_records_steps_in_order_and_keeps_instances_separate(self):
        await self.tracer.emit_step("Начало", "Первый шаг")
        await self.tracer.emit_step("Завершение", "Второй шаг")

        self.assertEqual(
            self.tracer.steps,
            [
                {"step_name": "Начало", "description": "Первый шаг"},
                {"step_name": "Завершение", "description": "Второй шаг"},
            ],
        )
        self.assertEqual(RecordingTracer().steps, [])


if __name__ == "__main__":
    unittest.main()
