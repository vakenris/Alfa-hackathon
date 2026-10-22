"""Autonomous insight prototype; importing it does not configure the backend."""

from .llm import FakeLLMClient, LLMClientProtocol
from .config import AgentConfig
from .models import AnalyticsDocument
from .orchestrator import AgentInsightEngine
from .tools import AnalyticsSourceProtocol, MockAnalyticsSource
from .tracer import RecordingTracer

__all__ = [
    "AgentInsightEngine", "FakeLLMClient", "LLMClientProtocol", "RecordingTracer",
    "AgentConfig", "AnalyticsDocument", "AnalyticsSourceProtocol", "MockAnalyticsSource",
]
