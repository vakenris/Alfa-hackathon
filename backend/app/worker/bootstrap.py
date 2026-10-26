"""Wire the ML runtime to reviewed PDF pages and an explicit budgeted client."""

import importlib
import inspect
import re

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.worker.config import worker_settings


class RuntimeSettings(BaseSettings):
    client_factory: str
    approved_model_ids: list[str] = Field(min_length=1)

    model_config = SettingsConfigDict(
        env_prefix="WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("client_factory")
    @classmethod
    def validate_factory(cls, value: str) -> str:
        value = value.strip()

        pattern = (
            r"[A-Za-z_]\w*"
            r"(?:\.[A-Za-z_]\w*)*"
            r":[A-Za-z_]\w*"
        )

        if not re.fullmatch(pattern, value):
            raise ValueError(
                "Нужен путь module:function "
                "к фабрике бюджетного клиента"
            )

        return value

    @field_validator("approved_model_ids")
    @classmethod
    def validate_models(cls, values: list[str]) -> list[str]:
        values = [value.strip() for value in values]

        if (
            any(not value for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(
                "Нужны непустые уникальные "
                "подтверждённые model ID"
            )

        return values


def create_engine():
    config = RuntimeSettings()

    if worker_settings.timeout_seconds <= 400:
        raise ValueError(
            "Для ML задай WORKER_TIMEOUT_SECONDS=450"
        )

    # Эти модули появляются после объединения с ML-веткой.
    from backend.app.agent.config import AgentConfig
    from backend.app.agent.runtime import RuntimeAgentEngine
    from backend.app.llm.budget_guard import (
        BudgetedChatCompletionsClient,
    )
    from backend.app.db.session import async_session_factory
    from backend.app.ingestion.analytics_source import (
        BankPdfAnalyticsSource,
    )

    module_name, function_name = config.client_factory.split(
        ":", 1
    )

    factory = getattr(
        importlib.import_module(module_name),
        function_name,
    )

    if (
        not callable(factory)
        or inspect.iscoroutinefunction(factory)
    ):
        raise ValueError(
            "Фабрика должна синхронно возвращать "
            "нового бюджетного клиента"
        )

    def make_client():
        client = factory()

        if not isinstance(
            client,
            BudgetedChatCompletionsClient,
        ):
            raise ValueError(
                "Фабрика должна возвращать "
                "BudgetedChatCompletionsClient"
            )

        if client.config.model not in config.approved_model_ids:
            raise ValueError(
                "Model ID клиента отсутствует "
                "в WORKER_APPROVED_MODEL_IDS"
            )

        return client

    return RuntimeAgentEngine(
        client_factory=make_client,
        source_factory=lambda: BankPdfAnalyticsSource(
            async_session_factory
        ),
        approved_model_ids=config.approved_model_ids,
        config=AgentConfig(
            max_llm_calls=1,
            max_recovery_attempts=0,
            llm_timeout_seconds=180,
            run_timeout_seconds=400,
        ),
    )