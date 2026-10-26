import asyncio

from backend.app.core.logging import logger


async def publish_event(bus, job_id: str, payload: dict):
    try:
        await asyncio.wait_for(
            bus.publish(f"job:{job_id}", payload),
            timeout=3,
        )
    except Exception:
        logger.warning(
            "SSE publication failed for job %s",
            job_id,
            exc_info=True,
        )


async def close_bus(bus):
    client = getattr(bus, "_redis", None)

    if client is not None:
        try:
            await asyncio.wait_for(
                client.aclose(),
                timeout=3,
            )
        except Exception:
            logger.warning(
                "Redis connection close failed",
                exc_info=True,
            )