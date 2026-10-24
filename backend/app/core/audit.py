"""Audit module — два уровня аудита:

1. ToolAuditManager — in-memory whitelist и логирование вызовов инструментов агента
   (написан Ролью 1, используется ML-слоем через agent/tools).

2. AuditMiddleware — ASGI-middleware, пишет каждый HTTP-запрос в таблицу audit_logs
   (написан Ролью 1, подключён в main.py через app.add_middleware).

3. audit_action() — async context manager для явного аудита доменных действий в роутах.
"""

import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from backend.app.core.logging import logger
from backend.app.db.models.audit import AuditLog
from backend.app.db.session import async_session_factory


# =============================================================================
# Layer 1 — Tool-level audit (whitelist + in-memory log)
# =============================================================================

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
    """Whitelist-based safety guard and in-memory audit log for AI agent tool calls.

    Only tools listed in SAFE_TOOLS may be invoked by the agent.
    All calls (safe or blocked) are recorded for traceability.
    """

    SAFE_TOOLS: frozenset[str] = frozenset({
        "get_company_profile",
        "get_counterparties_graph",
        "search_bank_analytics",
        "search_market_news",
        "match_bank_products",
    })

    def __init__(self, max_history: int = 500):
        self._history: List[ToolAuditEntry] = []
        self._max_history = max_history

    def verify_tool_safety(self, tool_name: str) -> bool:
        """Return True only for whitelisted read-only tools."""
        return tool_name in self.SAFE_TOOLS

    def record_tool_call(
        self,
        tool_name: str,
        user_role: str,
        duration_ms: float,
        status: str,
        params: Dict[str, Any],
        error: Optional[str] = None,
    ) -> ToolAuditEntry:
        is_safe = self.verify_tool_safety(tool_name)
        # Truncate params summary to avoid storing large payloads
        input_summary = str(params)[:200]

        entry = ToolAuditEntry(
            timestamp=datetime.utcnow(),
            tool_name=tool_name,
            user_role=user_role,
            is_safe=is_safe,
            duration_ms=duration_ms,
            status=status if is_safe else "BLOCKED_UNSAFE_TOOL",
            input_summary=input_summary,
            error=error,
        )

        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        logger.info(
            f"[AUDIT] Tool: {tool_name} | Role: {user_role} | "
            f"Status: {entry.status} | Safe: {is_safe} | Latency: {duration_ms:.1f}ms"
        )
        return entry

    def get_recent_audits(self, limit: int = 50) -> List[ToolAuditEntry]:
        return self._history[-limit:]


# Singleton — shared across the application process
audit_manager = ToolAuditManager()


# =============================================================================
# Layer 2 — Route-level explicit audit (context manager)
# =============================================================================

@asynccontextmanager
async def audit_action(
    session: AsyncSession,
    role: str,
    action: str,
    *,
    method: str = "INTERNAL",
    path: str = "/internal",
    company_id: Optional[int] = None,
):
    """Context manager that writes an AuditLog row for an explicit domain action.

    On success status_code=200; on exception status_code=500 with error_detail.
    Does NOT commit — relies on the caller's session lifecycle.

    Example::
        async with audit_action(db, role.value, "create_insight", company_id=1):
            result = await service.create(...)
    """
    start = time.monotonic()
    error_detail: Optional[str] = None
    status_code = 200
    try:
        yield
    except Exception as exc:
        status_code = 500
        error_detail = str(exc)
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        log = AuditLog(
            user_role=role,
            company_id=company_id,
            method=method,
            path=path,
            action=action,
            status_code=status_code,
            error_detail=error_detail,
            duration_ms=round(duration_ms, 2),
        )
        session.add(log)
        try:
            await session.flush([log])
        except Exception:
            logger.warning("audit_action: could not flush AuditLog row", exc_info=True)


# =============================================================================
# Layer 3 — HTTP middleware (automatic per-request DB audit)
# =============================================================================

class AuditMiddleware(BaseHTTPMiddleware):
    """ASGI middleware: writes one AuditLog row per HTTP request.

    Uses its own short-lived DB session (independent of request session)
    so a request rollback does not erase the audit record.

    Health probe paths are skipped to avoid log noise.
    """

    _SKIP_PATHS: frozenset[str] = frozenset({
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
    })

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in self._SKIP_PATHS:
            return await call_next(request)

        start = time.monotonic()
        role = request.headers.get("X-User-Role", "CEO").upper()

        # Extract company_id from URL: /companies/{id}/...
        company_id: Optional[int] = None
        parts = path.split("/")
        for i, part in enumerate(parts):
            if part == "companies" and i + 1 < len(parts):
                try:
                    company_id = int(parts[i + 1])
                except ValueError:
                    pass
                break

        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        try:
            async with async_session_factory() as session:
                log = AuditLog(
                    user_role=role,
                    company_id=company_id,
                    method=request.method,
                    path=path,
                    action=None,
                    status_code=response.status_code,
                    error_detail=None,
                    duration_ms=round(duration_ms, 2),
                )
                session.add(log)
                await session.commit()
        except Exception:
            logger.warning("AuditMiddleware: failed to persist AuditLog row", exc_info=True)

        return response
