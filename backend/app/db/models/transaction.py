from sqlalchemy import Column, Integer, String, Float, Index
from backend.app.db.base import Base


class CounterpartyTransaction(Base):
    __tablename__ = "counterparty_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    payer_u = Column(String(64), index=True, nullable=False)
    beneficiary_u = Column(String(64), index=True, nullable=False)
    pay_amt = Column(Float, default=0.0)
    pay_cnt = Column(Integer, default=1)

    __table_args__ = (
        Index("idx_payer_beneficiary", "payer_u", "beneficiary_u"),
    )
