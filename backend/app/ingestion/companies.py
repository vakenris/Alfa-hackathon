"""Load company profiles from the hackathon CSV."""

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.company import Company
from backend.app.ingestion.normalizer import (
    normalize_text,
    parse_decimal,
    parse_flag,
    parse_identifier,
)


TEXT_FIELDS = {
    "ORG_TYPE": "org_type",
    "OKFS_NAME": "okfs_name",
    "COMPANY_AGE_YEARS_GRADATION": "company_age_years",
    "ECO_MARK_LABEL": "eco_mark_label",
    "MAINOKVED_CODE": "mainokved_code",
    "MAINOKVED_NAME": "mainokved_name",
    "AMOUNT_WORKERS_FNS_GRADATION": "amount_workers_fns",
    "AMOUNT_WORKERS_SPARK_GRADATION": "amount_workers_spark",
    "CL_TYPE": "cl_type",
    "FTS_COUNTRY_NAME": "fts_country_name",
    "GROUP_ID": "group_id",
    "CITY": "city",
    "REGION": "region",
}

FLAG_FIELDS = {
    "NEGATIVEMARK": "negative_mark",
    "CL_PRODUCT_RKO_MARK": "cl_product_rko_mark",
    "CL_PRODUCT_GAR_MARK": "cl_product_gar_mark",
    "CL_PRODUCT_LOA_MARK": "cl_product_loa_mark",
    "CL_PRODUCT_LEA_MARK": "cl_product_lea_mark",
    "CL_PRODUCT_DEP_MARK": "cl_product_dep_mark",
    "CL_PRODUCT_ZPP_MARK": "cl_product_zpp_mark",
    "FTS_FLAG": "fts_flag",
    "FACTORING_MARK": "factoring_mark",
}

REQUIRED_COLUMNS = {
    "SYNTHETIC_U",
    "REVENUE_COMPANY",
    *TEXT_FIELDS,
    *FLAG_FIELDS,
}


@dataclass
class ImportReport:
    read_rows: int = 0
    accepted: int = 0
    duplicate_rows: int = 0
    rejected_rows: int = 0
    conflicting_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0


def normalize_company(row: dict[str, str]) -> dict:
    uid = parse_identifier(
        row["SYNTHETIC_U"],
        field_name="SYNTHETIC_U",
    )

    values = {
        "synthetic_u": uid,
        "name": f"Компания {uid}",
    }

    values.update(
        {
            target: normalize_text(row[source])
            for source, target in TEXT_FIELDS.items()
        }
    )

    for source, target in FLAG_FIELDS.items():
        value = parse_flag(row[source])

        if value is None:
            raise ValueError(
                f"Не заполнен флаг {source}"
            )

        values[target] = value

    revenue = parse_decimal(row["REVENUE_COMPANY"])

    if revenue is None or revenue < 0:
        raise ValueError(
            "REVENUE_COMPANY должна быть неотрицательным числом"
        )

    # Текущая модель БД использует Float.
    # Значения, теряющие точность при таком хранении, отклоняем.
    amount = float(revenue)

    if Decimal.from_float(amount) != revenue:
        raise ValueError(
            "Для такой точности REVENUE_COMPANY требуется Numeric"
        )

    values["revenue_company"] = amount

    for name, value in values.items():
        column = Company.__table__.columns[name]
        maximum = getattr(column.type, "length", None)

        if isinstance(value, str) and maximum and len(value) > maximum:
            raise ValueError(
                f"Поле {name} длиннее {maximum} символов"
            )

    return values


def read_companies_csv(
    path: str | Path,
) -> tuple[list[dict], ImportReport]:
    report = ImportReport()
    candidates = {}
    blocked = set()

    with Path(path).open(
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
                "В CSV повторяются заголовки столбцов"
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
                    f"Неверное число полей в строке {reader.line_num}"
                )

            try:
                uid = parse_identifier(
                    row["SYNTHETIC_U"],
                    field_name="SYNTHETIC_U",
                )
            except ValueError as error:
                report.rejected_rows += 1
                report.errors.append(
                    f"Строка {reader.line_num}: {error}"
                )
                continue

            if uid in blocked:
                report.rejected_rows += 1

            elif uid not in candidates:
                candidates[uid] = [
                    row,
                    reader.line_num,
                    1,
                ]

            elif candidates[uid][0] == row:
                candidates[uid][2] += 1

            else:
                previous = candidates.pop(uid)
                blocked.add(uid)

                report.conflicting_ids.append(uid)
                report.rejected_rows += previous[2] + 1

                report.errors.append(
                    f"Конфликтующие профили для {uid}; "
                    "все его строки отклонены"
                )

    companies = []

    for row, line_number, count in candidates.values():
        try:
            companies.append(
                normalize_company(row)
            )
        except (ValueError, TypeError) as error:
            report.rejected_rows += count
            report.errors.append(
                f"Строка {line_number}: {error}"
            )
        else:
            report.accepted += 1
            report.duplicate_rows += count - 1

    return companies, report


async def save_companies(
    session: AsyncSession,
    companies: list[dict],
    report: ImportReport,
    batch_size: int = 500,
) -> ImportReport:
    if not 1 <= batch_size <= 1000:
        raise ValueError(
            "batch_size должен быть от 1 до 1000"
        )

    dialect = session.get_bind().dialect.name

    insert = {
        "sqlite": sqlite_insert,
        "postgresql": pg_insert,
    }.get(dialect)

    if insert is None:
        raise ValueError(
            f"Неподдерживаемая база данных: {dialect}"
        )

    statement = insert(Company.__table__)

    statement = statement.on_conflict_do_update(
        index_elements=["synthetic_u"],
        set_={
            column.name: statement.excluded[column.name]
            for column in Company.__table__.columns
            if column.name not in {
                "id",
                "synthetic_u",
                "name",
            }
        },
    )

    inserted = 0
    updated = 0

    try:
        for offset in range(0, len(companies), batch_size):
            batch = companies[offset:offset + batch_size]

            identifiers = [
                item["synthetic_u"]
                for item in batch
            ]

            result = await session.execute(
                select(Company.synthetic_u).where(
                    Company.synthetic_u.in_(identifiers)
                )
            )

            existing = set(result.scalars())

            inserted += len(batch) - len(existing)
            updated += len(existing)

            await session.execute(
                statement,
                batch,
            )

        await session.commit()

    except Exception:
        await session.rollback()
        raise

    report.inserted = inserted
    report.updated = updated

    return report