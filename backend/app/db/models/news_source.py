from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from backend.app.db.base import Base


class ExternalNewsSource(Base):
    __tablename__ = "external_news_sources"

    id = Column(String(64), primary_key=True)
    article_id = Column(
        Integer,
        ForeignKey("news_articles.id"),
        nullable=False,
        unique=True,
    )
    source_url = Column(String(2048), nullable=False)
    source_item_id = Column(String(512), nullable=True)
    feed_url = Column(String(2048), nullable=False)
    feed_sha256 = Column(String(64), nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    content_scope = Column(String(32), nullable=False)
    original_html = Column(Text, nullable=False)

    imported_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )