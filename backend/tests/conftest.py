import pytest
import pytest_asyncio
import httpx
from backend.app.main import app
from backend.app.db.session import init_db, async_session_factory
from backend.app.ingestion.seeder import seed_data


@pytest_asyncio.fixture(scope="session", autouse=True)
async def prepare_database():
    await init_db()
    async with async_session_factory() as session:
        await seed_data(session)


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
