import argparse
import asyncio
import importlib
import inspect

from backend.app.core.config import settings
from backend.app.core.logging import logger, setup_logging
from backend.app.db.session import (
    async_session_factory,
    engine,
    init_db,
)
from backend.app.ml.interface import MockMLInsightEngine
from backend.app.worker.config import worker_settings
from backend.app.worker.queue import DatabaseJobQueue
from backend.app.worker.tasks import execute_job, set_ml_engine


def load_engine(mock: bool):
    if mock:
        logger.warning(
            "MOCK worker: synthetic results, no real LLM analysis"
        )
        return MockMLInsightEngine()

    path = worker_settings.engine_factory

    if not path or ":" not in path:
        raise ValueError(
            "Set WORKER_ENGINE_FACTORY=module:function, "
            "or use --mock for a demo"
        )

    module_name, function_name = path.split(":", 1)

    factory = getattr(
        importlib.import_module(module_name),
        function_name,
    )

    instance = factory()

    if not inspect.iscoroutinefunction(
        getattr(instance, "generate_insight", None)
    ):
        raise ValueError(
            "Engine must provide async generate_insight"
        )

    if isinstance(instance, MockMLInsightEngine):
        raise ValueError(
            "Mock engine requires explicit --mock"
        )

    return instance


async def run(
    *,
    once: bool,
    mock: bool,
    job_id: str | None = None,
):
    try:
        if worker_settings.mode != "database":
            raise ValueError(
                "Set WORKER_MODE=database "
                "in BOTH API and worker processes"
            )

        if settings.USE_IN_MEMORY_BUS:
            raise ValueError(
                "External worker requires "
                "USE_IN_MEMORY_BUS=false and Redis for SSE"
            )

        set_ml_engine(load_engine(mock))
        await init_db()

        queue = DatabaseJobQueue(async_session_factory)

        logger.info(
            "Database worker started; one job at a time"
        )

        while True:
            await queue.expire_stale()
            job = await queue.claim(job_id=job_id)

            if job is not None:
                try:
                    await execute_job(job)
                except Exception:
                    logger.exception(
                        "Job %s could not be finalized; retained in DB",
                        job.id,
                    )

            if once or job_id is not None:
                return

            if job is None:
                await asyncio.sleep(
                    worker_settings.poll_seconds
                )

    finally:
        await engine.dispose()


def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Persistent insight worker"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one waiting job",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Explicit demo engine; synthetic output",
    )
    parser.add_argument(
        "--job-id",
        help="Process only this PENDING job and exit",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run(
            once=args.once,
            mock=args.mock,
            job_id=args.job_id,
        ))
    except KeyboardInterrupt:
        pass
    except (
        ValueError,
        ImportError,
        AttributeError,
        TypeError,
    ) as error:
        parser.exit(1, f"Worker error: {error}\n")


if __name__ == "__main__":
    main()