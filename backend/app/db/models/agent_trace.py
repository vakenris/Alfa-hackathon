from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from backend.app.db.base import Base


class JobRecord(Base):
    __tablename__ = "job_records"

    id = Column(String(64), primary_key=True)  # UUID
    company_id = Column(Integer, nullable=False)
    event_id = Column(Integer, nullable=False)
    status = Column(String(32), default="PENDING", index=True)  # PENDING, PROCESSING, SUCCESS, FAILED
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    trace_steps = relationship("AgentTraceStep", back_populates="job", cascade="all, delete-orphan")


class AgentTraceStep(Base):
    __tablename__ = "agent_trace_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), ForeignKey("job_records.id"), nullable=False, index=True)
    step_number = Column(Integer, nullable=False)
    step_name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("JobRecord", back_populates="trace_steps")
