from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey,
    Integer, JSON, String, Text, UniqueConstraint,
)

from backend.app.db.base import Base


class BankDocument(Base):
    __tablename__ = "bank_documents"

    # The PDF's SHA-256 is its stable document ID.
    id = Column(String(64), primary_key=True)
    source_file = Column(String(255), nullable=False)
    title = Column(String(512), nullable=False)
    category = Column(String(128), nullable=False)
    source_origin = Column(String(128), nullable=False)
    published_on = Column(Date, nullable=True, index=True)
    publication_period = Column(String(7), nullable=True)
    historical = Column(Boolean, nullable=False, default=False)
    page_count = Column(Integer, nullable=False)
    extraction_hash = Column(String(64), nullable=False)
    extraction_metadata = Column(JSON, nullable=False)
    empty_pages = Column(JSON, nullable=False, default=list)
    requires_review = Column(Boolean, nullable=False, default=True)
    provenance_status = Column(String(16), nullable=False, default="unverified")
    ingested_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )

    __table_args__ = (
        CheckConstraint("page_count >= 1", name="ck_bank_document_page_count"),
    )


class BankDocumentPage(Base):
    __tablename__ = "bank_document_pages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        String(64),
        ForeignKey("bank_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_number = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    text_sha256 = Column(String(64), nullable=False)
    extraction_method = Column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "document_id", "page_number", name="uq_bank_document_page"
        ),
        CheckConstraint("page_number >= 1", name="ck_bank_page_number"),
    )