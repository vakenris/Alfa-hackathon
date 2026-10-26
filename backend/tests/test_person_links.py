import csv
from dataclasses import asdict

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Insert

from backend.app.db.base import Base
from backend.app.db.models.company import Company
from backend.app.db.models.person_link import PersonCompanyLink, PersonLinkImport
from backend.app.db.repositories.person_link_repo import PersonLinkRepository
from backend.app.ingestion.person_links import import_person_links, read_person_links


def write_csv(tmp_path, rows):
    path = tmp_path / "links.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([
            "SYNTHETIC_F", "CITY_NAME", "RC_NAME", "SYNTHETIC_U",
        ])
        writer.writerows(rows)
    return path


@pytest_asyncio.fixture
async def link_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[
                    Company.__table__,
                    PersonCompanyLink.__table__,
                    PersonLinkImport.__table__,
                ],
            )

        async with async_sessionmaker(
            engine, expire_on_commit=False
        )() as session:
            session.add(Company(
                synthetic_u="U_1",
                name="Компания",
                city="Город компании",
            ))
            await session.commit()
            yield session
    finally:
        await engine.dispose()


def test_parser_preserves_ids_missing_values_and_conflicting_locations(tmp_path):
    path = write_csv(tmp_path, [
        ["F_001", "Москва", "Центр", "U_1"],
        ["F_001", "Москва", "Центр", "U_1"],
        ["F_001", "Омск", "Сибирь", "U_002"],
        ["F_2", "", "", "U_1"],
    ])
    rows, report = read_person_links(path)

    assert report.read_rows == 4 and report.accepted_rows == 3
    assert report.duplicate_rows == 1 and report.people == 2
    assert report.company_ids == 2
    assert report.conflicting_location_people == 1
    assert report.missing_location_rows == 1
    assert report.missing_profile_ids is None

    assert rows[0]["synthetic_f"] == "F_001"
    assert rows[1]["synthetic_u"] == "U_002"
    assert rows[0]["city"] == "Москва"
    assert rows[1]["city"] == "Омск"
    assert rows[0]["location_conflict"]
    assert rows[1]["location_conflict"]
    assert rows[2]["city"] is None
    assert not rows[2]["location_conflict"]


@pytest.mark.parametrize("rows", [
    [],
    [["", "Москва", "Центр", "U_1"]],
    [["F_1", "Москва", "Центр", ""]],
    [["F_1", "Москва", "Центр"]],
    [["F_1", "Москва", "Центр", "U_1", "лишнее"]],
    [
        ["F_1", "Москва", "Центр", "U_1"],
        ["F_1", "Омск", "Сибирь", "U_1"],
    ],
])
def test_invalid_csv_is_rejected(tmp_path, rows):
    with pytest.raises(ValueError):
        read_person_links(write_csv(tmp_path, rows))


@pytest.mark.parametrize("header", [
    "SYNTHETIC_F;SYNTHETIC_U;CITY_NAME;CITY_NAME",
    "SYNTHETIC_F;SYNTHETIC_U;CITY_NAME",
])
def test_invalid_headers_are_rejected(tmp_path, header):
    path = tmp_path / "bad.csv"
    path.write_text(header + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="столбцы"):
        read_person_links(path)


@pytest.mark.asyncio
async def test_import_retains_unknown_companies_and_does_not_change_company_location(
    link_session, tmp_path,
):
    path = write_csv(tmp_path, [
        ["F_1", "Москва", "Центр", "U_1"],
        ["F_1", "Омск", "Сибирь", "U_external"],
    ])

    report = await import_person_links(
        link_session, path, batch_size=1
    )
    assert report.inserted_rows == 2
    assert report.missing_profile_ids == 1

    company = await link_session.scalar(select(Company))
    assert company.city == "Город компании"
    assert await link_session.scalar(
        select(func.count()).select_from(Company)
    ) == 1

    replay = await import_person_links(link_session, path)
    assert replay.already_imported
    assert replay.inserted_rows == 0
    assert await link_session.scalar(
        select(func.count()).select_from(PersonCompanyLink)
    ) == 2

    record = await link_session.scalar(select(PersonLinkImport))
    assert record.stats == asdict(report)
    assert len(record.source_hash) == 64


@pytest.mark.asyncio
async def test_changed_source_is_blocked(link_session, tmp_path):
    path = write_csv(tmp_path, [
        ["F_1", "Москва", "Центр", "U_1"],
    ])
    await import_person_links(link_session, path)

    path = write_csv(tmp_path, [
        ["F_2", "Москва", "Центр", "U_1"],
    ])
    with pytest.raises(ValueError, match="другой CSV"):
        await import_person_links(link_session, path)

    assert await link_session.scalar(
        select(PersonCompanyLink.synthetic_f)
    ) == "F_1"


@pytest.mark.asyncio
async def test_partial_database_insert_is_rolled_back(
    link_session, tmp_path, monkeypatch,
):
    path = write_csv(tmp_path, [
        ["F_1", "Москва", "Центр", "U_1"],
        ["F_2", "Омск", "Сибирь", "U_1"],
    ])

    original_execute = link_session.execute
    batches = 0

    async def fail_second_batch(statement, *args, **kwargs):
        nonlocal batches
        if (
            isinstance(statement, Insert)
            and statement.table.name == "person_company_links"
        ):
            batches += 1
            if batches == 2:
                raise RuntimeError("test insert failure")
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(
        link_session, "execute", fail_second_batch
    )

    with pytest.raises(RuntimeError, match="test insert failure"):
        await import_person_links(
            link_session, path, batch_size=1
        )

    assert batches == 2
    assert await link_session.scalar(
        select(func.count()).select_from(PersonCompanyLink)
    ) == 0
    assert await link_session.scalar(
        select(func.count()).select_from(PersonLinkImport)
    ) == 0


@pytest.mark.asyncio
async def test_shared_people_rank_related_companies_and_preserve_unknowns(
    link_session, tmp_path,
):
    path = write_csv(tmp_path, [
        ["F_1", "Москва", "Центр", "U_1"],
        ["F_1", "Москва", "Центр", "U_2"],
        ["F_1", "Москва", "Центр", "U_external"],
        ["F_2", "Москва", "Центр", "U_1"],
        ["F_2", "Москва", "Центр", "U_2"],
    ])

    await import_person_links(link_session, path)
    repository = PersonLinkRepository(link_session)

    people = await repository.list_people(
        "U_1", limit=1, offset=1
    )
    assert len(people) == 1
    assert people[0]["synthetic_f"] == "F_2"

    related = await repository.related_companies("U_1")
    assert related == [
        {
            "synthetic_u": "U_2",
            "name": None,
            "shared_people": 2,
        },
        {
            "synthetic_u": "U_external",
            "name": None,
            "shared_people": 1,
        },
    ]

    assert await repository.related_companies("U_absent") == []