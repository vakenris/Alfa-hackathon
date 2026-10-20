from datetime import datetime
from backend.app.core.bus import get_event_bus
from backend.app.core.logging import logger
from backend.app.db.repositories.job_repo import JobRepository


class AgentTracer:
    def __init__(self, job_id: str, job_repo: JobRepository):
        self.job_id = job_id
        self.job_repo = job_repo
        self.current_step = 0
        self.bus = get_event_bus()

    async def emit_step(self, step_name: str, description: str):
        self.current_step += 1
        logger.info(f"[Job {self.job_id}] Step {self.current_step}: {step_name} - {description}")

        # Save to DB
        await self.job_repo.add_trace_step(
            job_id=self.job_id,
            step_number=self.current_step,
            step_name=step_name,
            description=description
        )

        # Broadcast via Event Bus for live SSE
        payload = {
            "event": "step",
            "job_id": self.job_id,
            "step_number": self.current_step,
            "step_name": step_name,
            "description": description,
            "timestamp": datetime.utcnow().isoformat()
        }
        await self.bus.publish(f"job:{self.job_id}", payload)
