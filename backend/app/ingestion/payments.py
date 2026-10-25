"""Stream one payment dataset into the database without duplicate imports."""

import csv
import hashlib
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.logging import logger
from backend.app.db.models.company import Company
from backend.app.db.models.transaction import (
    CounterpartyTransaction,
    PaymentImport,
)
from backend.app.ingestion.normalizer import (
    parse_count,
    parse_decimal,
    parse_identifier,
)


DATASET_ID = "hackathon_payments"

REQUIRED_COLUMNS = {
    "PAYER",
    "BENEFICIARY",
    "PAY_AMT",
    "PAY_CNT",
}


@dataclass
class PaymentReport:
    source_hash: str = ""
    excluded_ids: list[str] = field(default_factory=list)
    read_rows: int = 0
    accepted_rows: int = 0
    excluded_rows: int = 0
    self_rows: int = 0
    rejected_rows: int = 0
    missing_profile_ids: int | None = None
    inserted_rows: int = 0
    already_imported: bool = False
    errors: list[str] = field(default_factory=list)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(
            stream,
            "sha256",
        ).hexdigest()


def iter_payments(path: Path, report: PaymentReport):
    excluded = set(report.excluded_ids)

    with path.open(
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        reader = csv.DictReader(
            stream,
            delimiter=";",
            strict=True,
        )

        headers = reader.fieldnames or []

        if len(headers) != len(set(headers)):
            raise ValueError(
                "В CSV повторяются заголовки"
            )

        missing = REQUIRED_COLUMNS - set(headers)

        if missing:
            raise ValueError(
                "В CSV отсутствуют столбцы: "
                + ", ".join(sorted(missing))
            )

        for row in reader:
            report.read_rows += 1

            if None in row or any(
                value is None
                for value in row.values()
            ):
                raise ValueError(
                    f"Неверное число полей в строке "
                    f"{reader.line_num}"
                )

            try:
                payer = parse_identifier(
                    row["PAYER"],
                    field_name="PAYER",
                )

                beneficiary = parse_identifier(
                    row["BENEFICIARY"],
                    field_name="BENEFICIARY",
                )

                if len(payer) > 64 or len(beneficiary) > 64:
                    raise ValueError(
                        "Идентификатор компании длиннее 64 символов"
                    )

                if payer in excluded or beneficiary in excluded:
                    report.excluded_rows += 1
                    continue

                if payer == beneficiary:
                    report.self_rows += 1
                    continue

                amount = parse_decimal(row["PAY_AMT"])
                count = parse_count(row["PAY_CNT"])

                if amount is None or amount < 0:
                    raise ValueError(
                        "PAY_AMT должна быть неотрицательным числом"
                    )

                if count is None or not 1 <= count <= 2147483647:
                    raise ValueError(
                        "PAY_CNT должна быть целым числом "
                        "от 1 до 2147483647"
                    )

                value = float(amount)

                if Decimal.from_float(value) != amount:
                    raise ValueError(
                        "Для такой точности PAY_AMT требуется Numeric"
                    )

            except ValueError as error:
                report.rejected_rows += 1

                if len(report.errors) < 20:
                    report.errors.append(
                        f"Строка {reader.line_num}: {error}"
                    )

                continue

            report.accepted_rows += 1

            yield {
                "payer_u": payer,
                "beneficiary_u": beneficiary,
                "pay_amt": value,
                "pay_cnt": count,
            }


def check_payments(
    path: str | Path,
    excluded_ids=(),
) -> PaymentReport:
    path = Path(path)

    report = PaymentReport(
        source_hash=file_hash(path),
        excluded_ids=sorted(set(excluded_ids)),
    )

    for _ in iter_payments(path, report):
        pass

    if file_hash(path) != report.source_hash:
        raise ValueError(
            "CSV изменился во время проверки"
        )

    return report


async def import_payments(
    session: AsyncSession,
    path: str | Path,
    excluded_ids=(),
    batch_size: int = 1000,
) -> PaymentReport:
    if not 1 <= batch_size <= 10000:
        raise ValueError(
            "batch_size должен быть от 1 до 10000"
        )

    path = Path(path)

    report = PaymentReport(
        source_hash=file_hash(path),
        excluded_ids=sorted(set(excluded_ids)),
    )

    try:
        previous = await session.get(
            PaymentImport,
            DATASET_ID,
        )

        if previous is not None:
            if previous.source_hash != report.source_hash:
                raise ValueError(
                    "В этой БД уже загружен другой файл платежей; "
                    "автоматическая замена не выполняется"
                )

            if previous.stats["excluded_ids"] != report.excluded_ids:
                raise ValueError(
                    "Список исключённых компаний отличается "
                    "от предыдущего импорта"
                )

            return PaymentReport(
                **{
                    **previous.stats,
                    "already_imported": True,
                    "inserted_rows": 0,
                }
            )

        existing = await session.scalar(
            select(CounterpartyTransaction.id).limit(1)
        )

        if existing is not None:
            raise ValueError(
                "В таблице уже есть платежи без записи импорта; "
                "используйте БД с компаниями без демоплатежей"
            )

        result = await session.execute(
            select(Company.synthetic_u)
        )

        known_ids = set(result.scalars())

        if not known_ids:
            raise ValueError(
                "Сначала загрузите компании в эту БД"
            )

        record = PaymentImport(
            id=DATASET_ID,
            source_hash=report.source_hash,
            source_name=path.name,
            stats={},
        )

        session.add(record)
        await session.flush()

        missing_ids = set()
        batch = []

        statement = insert(
            CounterpartyTransaction.__table__
        )

        for payment in iter_payments(path, report):
            for uid in (
                payment["payer_u"],
                payment["beneficiary_u"],
            ):
                if uid not in known_ids:
                    missing_ids.add(uid)

            batch.append(payment)

            if len(batch) >= batch_size:
                await session.execute(
                    statement,
                    batch,
                )

                report.inserted_rows += len(batch)
                batch.clear()

                if report.inserted_rows % 100000 == 0:
                    logger.info(
                        "Загружено платежных строк: %s",
                        report.inserted_rows,
                    )

        if batch:
            await session.execute(
                statement,
                batch,
            )

            report.inserted_rows += len(batch)

        if not report.accepted_rows:
            raise ValueError(
                "В файле нет пригодных для загрузки платежей"
            )

        if file_hash(path) != report.source_hash:
            raise ValueError(
                "CSV изменился во время импорта"
            )

        report.missing_profile_ids = len(missing_ids)
        record.stats = asdict(report)

        await session.commit()

        return report

    except Exception:
        await session.rollback()
        report.inserted_rows = 0
        raise