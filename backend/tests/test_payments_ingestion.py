import csv

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from backend.app.db.base import Base
from backend.app.db.models.company import Company
from backend.app.db.models.transaction import (
    CounterpartyTransaction,
    PaymentImport,
)
from backend.app.ingestion.payments import (
    check_payments,
    import_payments,
)


def write_payments(tmp_path, rows):
    path = tmp_path / "payments.csv"

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        writer = csv.writer(
            stream,
            delimiter=";",
        )

        writer.writerow(
            [
                "BENEFICIARY",
                "PAYER",
                "PAY_AMT",
                "PAY_CNT",
            ]
        )

        writer.writerows(rows)

    return path


@pytest_asyncio.fixture
async def payment_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:"
    )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[
                    Company.__table__,
                    CounterpartyTransaction.__table__,
                    PaymentImport.__table__,
                ],
            )

        factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
        )

        async with factory() as session:
            session.add(
                Company(
                    synthetic_u="CLIENT",
                    name="Тестовая компания",
                )
            )

            await session.commit()

            yield session

    finally:
        await engine.dispose()


def test_validation_separates_exclusions_self_transfers_and_errors(
    tmp_path,
):
    path = write_payments(
        tmp_path,
        [
            ["EXTERNAL", "CLIENT", "1000.0", "2"],
            ["CLIENT", "CLIENT", "1000.0", "1"],
            ["EXTERNAL", "AMBIGUOUS", "1000.0", "1"],
            ["EXTERNAL", "CLIENT", "-1000.0", "1"],
        ],
    )

    report = check_payments(
        path,
        ["AMBIGUOUS"],
    )

    assert report.read_rows == 4
    assert report.accepted_rows == 1
    assert report.self_rows == 1
    assert report.excluded_rows == 1
    assert report.rejected_rows == 1
    assert report.inserted_rows == 0


@pytest.mark.asyncio
async def test_same_file_is_not_imported_twice_and_external_profile_is_optional(
    tmp_path,
    payment_session,
):
    path = write_payments(
        tmp_path,
        [["EXTERNAL", "CLIENT", "1000.0", "2"]],
    )

    first = await import_payments(
        payment_session,
        path,
    )

    second = await import_payments(
        payment_session,
        path,
    )

    assert first.inserted_rows == 1
    assert first.missing_profile_ids == 1
    assert second.already_imported is True
    assert second.inserted_rows == 0

    count = await payment_session.scalar(
        select(func.count()).select_from(
            CounterpartyTransaction
        )
    )

    assert count == 1


@pytest.mark.asyncio
async def test_changed_file_cannot_double_the_graph(
    tmp_path,
    payment_session,
):
    path = write_payments(
        tmp_path,
        [["EXTERNAL", "CLIENT", "1000.0", "2"]],
    )

    await import_payments(
        payment_session,
        path,
    )

    write_payments(
        tmp_path,
        [["EXTERNAL", "CLIENT", "2000.0", "2"]],
    )

    with pytest.raises(
        ValueError,
        match="другой файл",
    ):
        await import_payments(
            payment_session,
            path,
        )

    total = await payment_session.scalar(
        select(
            func.sum(CounterpartyTransaction.pay_amt)
        )
    )

    assert total == 1000.0


@pytest.mark.asyncio
async def test_changed_exclusion_policy_is_rejected(
    tmp_path,
    payment_session,
):
    path = write_payments(
        tmp_path,
        [["EXTERNAL", "CLIENT", "1000.0", "2"]],
    )

    await import_payments(
        payment_session,
        path,
    )

    with pytest.raises(
        ValueError,
        match="Список исключённых",
    ):
        await import_payments(
            payment_session,
            path,
            ["OTHER"],
        )


@pytest.mark.asyncio
async def test_malformed_csv_rolls_back_written_batches_and_import_record(
    tmp_path,
    payment_session,
):
    path = write_payments(
        tmp_path,
        [["EXTERNAL", "CLIENT", "1000.0", "2"]],
    )

    with path.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as stream:
        stream.write(
            "EXTERNAL;CLIENT;1000\n"
        )

    with pytest.raises(
        ValueError,
        match="число полей",
    ):
        await import_payments(
            payment_session,
            path,
            batch_size=1,
        )

    count = await payment_session.scalar(
        select(func.count()).select_from(
            CounterpartyTransaction
        )
    )

    imports = await payment_session.scalar(
        select(func.count()).select_from(
            PaymentImport
        )
    )

    assert count == 0
    assert imports == 0


@pytest.mark.asyncio
async def test_demo_payments_are_not_mixed_with_imported_data(
    tmp_path,
    payment_session,
):
    payment_session.add(
        CounterpartyTransaction(
            payer_u="CLIENT",
            beneficiary_u="DEMO",
            pay_amt=100,
            pay_cnt=1,
        )
    )

    await payment_session.commit()

    path = write_payments(
        tmp_path,
        [["EXTERNAL", "CLIENT", "1000.0", "2"]],
    )

    with pytest.raises(
        ValueError,
        match="уже есть платежи",
    ):
        await import_payments(
            payment_session,
            path,
        )