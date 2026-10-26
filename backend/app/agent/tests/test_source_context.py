"""Offline regressions for exact bounded bank passages and their visible scope."""

import unittest
import json
from unittest.mock import patch

from backend.app.agent.config import AgentConfig
from backend.app.agent.context import prepare_context
from backend.app.agent.tools import MockAnalyticsSource
from backend.app.agent.tests.test_context_and_actions import bank_document, company, event, draft
from backend.app.agent import AgentInsightEngine, FakeLLMClient, ManagementActionEngine, RecordingTracer


class BankPassageContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    async def prepare(self, text, limit=400, *, provenance="unverified"):
        document = bank_document(content=text, provenance_status=provenance)
        context = await prepare_context(company(), event(), [], MockAnalyticsSource([document]),
                                        AgentConfig(max_article_chars=limit), bank_query="ключевая ставка")
        return document, context

    def check_exact_scope(self, text, context, limit):
        item = context.evidence["bank:100:excerpt"]
        self.assertIn(item.quote, text)
        self.assertLessEqual(len(item.quote), limit)
        self.assertTrue(item.text_truncated)
        visible = next(value for value in context.payload["evidence"] if value["id"] == item.id)
        self.assertEqual(visible["quote"], item.quote)
        self.assertTrue(visible["text_truncated"])
        self.assertIn("BANK_SOURCE_TRUNCATED", context.payload["warnings"])
        return item

    async def test_relevant_late_paragraph_retains_period_abbreviation_and_adjacent_condition(self):
        introduction = "Техническая информация об оформлении обзора. " * 50
        forecast = "На конец 2027 г. ключевая ставка в сценарии составляет восемь процентов."
        caveat = "Этот прогноз действует только при замедлении инфляции и не описывает текущую ставку."
        text = introduction + "\n\n" + forecast + "\n\n" + caveat
        document, context = await self.prepare(text)
        item = self.check_exact_scope(text, context, 400)
        self.assertIn(forecast, item.quote)
        self.assertIn(caveat, item.quote)
        self.assertEqual(item.provenance_status, "unverified")
        self.assertEqual(item.source_hash, document.source_hash)
        self.assertEqual(item.source_page, document.source_page)
        self.assertEqual(item.published_at, document.article.published_at)

    async def test_single_long_paragraph_keeps_whole_sentences_around_target(self):
        before = "Условия сценария требуют проверки инфляционного давления."
        target = "Ключевая ставка в прогнозе на конец будущего года может снизиться."
        after = "Однако при ускорении инфляции этот сценарий не реализуется."
        text = "Техническая информация о выпуске. " * 50 + before + " " + target + " " + after
        _, context = await self.prepare(text, 300)
        item = self.check_exact_scope(text, context, 300)
        self.assertIn(before, item.quote)
        self.assertIn(target, item.quote)
        self.assertIn(after, item.quote)

    async def test_oversized_adjacent_condition_blocks_quote_instead_of_cherry_picking(self):
        target = "Ключевая ставка в прогнозе может снизиться."
        caveat = "Однако это возможно только при " + "одновременном соблюдении всех перечисленных условий " * 12 + "."
        text = "Вводная информация. " * 40 + "\n\n" + target + "\n\n" + caveat
        _, context = await self.prepare(text, 250)
        self.assertNotIn("bank:100:excerpt", context.evidence)
        self.assertFalse(any(item.is_bank_analytics for item in context.sources))
        self.assertIn("BANK_SOURCE_CONTEXT_DOES_NOT_FIT", context.payload["warnings"])

    async def test_long_sentence_is_not_cut_at_a_favourable_clause(self):
        text = ("Вводная информация. " * 40 + "\n\nКлючевая ставка может снизиться; однако "
                + "при выполнении дополнительных существенных условий " * 12 + ".")
        _, context = await self.prepare(text, 250)
        self.assertNotIn("bank:100:excerpt", context.evidence)
        self.assertIn("BANK_SOURCE_CONTEXT_DOES_NOT_FIT", context.warnings)

    async def test_short_source_remains_entire_exact_text_with_original_provenance(self):
        text = "На конец будущего года ожидается сохранение ключевой ставки. Это условный прогноз."
        document, context = await self.prepare(text, provenance="verified")
        item = context.evidence["bank:100:excerpt"]
        self.assertEqual(item.quote, text)
        self.assertFalse(item.text_truncated)
        self.assertEqual(item.provenance_status, document.provenance_status)
        self.assertNotIn("BANK_SOURCE_TRUNCATED", context.warnings)

    async def test_partial_quote_scope_survives_public_projection_and_saved_question(self):
        text=("Вводная информация. " * 100 + "\n\nОжидается сохранение ключевой ставки до конца года. "
              "Прогноз действует только при замедлении инфляции.")
        client=FakeLLMClient(json.dumps(draft("bank:100:excerpt"),ensure_ascii=False))
        core=AgentInsightEngine(client,analytics_source=MockAnalyticsSource([bank_document(content=text)]),
                               config=AgentConfig(max_article_chars=400))
        analysis=await core.generate_analysis(company(),event(),[],RecordingTracer())
        self.assertIn("[частичная выдержка]",analysis.insight.bank_view)
        self.assertIn("Прогноз действует только при замедлении инфляции.",analysis.insight.bank_view)
        answer=await ManagementActionEngine(core).answer_question("Какова позиция банка?",
            analysis=analysis,company=company(),tracer=RecordingTracer())
        self.assertEqual(answer.status,"complete")
        self.assertIn("частичная выдержка",answer.summary)
        self.assertEqual(len(client.prompts),1)


if __name__ == "__main__":
    unittest.main()
