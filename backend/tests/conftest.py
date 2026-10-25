import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest_asyncio
from sqlalchemy import event, select


# Настройки должны быть заданы до импорта приложения.
_test_directory = TemporaryDirectory(
    prefix="alfa-tests-",
    dir=Path(__file__).resolve().parent,
)

_test_database = Path(_test_directory.name) / "test.db"

os.environ["DATABASE_URL"] = (
    f"sqlite+aiosqlite:///{_test_database.as_posix()}"
)
os.environ["USE_IN_MEMORY_BUS"] = "true"
os.environ["AUTO_SEED_DEMO"] = "false"


from backend.app.main import app
from backend.app.db.base import Base
from backend.app.db.session import async_session_factory, engine
from backend.app.db.models.agent_trace import AgentTraceStep, JobRecord
from backend.app.db.models.company import Company
from backend.app.db.models.insight import Evidence, Insight
from backend.app.db.models.news import NewsArticle
from backend.app.ingestion.seeder import seed_data


@event.listens_for(engine.sync_engine, "connect")
def enable_foreign_keys(connection, _record):
    cursor = connection.cursor()

    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def prepare_database():
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all
            )

        async with async_session_factory() as session:
            await seed_data(session)

            company = await session.scalar(
                select(Company).where(
                    Company.synthetic_u == "U_1001"
                )
            )
            assert company is not None

            article = NewsArticle(
                title=(
                    "Тестовый материал: сохранение ключевой ставки"
                ),
                source="Синтетические тестовые данные",
                content=(
                    "В тестовом сценарии ключевая ставка "
                    "сохранена на уровне 14%."
                ),
                category="Макроэкономика",
                published_at=datetime(2026, 9, 11),
                is_bank_analytics=False,
            )

            session.add(article)
            await session.flush()

            job = JobRecord(
                id="test-job-uuid",
                company_id=company.id,
                event_id=article.id,
                status="SUCCESS",
            )

            session.add(job)
            await session.flush()

            insight = Insight(
                job_id=job.id,
                company_id=company.id,
                event_id=article.id,
                what_happened=(
                    "ЦБ сохранил ставку на уровне 14%"
                ),
                why_important=(
                    "Влияет на обслуживание кредитного портфеля"
                ),
                consequences=(
                    "Стоимость заимствований остаётся высокой"
                ),
                bank_view="Тестовая позиция банка",
                ai_view="Тестовый вывод модели",
                recommended_action=(
                    "Проверить платёжный календарь"
                ),
                relevant_bank_product="Факторинг",
            )

            session.add(insight)
            await session.flush()

            session.add_all(
                [
                    AgentTraceStep(
                        job_id=job.id,
                        step_number=1,
                        step_name="Анализ профиля",
                        description="Профиль загружен",
                    ),
                    Evidence(
                        insight_id=insight.id,
                        claim=(
                            "Тестовая связь новости "
                            "с кредитным профилем"
                        ),
                        source_title=article.title,
                    ),
                ]
            )

            await session.commit()

        yield

    finally:
        await engine.dispose()
        _test_directory.cleanup()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as http_client:
        yield http_client