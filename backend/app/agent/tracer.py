"""An in-memory tracer for use without a database or event bus."""


class RecordingTracer:
    def __init__(self):
        self.steps: list[dict[str, str]] = []

    async def emit_step(self, step_name: str, description: str):
        self.steps.append({"step_name": step_name, "description": description})
