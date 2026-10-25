"""Explicit task-to-model selection, with no retries or automatic escalation."""

from collections.abc import Collection, Mapping
from types import MappingProxyType

import httpx

from .client import ChatCompletionsClient, ChatCompletionsConfig


KNOWN_LIVE_MODEL_ID = "deepseek/deepseek-v4.1-flash"


class ModelRouter:
    """Select a configured model before a request and return a fresh real client.

    ``approved_model_ids`` must come from the caller's confirmed gateway policy.
    The known live identifier is a convenience constant, not an implicit allowlist.
    ``routes=None`` explicitly selects the approved base model for every valid
    task. An explicit route map instead rejects tasks absent from that map.
    No network request, environment lookup or provider discovery happens here.
    """

    def __init__(
        self, base_config: ChatCompletionsConfig, routes: Mapping[str, str] | None = None, *,
        approved_model_ids: Collection[str],
    ):
        if not isinstance(base_config, ChatCompletionsConfig):
            raise ValueError("A validated ChatCompletionsConfig is required.")
        if (isinstance(approved_model_ids, (str, bytes))
                or not isinstance(approved_model_ids, Collection)
                or not approved_model_ids
                or any(type(item) is not str or not item or item != item.strip()
                       for item in approved_model_ids)):
            raise ValueError("Explicit exact approved model identifiers are required.")
        approved = frozenset(approved_model_ids)
        if base_config.model not in approved:
            raise ValueError("The base model is not explicitly approved.")
        if routes is not None and (not isinstance(routes, Mapping) or not routes):
            raise ValueError("At least one explicit task route is required.")
        selected = {}
        for task, model in (routes.items() if routes is not None else ()):
            if type(task) is not str or not task or task != task.strip() or len(task) > 128:
                raise ValueError("Route task names must be explicit nonempty strings.")
            if type(model) is not str or model not in approved:
                raise ValueError("Every routed model must be explicitly approved.")
            selected[task] = model
        self._base_config = base_config.model_copy(deep=True)
        self._default_model = base_config.model if routes is None else None
        self.routes = MappingProxyType(selected)
        self.approved_model_ids = approved

    def config_for(self, task: str) -> ChatCompletionsConfig:
        """Return independent validated configuration; unknown tasks fail closed."""
        if type(task) is not str or not task or task != task.strip() or len(task) > 128:
            raise ValueError("Task names must be explicit nonempty strings.")
        if self._default_model is None and task not in self.routes:
            raise ValueError("No explicit model route exists for this task.")
        values = self._base_config.model_dump()
        values["model"] = self._default_model if self._default_model is not None else self.routes[task]
        return ChatCompletionsConfig.model_validate(values)

    def client_for(
        self, task: str, *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> ChatCompletionsClient:
        """Return the original transport class, preserving runtime type checks."""
        return ChatCompletionsClient(self.config_for(task), transport=transport)
