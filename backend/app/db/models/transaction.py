from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
)

from backend.app.db.base import Base


class CounterpartyTransaction(Base):
    __tablename__ = "counterparty_transactions"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    payer_u = Column(
        String(64),
        index=True,
        nullable=False,
    )

    beneficiary_u = Column(
        String(64),
        index=True,
        nullable=False,
    )

    pay_amt = Column(Float, default=0.0)
    pay_cnt = Column(Integer, default=1)

    __table_args__ = (
        Index(
            "idx_payer_beneficiary",
            "payer_u",
            "beneficiary_u",
        ),
    )


class PaymentImport(Base):
    __tablename__ = "payment_imports"

    id = Column(
        String(32),
        primary_key=True,
    )

    source_hash = Column(
        String(64),
        nullable=False,
    )

    source_name = Column(
        String(255),
        nullable=False,
    )

    stats = Column(
        JSON,
        nullable=False,
    )

    created_at = Column(
        DateTime,
        default=lambda: datetime.now(
            timezone.utc
        ).replace(tzinfo=None),
        nullable=False,
    )