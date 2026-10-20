from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from backend.app.db.base import Base


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512), nullable=False)
    source = Column(String(128), default="Внешние новости")
    published_at = Column(DateTime, default=datetime.utcnow)
    content = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    category = Column(String(128), index=True, default="Общее")
    target_okved_prefix = Column(String(32), index=True, nullable=True)
    is_bank_analytics = Column(Boolean, default=False)
