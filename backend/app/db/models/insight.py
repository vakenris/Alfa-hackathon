from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from backend.app.db.base import Base


class Insight(Base):
    __tablename__ = "insights"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), index=True, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("news_articles.id"), nullable=False)
    
    # 6 core answers requested by Alfa-Bank
    what_happened = Column(Text, nullable=False)
    why_important = Column(Text, nullable=False)
    consequences = Column(Text, nullable=False)
    bank_view = Column(Text, nullable=False)
    ai_view = Column(Text, nullable=False)
    recommended_action = Column(Text, nullable=False)
    relevant_bank_product = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    evidences = relationship("Evidence", back_populates="insight", cascade="all, delete-orphan")


class Evidence(Base):
    __tablename__ = "evidences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    insight_id = Column(Integer, ForeignKey("insights.id"), nullable=False)
    claim = Column(Text, nullable=False)
    source_title = Column(String(512), nullable=False)
    source_url = Column(String(512), nullable=True)
    company_feature = Column(String(255), nullable=True)

    insight = relationship("Insight", back_populates="evidences")
