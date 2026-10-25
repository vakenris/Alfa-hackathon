from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models.company import Company
from backend.app.db.models.transaction import CounterpartyTransaction
from backend.app.schemas.company import CounterpartyItem


class CompanyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(
        self,
        company_id: int,
    ) -> Optional[Company]:
        stmt = select(Company).where(
            Company.id == company_id
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_synthetic_u(
        self,
        synthetic_u: str,
    ) -> Optional[Company]:
        stmt = select(Company).where(
            Company.synthetic_u == synthetic_u
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Company]:
        stmt = (
            select(Company)
            .order_by(Company.id)
            .offset(offset)
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_top_counterparties(
        self,
        synthetic_u: str,
        limit: int = 10,
    ) -> List[CounterpartyItem]:
        """Вернуть до limit контрагентов в каждом направлении."""
        if limit < 1:
            return []

        tx = CounterpartyTransaction
        items: List[CounterpartyItem] = []

        directions = (
            (
                tx.payer_u,
                tx.beneficiary_u,
                "SUPPLIER",
            ),
            (
                tx.beneficiary_u,
                tx.payer_u,
                "CUSTOMER",
            ),
        )

        for own, other, direction in directions:
            total_amount = func.sum(tx.pay_amt)
            transaction_count = func.sum(tx.pay_cnt)

            stmt = (
                select(
                    other,
                    Company.name,
                    total_amount,
                    transaction_count,
                )
                .select_from(tx)
                .outerjoin(
                    Company,
                    Company.synthetic_u == other,
                )
                .where(
                    own == synthetic_u,
                    other != synthetic_u,
                )
                .group_by(
                    other,
                    Company.name,
                )
                .order_by(
                    total_amount.desc(),
                    other.asc(),
                )
                .limit(limit)
            )

            result = await self.session.execute(stmt)

            for uid, name, amount, count in result:
                items.append(
                    CounterpartyItem(
                        synthetic_u=uid,
                        name=name,
                        direction=direction,
                        total_amount=float(amount),
                        transaction_count=int(count),
                    )
                )

        return sorted(
            items,
            key=lambda item: (
                -item.total_amount,
                item.synthetic_u,
                item.direction,
            ),
        )