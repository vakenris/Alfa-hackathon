"""Offline adapter checks; all loaders, sessions and repositories are fakes."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import backend.app.agent.backend_source as source_module
from backend.app.agent.backend_source import BackendAnalyticsSource
from backend.app.agent.models import AnalyticsDocument
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.event import NewsArticleResponse


class BackendAnalyticsSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.company = CompanyResponse(id=1, synthetic_u="TEST", name="Завод")
        self.event = self.article(100, "Событие", bank=False)

    def article(self, article_id, title, *, content="", bank=True, date=None):
        return NewsArticleResponse(
            id=article_id, title=title, content=content, source="Название источника",
            published_at=date or datetime(2026, 10, 21, tzinfo=timezone.utc),
            category="Общее", is_bank_analytics=bank,
        )

    def document(self, article_id, title, **kwargs):
        return AnalyticsDocument(article=self.article(article_id, title, **kwargs))

    async def search(self, source, query="", *, event=None, limit=3):
        return await source.search(
            query, company=self.company, event=event or self.event, limit=limit,
        )

    def repository_fixture(self, rows, *, failure=None):
        state = {"opened": 0, "closed": 0, "limits": []}
        session = object()

        @asynccontextmanager
        async def session_factory():
            state["opened"] += 1
            try:
                yield session
            finally:
                state["closed"] += 1

        class FakeNewsRepository:
            def __init__(self, provided_session):
                if provided_session is not session:
                    raise AssertionError("Unexpected session")

            async def list_bank_analytics(self, limit):
                state["limits"].append(limit)
                if failure is not None:
                    raise failure
                return rows

        db = ModuleType("backend.app.db")
        db.__path__ = []
        repositories = ModuleType("backend.app.db.repositories")
        repositories.__path__ = []
        repo = ModuleType("backend.app.db.repositories.news_repo")
        repo.NewsRepository = FakeNewsRepository
        modules = {
            "backend.app.db": db,
            "backend.app.db.repositories": repositories,
            "backend.app.db.repositories.news_repo": repo,
        }
        return session_factory, state, modules

    async def test_blank_generic_intent_does_not_load_random_articles(self):
        loader = AsyncMock(return_value=[self.document(1, "Обзор рынка")])
        source = BackendAnalyticsSource(loader)
        self.assertEqual(await self.search(source, "  "), [])
        loader.assert_not_awaited()

    async def test_unknown_query_and_unrelated_documents_return_empty(self):
        loader = AsyncMock(return_value=[self.document(1, "Перевозки контейнеров")])
        self.assertEqual(await self.search(BackendAnalyticsSource(loader), "квазизорп"), [])
        loader.assert_awaited_once_with(50)

    async def test_query_uses_russian_synonyms_and_inflections(self):
        cases = (
            ("инфляционные риски", "Удорожание кредитования предприятий"),
            ("ставки фрахта", "Контейнерные перевозки: изменение загрузки"),
            ("урожай пшеницы", "Положение аграрного сектора"),
            ("металлургические предприятия", "Спрос на сталь"),
            ("валютные операции", "Динамика юаня"),
            ("налоговые изменения", "Новые акцизы"),
        )
        for query, title in cases:
            with self.subTest(query=query):
                source = BackendAnalyticsSource(AsyncMock(return_value=[self.document(1, title)]))
                self.assertEqual([doc.article.id for doc in await self.search(source, query)], [1])

    async def test_event_title_is_context_even_with_blank_or_unknown_query(self):
        source = BackendAnalyticsSource(AsyncMock(return_value=[
            self.document(1, "Цены на контейнерные перевозки"),
            self.document(2, "Кредитование предприятий"),
        ]))
        event = self.article(100, "Рост ставок фрахта", bank=False)
        for query in ("", "квазизорп"):
            with self.subTest(query=query):
                self.assertEqual([doc.article.id for doc in await self.search(source, query, event=event)], [1])

    async def test_freight_rates_do_not_match_key_rate_on_word_rate_alone(self):
        source = BackendAnalyticsSource(AsyncMock(return_value=[
            self.document(1, "Ключевая ставка Центрального банка"),
            self.document(2, "Рост ставок фрахта"),
        ]))
        self.assertEqual([doc.article.id for doc in await self.search(source, "Фрахтовые ставки")], [2])

    async def test_unicode_case_and_yo_are_normalized(self):
        source = BackendAnalyticsSource(AsyncMock(return_value=[
            self.document(1, "ЗАЁМНЫЕ СРЕДСТВА"),
            self.document(2, "ＬＩＴＨＩＵＭ"),
        ]))
        self.assertEqual([doc.article.id for doc in await self.search(source, "заемное финансирование")], [1])
        self.assertEqual([doc.article.id for doc in await self.search(source, "lithium")], [2])

    async def test_scoring_uses_content_as_well_as_title(self):
        source = BackendAnalyticsSource(AsyncMock(return_value=[
            self.document(1, "Отраслевой отчет", content="Дефицит железнодорожных вагонов."),
            self.document(2, "Депозитные операции"),
        ]))
        self.assertEqual([doc.article.id for doc in await self.search(source, "логистика")], [1])

    async def test_non_bank_articles_are_excluded(self):
        source = BackendAnalyticsSource(AsyncMock(return_value=[
            self.document(1, "Инфляция", bank=False), self.document(2, "Инфляция"),
        ]))
        self.assertEqual([doc.article.id for doc in await self.search(source, "инфляция")], [2])

    async def test_loader_and_result_count_are_bounded(self):
        loader = AsyncMock(return_value=[self.document(i, "Налоговая политика") for i in range(1, 60)])
        source = BackendAnalyticsSource(loader, candidate_limit=4)
        result = await self.search(source, "налоги", limit=2)
        loader.assert_awaited_once_with(4)
        self.assertEqual([doc.article.id for doc in result], [1, 2])
        self.assertEqual(source.coverage, "newest_bounded_bank_articles")
        self.assertEqual(source.candidate_limit, 4)

    async def test_documents_beyond_candidate_window_are_not_ranked(self):
        source = BackendAnalyticsSource(AsyncMock(return_value=[
            self.document(1, "Урожай зерна"), self.document(2, "Налоги"),
        ]), candidate_limit=1)
        self.assertEqual(await self.search(source, "налоги"), [])

    async def test_dates_break_ties_deterministically_without_age_cutoff(self):
        old = datetime(2001, 1, 1)  # Repository naive timestamp is treated as UTC.
        recent = datetime(2026, 10, 20, tzinfo=timezone.utc)
        docs = [
            self.document(3, "Налоговая политика", date=old),
            self.document(2, "Налоговая политика", date=recent),
            self.document(1, "Налоговая политика", date=recent),
        ]
        source = BackendAnalyticsSource(AsyncMock(return_value=docs))
        self.assertEqual([doc.article.id for doc in await self.search(source, "налоги")], [1, 2, 3])

    async def test_duplicate_ids_are_not_returned_twice(self):
        doc = self.document(1, "Инфляция")
        source = BackendAnalyticsSource(AsyncMock(return_value=[doc, doc]))
        self.assertEqual(len(await self.search(source, "инфляция")), 1)

    async def test_returned_documents_do_not_mutate_loader_objects(self):
        doc = self.document(1, "Инфляция")
        source = BackendAnalyticsSource(AsyncMock(return_value=[doc]))
        result = await self.search(source, "инфляция")
        result[0].article.content = "Changed"
        self.assertEqual(doc.article.content, "")

    async def test_empty_result_and_loader_failure_are_distinct(self):
        self.assertEqual(await self.search(BackendAnalyticsSource(AsyncMock(return_value=[])), "налоги"), [])
        source = BackendAnalyticsSource(AsyncMock(side_effect=RuntimeError("Repository unavailable")))
        with self.assertRaisesRegex(RuntimeError, "Repository unavailable"):
            await self.search(source, "налоги")

    async def test_invalid_loader_result_is_failure_not_empty_search(self):
        for returned in (None, {}, [None]):
            with self.subTest(returned=returned):
                with self.assertRaises((TypeError, ValueError)):
                    await self.search(BackendAnalyticsSource(AsyncMock(return_value=returned)), "налоги")

    async def test_constructor_and_search_validate_bounds(self):
        for candidate_limit in (0, 51, True, "5"):
            with self.subTest(candidate_limit=candidate_limit):
                with self.assertRaises(ValueError):
                    BackendAnalyticsSource(AsyncMock(), candidate_limit=candidate_limit)
        source = BackendAnalyticsSource(AsyncMock())
        for limit in (0, -1, True, "1"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    await self.search(source, "налоги", limit=limit)

    async def test_module_and_repository_factory_do_not_import_db_or_open_session(self):
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith(("backend.app.db", "backend.app.core")):
                raise AssertionError("Database/settings imports forbidden before search")
            return original_import(name, *args, **kwargs)

        factory = Mock(side_effect=AssertionError("Session must stay closed"))
        with patch("builtins.__import__", side_effect=guarded_import):
            importlib.reload(source_module)
            source = source_module.BackendAnalyticsSource.from_repository_factory(factory)
            self.assertEqual(await self.search(source, ""), [])
        factory.assert_not_called()

    async def test_repository_bridge_reads_with_bounded_limit_and_defaults_unverified(self):
        article = self.article(1, "Инфляция", content="Ссылка в тексте: https://untrusted.invalid/report")
        row = SimpleNamespace(**article.model_dump(mode="python"))
        factory, state, modules = self.repository_fixture([row])
        source = BackendAnalyticsSource.from_repository_factory(factory)
        self.assertEqual(state["opened"], 0)
        with patch.dict(sys.modules, modules):
            result = await self.search(source, "инфляция")
        self.assertEqual(state, {"opened": 1, "closed": 1, "limits": [50]})
        self.assertEqual(result[0].provenance_status, "unverified")
        self.assertIsNone(result[0].source_url)
        self.assertIsNone(result[0].source_document_id)
        self.assertIsNone(result[0].source_page)
        self.assertIsNone(result[0].source_hash)

    async def test_trusted_resolver_can_supply_metadata_without_inventing_it(self):
        article = self.article(1, "Инфляция")
        metadata = {
            "source_url": "https://trusted.invalid/report.pdf", "provenance_status": "verified",
            "source_document_id": "supplied-report", "source_page": 7, "source_hash": "known-hash",
        }
        for resolver in (Mock(return_value=metadata), AsyncMock(return_value=metadata)):
            with self.subTest(resolver=type(resolver).__name__):
                factory, _, modules = self.repository_fixture([article])
                source = BackendAnalyticsSource.from_repository_factory(factory, resolver)
                with patch.dict(sys.modules, modules):
                    result = await self.search(source, "инфляция")
                resolver.assert_called_once_with(article)
                self.assertEqual(result[0].provenance_status, "verified")
                self.assertEqual(result[0].source_document_id, "supplied-report")
                self.assertEqual(result[0].source_page, 7)
                self.assertEqual(result[0].source_hash, "known-hash")
                self.assertEqual(str(result[0].source_url), metadata["source_url"])

    async def test_resolver_may_not_replace_article_or_add_unknown_metadata(self):
        for metadata in ({"article": {}}, {"url_to_fetch": "https://untrusted.invalid"}, []):
            with self.subTest(metadata=metadata):
                factory, state, modules = self.repository_fixture([self.article(1, "Инфляция")])
                source = BackendAnalyticsSource.from_repository_factory(factory, lambda row: metadata)
                with patch.dict(sys.modules, modules):
                    with self.assertRaisesRegex(ValueError, "Invalid provenance"):
                        await self.search(source, "инфляция")
                self.assertEqual(state["closed"], 1)

    async def test_repository_failure_propagates_and_closes_fake_session(self):
        factory, state, modules = self.repository_fixture([], failure=RuntimeError("Unavailable"))
        source = BackendAnalyticsSource.from_repository_factory(factory)
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, "Unavailable"):
                await self.search(source, "инфляция")
        self.assertEqual(state["closed"], 1)


if __name__ == "__main__":
    unittest.main()
