import argparse
import asyncio
import csv
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from backend.app.core.logging import setup_logging
from backend.app.db.session import async_session_factory, engine, init_db
from backend.app.db.repositories.bank_analytics_repo import (
    BankAnalyticsRepository, page_is_reviewed,
)
from backend.app.ingestion.analytics import read_analytics, save_analytics
from backend.app.ingestion.analytics_source import BankPdfAnalyticsSource
from backend.app.ingestion.companies import read_companies_csv, save_companies
from backend.app.ingestion.payments import check_payments, import_payments
from backend.app.ingestion.person_links import read_person_links, import_person_links
from backend.app.ingestion.seeder import seed_data


async def run_command(args: argparse.Namespace) -> None:
    try:
        if args.command in {"seed", "seed-demo"}:
            await init_db()
            async with async_session_factory() as session:
                await seed_data(session)
            return

        if args.command == "search-analytics":
            async with async_session_factory() as session:
                hits = await BankAnalyticsRepository(session).search(
                    args.query, as_of=args.as_of, limit=args.limit,
                    include_historical=args.include_historical,
                    reviewed_only=not args.include_unreviewed,
                )
            print(json.dumps({
                "coverage": (
                    "newest_300_dated_bank_pdf_pages_preview_first_50000_chars"
                    if args.include_unreviewed else BankPdfAnalyticsSource.coverage
                ),
                "reviewed_only": not args.include_unreviewed,
                "as_of": args.as_of.isoformat(),
                "items": [{
                    "document_id": hit.document_id,
                    "source_file": hit.source_file,
                    "title": hit.title,
                    "published_on": hit.published_on.isoformat(),
                    "page_number": hit.page_number,
                    "score": hit.score,
                    "provenance_status": hit.provenance_status,
                    "extraction_hash": hit.extraction_hash,
                    "excerpt": hit.text[:1200],
                } for hit in hits],
            }, ensure_ascii=False, indent=2))
            return

        if args.command == "show-analytics":
            async with async_session_factory() as session:
                document, pages = await BankAnalyticsRepository(session).get_document(args.document)
                if args.page is not None and not any(page.page_number == args.page for page in pages):
                    raise ValueError("Указанной страницы нет в документе")
                print(json.dumps({
                    "document_id": document.id,
                    "source_file": document.source_file,
                    "title": document.title,
                    "page_count": document.page_count,
                    "extraction_hash": document.extraction_hash,
                    "provenance_status": document.provenance_status,
                    "requires_review": document.requires_review,
                    "reviewed_pages": [page.page_number for page in pages if page_is_reviewed(document, page)],
                }, ensure_ascii=False, indent=2))
                for page in pages:
                    if args.page is None or page.page_number == args.page:
                        status = "verified" if page_is_reviewed(document, page) else "unverified"
                        print(f"\n=== Страница {page.page_number}: {status} ===\n{page.text}")
            return

        if args.command == "review-analytics":
            async with async_session_factory() as session:
                report = await BankAnalyticsRepository(session).mark_reviewed(
                    args.document, args.extraction_hash, args.pages
                )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        if args.command == "seed-data":
            companies, report = read_companies_csv(args.companies)
            if not args.dry_run:
                await init_db()
                async with async_session_factory() as session:
                    await save_companies(session, companies, report)

        elif args.command == "ingest-payments":
            if args.dry_run:
                report = check_payments(args.payments, args.exclude_company)
            else:
                await init_db()
                async with async_session_factory() as session:
                    report = await import_payments(
                        session, args.payments, args.exclude_company
                    )

        elif args.command == "ingest-links":
            if args.dry_run:
                _, report = read_person_links(args.links)
            else:
                await init_db()
                async with async_session_factory() as session:
                    report = await import_person_links(session, args.links)

        elif args.command == "ingest-analytics":
            documents, report = read_analytics(
                args.manifest, args.pdf_dir, args.extracted_dir
            )
            if not args.dry_run:
                await init_db()
                async with async_session_factory() as session:
                    await save_analytics(session, documents, report)

        else:
            raise ValueError(f"Неизвестная команда: {args.command}")

        print(json.dumps(
            {"dry_run": args.dry_run, **asdict(report)},
            ensure_ascii=False,
            indent=2,
        ))
    finally:
        await engine.dispose()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Импорт данных Alfa Analytics")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("seed", aliases=["seed-demo"], help="Загрузить демоданные")

    companies = commands.add_parser("seed-data", help="Загрузить CSV компаний")
    companies.add_argument("--companies", required=True, type=Path)
    companies.add_argument(
        "--dry-run", action="store_true", help="Проверить CSV без записи в БД"
    )

    payments = commands.add_parser("ingest-payments", help="Загрузить CSV платежей")
    payments.add_argument("--payments", required=True, type=Path)
    payments.add_argument("--exclude-company", action="append", default=[])
    payments.add_argument(
        "--dry-run", action="store_true", help="Проверить CSV без записи в БД"
    )

    links = commands.add_parser("ingest-links", help="Загрузить связи физлиц и компаний")
    links.add_argument("--links", required=True, type=Path)
    links.add_argument("--dry-run", action="store_true", help="Проверить CSV без записи в БД")

    analytics = commands.add_parser(
        "ingest-analytics", help="Загрузить текст банковских PDF"
    )
    analytics.add_argument(
        "--manifest", type=Path, default=Path("data/bank_analytics_manifest.json")
    )
    analytics.add_argument(
        "--pdf-dir", type=Path, default=Path("data/raw/bank_analytics")
    )
    analytics.add_argument(
        "--extracted-dir", type=Path, default=Path("data/processed/bank_analytics")
    )
    analytics.add_argument(
        "--dry-run", action="store_true", help="Проверить корпус без записи в БД"
    )

    search = commands.add_parser("search-analytics", help="Найти страницы банковских PDF")
    search.add_argument("--query", required=True)
    search.add_argument("--as-of", required=True, type=date.fromisoformat)
    search.add_argument("--limit", type=int, default=3)
    search.add_argument("--include-historical", action="store_true")
    search.add_argument(
        "--include-unreviewed", action="store_true",
        help="Предпросмотр сырого OCR для оператора; агент его не использует",
    )

    show = commands.add_parser("show-analytics", help="Показать текст документа для сверки")
    show.add_argument("--document", required=True)
    show.add_argument("--page", type=int)

    review = commands.add_parser("review-analytics", help="Зафиксировать ручную сверку PDF и OCR")
    review.add_argument("--document", required=True)
    review.add_argument("--extraction-hash", required=True)
    review.add_argument("--pages", nargs="+", type=int, required=True)

    args = parser.parse_args()
    try:
        asyncio.run(run_command(args))
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()