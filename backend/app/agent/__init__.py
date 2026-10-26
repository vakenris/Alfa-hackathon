"""Autonomous insight prototype; importing it does not configure the backend."""

from .llm import FakeLLMClient, LLMClientProtocol
from .config import AgentConfig
from .models import AnalyticsDocument, AgentAnalysisResult
from .orchestrator import AgentInsightEngine
from .tools import AnalyticsSourceProtocol, MockAnalyticsSource
from .tracer import RecordingTracer
from .backend_source import BackendAnalyticsSource
from .runtime import RuntimeAgentEngine
from .actions import ManagementActionEngine
from .action_models import ManagementActionRequest, ManagementActionResult

__all__ = [
    "AgentInsightEngine", "FakeLLMClient", "LLMClientProtocol", "RecordingTracer",
    "AgentConfig", "AnalyticsDocument", "AnalyticsSourceProtocol", "MockAnalyticsSource",
    "AgentAnalysisResult", "BackendAnalyticsSource", "RuntimeAgentEngine",
    "ManagementActionEngine", "ManagementActionRequest", "ManagementActionResult",
]
