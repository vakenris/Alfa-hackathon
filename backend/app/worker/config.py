from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    mode: Literal["inline", "database"] = "inline"
    timeout_seconds: int = Field(default=180, ge=1, le=540)
    poll_seconds: float = Field(default=1, ge=0.1, le=30)
    engine_factory: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


worker_settings = WorkerSettings()