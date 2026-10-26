from datetime import datetime, timezone

from backend.app.core.bus import get_event_bus
from backend.app.core.logging import logger
from backend.app.db.repositories.job_repo import JobRepository
from backend.app.worker.events import publish_event


class AgentTracer:
    def __init__(
        self,
        job_id: str,
        job_repo: JobRepository,
        *,
        bus=None,
    ):
        self.job_id = job_id
        self.job_repo = job_repo
        self.current_step = 0
        self.bus = (
            bus if bus is not None else get_event_bus()
        )

    async def emit_step(
        self,
        step_name: str,
        description: str,
    ):
        number = self.current_step + 1

        await self.job_repo.add_trace_step(
            job_id=self.job_id,
            step_number=number,
            step_name=step_name,
            description=description,
        )

        self.current_step = number

        logger.info(
            "Job %s: step %s, %s",
            self.job_id,
            number,
            step_name,
        )

        await publish_event(self.bus, self.job_id, {
            "event": "step",
            "job_id": self.job_id,
            "step_number": number,
            "step_name": step_name,
            "description": description,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })