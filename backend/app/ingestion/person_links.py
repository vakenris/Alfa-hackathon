"""Import anonymized person/company links without inferring ownership."""

import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.company import Company
from backend.app.db.models.person_link import PersonCompanyLink, PersonLinkImport
from backend.app.ingestion.normalizer import normalize_text, parse_identifier


DATASET_ID = "hackathon_person_links"
COLUMNS = {"SYNTHETIC_F", "SYNTHETIC_U", "CITY_NAME", "RC_NAME"}
MAX_BYTES = 20 * 1024 * 1024


@dataclass
class PersonLinkReport:
    source_hash: str = ""
    read_rows: int = 0
    accepted_rows: int = 0
    duplicate_rows: int = 0
    people: int = 0
    company_ids: int = 0
    conflicting_location_people: int = 0
    missing_location_rows: int = 0
    missing_profile_ids: int | None = None
    inserted_rows: int = 0
    already_imported: bool = False


def read_person_links(
    path: str | Path,
) -> tuple[list[dict], PersonLinkReport]:
    path = Path(path)

    if len(path.name) > 255:
        raise ValueError("Имя CSV длиннее 255 символов")

    with path.open("rb") as stream:
        payload = stream.read(MAX_BYTES + 1)

    if len(payload) > MAX_BYTES:
        raise ValueError("CSV связей превышает 20 МБ")

    report = PersonLinkReport(
        source_hash=hashlib.sha256(payload).hexdigest()
    )

    reader = csv.DictReader(
        io.StringIO(payload.decode("utf-8-sig"), newline=""),
        delimiter=";",
        strict=True,
    )

    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or set(headers) != COLUMNS:
        raise ValueError(
            "Нужны ровно столбцы "
            "SYNTHETIC_F, SYNTHETIC_U, CITY_NAME, RC_NAME"
        )

    links = {}
    locations = defaultdict(set)

    for row in reader:
        report.read_rows += 1

        if None in row or any(value is None for value in row.values()):
            raise ValueError(
                f"Неверное число полей в строке {reader.line_num}"
            )

        try:
            fid = parse_identifier(
                row["SYNTHETIC_F"],
                field_name="SYNTHETIC_F",
            )
            uid = parse_identifier(
                row["SYNTHETIC_U"],
                field_name="SYNTHETIC_U",
            )
            city = normalize_text(row["CITY_NAME"])
            region = normalize_text(row["RC_NAME"])

            if len(fid) > 64 or len(uid) > 64:
                raise ValueError("Идентификатор длиннее 64 символов")

            if any(
                value is not None and len(value) > 128
                for value in (city, region)
            ):
                raise ValueError("Город или регион длиннее 128 символов")

        except ValueError as error:
            raise ValueError(
                f"Строка {reader.line_num}: {error}"
            ) from error

        key = (fid, uid)
        location = (city, region)

        if key in links:
            existing_location = (
                links[key]["city"],
                links[key]["region"],
            )

            if existing_location != location:
                raise ValueError(
                    f"Разные адреса для одной пары {fid}/{uid}; "
                    "импорт отменён"
                )

            report.duplicate_rows += 1
            continue

        links[key] = {
            "synthetic_f": fid,
            "synthetic_u": uid,
            "city": city,
            "region": region,
        }
        locations[fid].add(location)

    if not links:
        raise ValueError("В CSV нет связей")

    records = list(links.values())

    for item in records:
        item["location_conflict"] = (
            len(locations[item["synthetic_f"]]) > 1
        )

    report.accepted_rows = len(records)
    report.people = len(locations)
    report.company_ids = len({
        item["synthetic_u"] for item in records
    })
    report.conflicting_location_people = sum(
        len(value) > 1 for value in locations.values()
    )
    report.missing_location_rows = sum(
        item["city"] is None or item["region"] is None
        for item in records
    )

    return records, report


async def import_person_links(
    session: AsyncSession,
    path: str | Path,
    batch_size: int = 500,
) -> PersonLinkReport:
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size должен быть от 1 до 1000")

    records, report = read_person_links(path)

    try:
        previous = await session.get(PersonLinkImport, DATASET_ID)

        if previous is not None:
            if previous.source_hash != report.source_hash:
                raise ValueError(
                    "В БД уже загружен другой CSV связей; "
                    "автоматическая замена запрещена"
                )

            return PersonLinkReport(**{
                **previous.stats,
                "already_imported": True,
                "inserted_rows": 0,
            })

        existing = await session.scalar(
            select(PersonCompanyLink.synthetic_f).limit(1)
        )
        if existing is not None:
            raise ValueError(
                "В БД есть связи без записи импорта"
            )

        known_ids = set(
            (
                await session.scalars(
                    select(Company.synthetic_u)
                )
            ).all()
        )

        if not known_ids:
            raise ValueError(
                "Сначала загрузите компании в эту БД"
            )

        report.missing_profile_ids = len(
            {item["synthetic_u"] for item in records} - known_ids
        )

        record = PersonLinkImport(
            id=DATASET_ID,
            source_hash=report.source_hash,
            source_name=Path(path).name,
            stats={},
        )
        session.add(record)
        await session.flush()

        for offset in range(0, len(records), batch_size):
            batch = records[offset:offset + batch_size]

            await session.execute(
                insert(PersonCompanyLink.__table__),
                batch,
            )
            report.inserted_rows += len(batch)

        record.stats = asdict(report)
        await session.commit()

        return report

    except Exception:
        await session.rollback()
        raise