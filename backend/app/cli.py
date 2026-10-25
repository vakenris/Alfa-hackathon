import argparse
import asyncio
import csv
import json
from dataclasses import asdict
from pathlib import Path

from backend.app.core.logging import setup_logging
from backend.app.db.session import (
    async_session_factory,
    engine,
    init_db,
)
from backend.app.ingestion.companies import (
    read_companies_csv,
    save_companies,
)
from backend.app.ingestion.seeder import seed_data


async def run_command(args: argparse.Namespace) -> None:
    try:
        if args.command in {"seed", "seed-demo"}:
            await init_db()

            async with async_session_factory() as session:
                await seed_data(session)

            return

        companies, report = read_companies_csv(
            args.companies
        )

        if not args.dry_run:
            await init_db()

            async with async_session_factory() as session:
                await save_companies(
                    session,
                    companies,
                    report,
                )

        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    **asdict(report),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    finally:
        await engine.dispose()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Импорт данных Alfa Analytics"
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    commands.add_parser(
        "seed",
        aliases=["seed-demo"],
        help="Загрузить демоданные",
    )

    companies = commands.add_parser(
        "seed-data",
        help="Загрузить CSV компаний",
    )

    companies.add_argument(
        "--companies",
        required=True,
        type=Path,
    )

    companies.add_argument(
        "--dry-run",
        action="store_true",
        help="Проверить CSV без записи в БД",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_command(args))

    except (OSError, ValueError, csv.Error) as error:
        parser.exit(
            1,
            f"Ошибка: {error}\n",
        )


if __name__ == "__main__":
    main()