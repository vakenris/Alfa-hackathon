"""AuditLog SQLAlchemy model — persists structured audit records for every API action."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Index

from backend.app.db.base import Base


class AuditLog(Base):
    """One row per auditable request: who, what endpoint, what role, outcome, latency."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Who
    user_role = Column(String(32), nullable=False, index=True)  # CEO, CFO, ...
    company_id = Column(Integer, nullable=True, index=True)     # если применимо

    # What
    method = Column(String(16), nullable=False)                 # GET / POST / ...
    path = Column(String(512), nullable=False)
    action = Column(String(128), nullable=True)                 # смысловое имя действия

    # Outcome
    status_code = Column(Integer, nullable=False, index=True)
    error_detail = Column(Text, nullable=True)

    # Performance
    duration_ms = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("idx_audit_role_created", "user_role", "created_at"),
        Index("idx_audit_path_status", "path", "status_code"),
    )
