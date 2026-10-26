from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, JSON, String

from backend.app.db.base import Base


class PersonCompanyLink(Base):
    __tablename__ = "person_company_links"

    synthetic_f = Column(String(64), primary_key=True)
    synthetic_u = Column(String(64), primary_key=True, index=True)
    city = Column(String(128), nullable=True)
    region = Column(String(128), nullable=True)
    location_conflict = Column(Boolean, nullable=False, default=False)


class PersonLinkImport(Base):
    __tablename__ = "person_link_imports"

    id = Column(String(32), primary_key=True)
    source_hash = Column(String(64), nullable=False)
    source_name = Column(String(255), nullable=False)
    stats = Column(JSON, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )