from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["Health Checks"])


@router.get("/live")
async def health_live():
    """Liveness probe: verifies server is running."""
    return {"status": "ok", "check": "live"}


@router.get("/ready")
async def health_ready():
    """Readiness probe: verifies server is ready to accept traffic."""
    return {"status": "ok", "check": "ready"}
