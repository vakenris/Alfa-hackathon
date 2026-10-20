import asyncio
import sys
from backend.app.db.session import init_db, async_session_factory
from backend.app.ingestion.seeder import seed_data
from backend.app.core.logging import logger, setup_logging


async def main():
    setup_logging()
    command = sys.argv[1] if len(sys.argv) > 1 else "seed"

    if command == "seed":
        logger.info("Initializing database schema...")
        await init_db()
        logger.info("Seeding demo data...")
        async with async_session_factory() as session:
            await seed_data(session)
        logger.info("Seed command completed successfully.")
    else:
        logger.error(f"Unknown command: {command}")


if __name__ == "__main__":
    asyncio.run(main())
