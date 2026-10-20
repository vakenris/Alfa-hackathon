from typing import List, Optional
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.db.models.company import Company, CounterpartyTransaction
from backend.app.schemas.company import CounterpartyItem


class CompanyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, company_id: int) -> Optional[Company]:
        stmt = select(Company).where(Company.id == company_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_synthetic_u(self, synthetic_u: str) -> Optional[Company]:
        stmt = select(Company).where(Company.synthetic_u == synthetic_u)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, limit: int = 50, offset: int = 0) -> List[Company]:
        stmt = select(Company).order_by(Company.id).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_top_counterparties(self, synthetic_u: str, limit: int = 10) -> List[CounterpartyItem]:
        # Outgoing payments (payer -> beneficiary)
        stmt_out = select(
            CounterpartyTransaction.beneficiary_u,
            CounterpartyTransaction.pay_amt,
            CounterpartyTransaction.pay_cnt
        ).where(CounterpartyTransaction.payer_u == synthetic_u).limit(limit)
        
        result_out = await self.session.execute(stmt_out)
        items: List[CounterpartyItem] = []
        for row in result_out:
            items.append(CounterpartyItem(
                synthetic_u=row[0],
                direction="SUPPLIER",
                total_amount=row[1],
                transaction_count=row[2]
            ))

        # Incoming payments (payer -> beneficiary)
        stmt_in = select(
            CounterpartyTransaction.payer_u,
            CounterpartyTransaction.pay_amt,
            CounterpartyTransaction.pay_cnt
        ).where(CounterpartyTransaction.beneficiary_u == synthetic_u).limit(limit)

        result_in = await self.session.execute(stmt_in)
        for row in result_in:
            items.append(CounterpartyItem(
                synthetic_u=row[0],
                direction="CUSTOMER",
                total_amount=row[1],
                transaction_count=row[2]
            ))

        return items
