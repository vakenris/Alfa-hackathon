import csv

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from backend.app.db.base import Base
from backend.app.db.models.company import Company
from backend.app.ingestion.companies import (
    FLAG_FIELDS,
    REQUIRED_COLUMNS,
    read_companies_csv,
    save_companies,
)


def company_row(uid="U_001", **changes):
    row = dict.fromkeys(
        REQUIRED_COLUMNS,
        "",
    )

    row.update(
        dict.fromkeys(FLAG_FIELDS, "0.0")
    )

    row.update(
        {
            "SYNTHETIC_U": uid,
            "MAINOKVED_CODE": "01.11",
            "REVENUE_COMPANY": "136493.0",
            "CL_PRODUCT_LOA_MARK": "1.0",
            **changes,
        }
    )

    return row


def write_csv(tmp_path, rows):
    path = tmp_path / "companies.csv"

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=sorted(REQUIRED_COLUMNS),
            delimiter=";",
        )

        writer.writeheader()
        writer.writerows(rows)

    return path


@pytest_asyncio.fixture
async def import_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:"
    )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[Company.__table__],
            )

        factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
        )

        async with factory() as session:
            yield session

    finally:
        await engine.dispose()


def test_actual_csv_format_preserves_types_and_missing_text(
    tmp_path,
):
    path = write_csv(
        tmp_path,
        [company_row()],
    )

    companies, report = read_companies_csv(path)

    assert report.accepted == 1
    assert report.rejected_rows == 0

    assert companies[0]["synthetic_u"] == "U_001"
    assert companies[0]["mainokved_code"] == "01.11"
    assert companies[0]["cl_product_loa_mark"] is True
    assert companies[0]["cl_product_rko_mark"] is False
    assert companies[0]["amount_workers_spark"] is None


def test_conflicting_identifier_is_rejected_entirely(tmp_path):
    first = company_row("CONFLICT")

    path = write_csv(
        tmp_path,
        [
            first,
            first,
            company_row("CONFLICT", CITY="Москва"),
            company_row("CONFLICT", CITY="Самара"),
            company_row("VALID"),
        ],
    )

    companies, report = read_companies_csv(path)

    assert [
        item["synthetic_u"]
        for item in companies
    ] == ["VALID"]

    assert report.read_rows == 5
    assert report.rejected_rows == 4
    assert report.duplicate_rows == 0
    assert report.conflicting_ids == ["CONFLICT"]


def test_identical_rows_are_imported_once(tmp_path):
    row = company_row()

    path = write_csv(
        tmp_path,
        [row, row],
    )

    companies, report = read_companies_csv(path)

    assert len(companies) == 1
    assert report.duplicate_rows == 1
    assert report.rejected_rows == 0


def test_unknown_flag_is_rejected_instead_of_becoming_false(
    tmp_path,
):
    path = write_csv(
        tmp_path,
        [company_row(CL_PRODUCT_LOA_MARK="")],
    )

    companies, report = read_companies_csv(path)

    assert companies == []
    assert report.rejected_rows == 1
    assert "CL_PRODUCT_LOA_MARK" in report.errors[0]


def test_missing_columns_are_reported(tmp_path):
    path = tmp_path / "bad.csv"

    path.write_text(
        "SYNTHETIC_U\nU_1\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="REVENUE_COMPANY",
    ):
        read_companies_csv(path)


@pytest.mark.asyncio
async def test_reimport_updates_profile_without_changing_id_or_name(
    tmp_path,
    import_session,
):
    path = write_csv(
        tmp_path,
        [company_row(CITY="Самара")],
    )

    companies, report = read_companies_csv(path)

    await save_companies(
        import_session,
        companies,
        report,
    )

    assert (report.inserted, report.updated) == (1, 0)

    original = await import_session.scalar(
        select(Company)
    )

    original_id = original.id
    original.name = "Собственное название"

    await import_session.commit()

    path = write_csv(
        tmp_path,
        [company_row(CITY="Москва")],
    )

    companies, report = read_companies_csv(path)

    await save_companies(
        import_session,
        companies,
        report,
    )

    await import_session.refresh(original)

    assert (report.inserted, report.updated) == (0, 1)
    assert original.id == original_id
    assert original.name == "Собственное название"
    assert original.city == "Москва"

    count = await import_session.scalar(
        select(func.count()).select_from(Company)
    )

    assert count == 1


@pytest.mark.asyncio
async def test_database_error_rolls_back_all_batches(
    tmp_path,
    import_session,
):
    path = write_csv(
        tmp_path,
        [
            company_row("U_A"),
            company_row("U_B"),
        ],
    )

    companies, report = read_companies_csv(path)

    # Имитируем ошибку записи во втором пакете.
    companies[1]["name"] = None

    with pytest.raises(IntegrityError):
        await save_companies(
            import_session,
            companies,
            report,
            batch_size=1,
        )

    count = await import_session.scalar(
        select(func.count()).select_from(Company)
    )

    assert count == 0
    assert report.inserted == 0