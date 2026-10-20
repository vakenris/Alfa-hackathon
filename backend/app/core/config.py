import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Alfa Business News Analytics"
    VERSION: str = "1.0.0"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "*",
    ]

    # Database
    # Default to sqlite+aiosqlite for instant local run, overridden by .env for Postgres
    DATABASE_URL: str = "sqlite+aiosqlite:///./alfa_analytics.db"

    # Redis (for Pub/Sub and Queue)
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_IN_MEMORY_BUS: bool = True  # Fallback when Redis is not available locally

    # LLM Settings & Budget Guardian
    LLM_API_BASE_URL: str = "https://api.example.com/v1"
    LLM_API_KEY: str = "mock-key"
    LLM_MAX_DAILY_BUDGET_RUB: float = 500.0
    LLM_FALLBACK_TO_MOCK: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
