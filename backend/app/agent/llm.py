"""The shared client contract and a predictable offline implementation."""

import json
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClientProtocol(Protocol):
    async def generate(self, prompt: str) -> str:
        ...


_DEFAULT_RESPONSE_JSON = json.dumps(
    {
        "what_happened": "Демонстрационный ответ: реальный анализ новости не выполнялся.",
        "why_important": "Значимость для компании в подставном ответе не оценивалась.",
        "consequences": "Последствия в подставном ответе не оценивались.",
        "bank_view": "Подтверждённая позиция Альфа-Банка не предоставлена.",
        "ai_view": "Это фиксированный тестовый ответ, а не вывод языковой модели.",
        "recommended_action": "Для содержательного анализа требуется подключить LLM-клиент.",
        "relevant_bank_product": None,
        "evidences": [],
    },
    ensure_ascii=False,
)


class FakeLLMClient:
    """Return the configured text unchanged, including malformed test responses."""

    def __init__(
        self,
        response_json: str = _DEFAULT_RESPONSE_JSON,
        *,
        responses: list[str | Exception] | None = None,
    ):
        self.response_json = response_json
        self._responses = None if responses is None else list(responses)
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._responses is not None:
            index = len(self.prompts) - 1
            if index >= len(self._responses):
                raise RuntimeError("FakeLLMClient response script exhausted")
            response = self._responses[index]
            if isinstance(response, Exception):
                raise response
            return response
        return self.response_json
