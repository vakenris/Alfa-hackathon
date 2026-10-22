from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from backend.app.core.logging import logger
from backend.app.core.security import mask_sensitive_text


class ToolAuditEntry(BaseModel):
    timestamp: datetime
    tool_name: str
    user_role: str
    is_safe: bool
    duration_ms: float
    status: str
    input_summary: str
    error: Optional[str] = None


class ToolAuditManager:
    """Manages audit logging and safety guardrails for AI Agent Tool calls."""
    
    # Whitelist of allowed read-only safe tools
    SAFE_TOOLS = {
        "get_company_profile",
        "get_counterparties_graph",
        "search_bank_analytics",
        "search_market_news",
        "match_bank_products",
    }

    def __init__(self, max_history: int = 500):
        self._history: List[ToolAuditEntry] = []
        self._max_history = max_history

    def verify_tool_safety(self, tool_name: str) -> bool:
        """Enforces that AI agent can only invoke whitelisted read-only tools."""
        return tool_name in self.SAFE_TOOLS

    def record_tool_call(
        self,
        tool_name: str,
        user_role: str,
        duration_ms: float,
        status: str,
        params: Dict[str, Any],
        error: Optional[str] = None
    ) -> ToolAuditEntry:
        is_safe = self.verify_tool_safety(tool_name)
        
        # Mask sensitive arguments in summary
        clean_params = mask_sensitive_text(str(params))[:200]
        
        entry = ToolAuditEntry(
            timestamp=datetime.utcnow(),
            tool_name=tool_name,
            user_role=user_role,
            is_safe=is_safe,
            duration_ms=duration_ms,
            status=status if is_safe else "BLOCKED_UNSAFE_TOOL",
            input_summary=clean_params,
            error=error
        )

        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        logger.info(
            f"[AUDIT] Tool: {tool_name} | Role: {user_role} | Status: {entry.status} | "
            f"Safe: {is_safe} | Latency: {duration_ms:.1f}ms"
        )
        return entry

    def get_recent_audits(self, limit: int = 50) -> List[ToolAuditEntry]:
        return self._history[-limit:]


audit_manager = ToolAuditManager()
