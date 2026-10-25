import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from backend.app.db.base import Base
from backend.app.db.models.company import Company
from backend.app.db.models.transaction import CounterpartyTransaction
from backend.app.db.repositories.company_repo import CompanyRepository


@pytest_asyncio.fixture
async def repo_session():
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
                ],
            )

        factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
        )

        async with factory() as session:
            yield session

    finally:
        await engine.dispose()


def payment(payer, beneficiary, amount, count=1):
    return CounterpartyTransaction(
        payer_u=payer,
        beneficiary_u=beneficiary,
        pay_amt=amount,
        pay_cnt=count,
    )


@pytest.mark.asyncio
async def test_top_counterparties_aggregates_before_limiting(
    repo_session,
):
    repo_session.add_all(
        [
            Company(
                synthetic_u="CP_OUT",
                name="Тестовый контрагент",
            ),
            payment("CLIENT", "CP_OUT", 100, 2),
            payment("CLIENT", "CP_SMALL_OUT", 350, 1),
            payment("CLIENT", "CP_OUT", 300, 3),
            payment("CP_IN", "CLIENT", 500, 4),
            payment("CP_SMALL_IN", "CLIENT", 100, 1),
            payment("CP_IN", "CLIENT", 20, 1),
            payment("CLIENT", "CLIENT", 10000, 10),
            payment("UNRELATED_A", "UNRELATED_B", 99999, 1),
        ]
    )

    await repo_session.commit()

    repository = CompanyRepository(repo_session)

    items = await repository.get_top_counterparties(
        "CLIENT",
        limit=1,
    )

    assert [
        (
            item.synthetic_u,
            item.direction,
            item.total_amount,
            item.transaction_count,
        )
        for item in items
    ] == [
        ("CP_IN", "CUSTOMER", 520.0, 5),
        ("CP_OUT", "SUPPLIER", 400.0, 5),
    ]

    assert items[0].name is None
    assert items[1].name == "Тестовый контрагент"


@pytest.mark.asyncio
async def test_same_counterparty_keeps_both_payment_directions(
    repo_session,
):
    repo_session.add_all(
        [
            payment("CLIENT", "CP", 100, 2),
            payment("CP", "CLIENT", 40, 1),
        ]
    )

    await repo_session.commit()

    repository = CompanyRepository(repo_session)

    items = await repository.get_top_counterparties(
        "CLIENT"
    )

    assert [
        (item.direction, item.total_amount)
        for item in items
    ] == [
        ("SUPPLIER", 100.0),
        ("CUSTOMER", 40.0),
    ]


@pytest.mark.asyncio
async def test_equal_amounts_have_stable_order(repo_session):
    repo_session.add_all(
        [
            payment("CLIENT", "CP_B", 100),
            payment("CLIENT", "CP_A", 100),
        ]
    )

    await repo_session.commit()

    repository = CompanyRepository(repo_session)

    items = await repository.get_top_counterparties(
        "CLIENT",
        limit=1,
    )

    assert [
        item.synthetic_u
        for item in items
    ] == ["CP_A"]


@pytest.mark.asyncio
async def test_company_without_payments_has_empty_counterparties(
    repo_session,
):
    repo_session.add(
        payment("OTHER_A", "OTHER_B", 100)
    )

    await repo_session.commit()

    repository = CompanyRepository(repo_session)

    items = await repository.get_top_counterparties(
        "CLIENT"
    )

    assert items == []