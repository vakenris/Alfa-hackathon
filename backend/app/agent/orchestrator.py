"""A bounded tool loop implementing the existing backend engine contract."""

import asyncio
import json
from typing import List

from pydantic import ValidationError

from backend.app.ml.interface import AgentTracerProtocol, MLInsightResult
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse

from .config import AgentConfig
from .context import RunContext
from .llm import FakeLLMClient, LLMClientProtocol
from .models import (
    ACTION_ADAPTER, BANK_VIEW_UNAVAILABLE, AgentContextError, AgentEvidenceError,
    AgentLLMError, AgentResponseError, AgentTimeoutError, FinalAction, ToolObservation,
)
from .prompts import build_finalization_prompt, build_planning_prompt
from .tools import AnalyticsSourceProtocol, MockAnalyticsSource, execute_tool, validate_arguments


def reject_nonfinite(value: str):
    raise ValueError("Non-finite numbers are not JSON")


class AgentInsightEngine:
    def __init__(
        self, llm_client: LLMClientProtocol | None = None, *,
        analytics_source: AnalyticsSourceProtocol | None = None, config: AgentConfig | None = None,
    ):
        self.llm_client = llm_client if llm_client is not None else FakeLLMClient()
        self.analytics_source = analytics_source if analytics_source is not None else MockAnalyticsSource()
        self.config = config if config is not None else AgentConfig()

    async def generate_insight(
        self, company: CompanyResponse, event: NewsArticleResponse,
        counterparties: List[CounterpartyItem], tracer: AgentTracerProtocol,
    ) -> MLInsightResult:
        try:
            async with asyncio.timeout(self.config.run_timeout_seconds):
                return await self._run(company, event, counterparties, tracer)
        except TimeoutError:
            await tracer.emit_step("Ошибка времени", "Превышен общий timeout запуска агента.")
            raise AgentTimeoutError("Превышен общий timeout запуска агента.") from None

    async def _run(
        self, company: CompanyResponse, event: NewsArticleResponse,
        counterparties: list[CounterpartyItem], tracer: AgentTracerProtocol,
    ) -> MLInsightResult:
        try:
            context = RunContext(company, event, counterparties, self.config)
        except AgentContextError:
            await tracer.emit_step("Ошибка контекста", "Входные данные не помещаются в допустимый контекст.")
            raise
        await tracer.emit_step("Подготовка промпта", "Подготовлен ограниченный контекст компании и новости.")
        if context.payload["limits"]["event_text_truncated"] or context.payload["limits"]["counterparties_omitted"]:
            await tracer.emit_step("Ограничение контекста", "Часть входных данных сокращена; это отмечено в контексте.")

        call_budget = min(self.config.max_steps, self.config.max_llm_calls)
        tool_calls = 0
        seen_requests: set[str] = set()
        for call_index in range(call_budget):
            final_only = (
                call_index == call_budget - 1
                or tool_calls >= self.config.max_tool_calls
                or context.force_finalize
            )
            if final_only:
                await tracer.emit_step("Финализация", "Дальнейшие инструменты недоступны; запрошен итоговый MLInsightResult.")
                prompt = build_finalization_prompt(context)
            else:
                prompt = build_planning_prompt(
                    context, steps_remaining=call_budget - call_index,
                    tools_remaining=self.config.max_tool_calls - tool_calls,
                )
            await tracer.emit_step("Вызов клиента", f"Вызов LLM {call_index + 1}/{call_budget}.")
            try:
                async with asyncio.timeout(self.config.llm_timeout_seconds):
                    response = await self.llm_client.generate(prompt)
            except TimeoutError:
                await tracer.emit_step("Ошибка LLM", "Превышен timeout вызова LLM.")
                raise AgentLLMError("Превышен timeout вызова LLM.") from None
            except Exception:
                await tracer.emit_step("Ошибка LLM", "LLM-клиент завершился ошибкой; повторов нет.")
                raise AgentLLMError("LLM-клиент завершился ошибкой; повторов нет.") from None

            try:
                if not isinstance(response, str) or len(response) > self.config.max_response_chars:
                    raise AgentResponseError("Ответ LLM отсутствует или превышает лимит размера.")
                payload = json.loads(response, parse_constant=reject_nonfinite)
                if final_only:
                    result = MLInsightResult.model_validate(payload)
                else:
                    # Preserve the original direct MLInsightResult response format too.
                    if isinstance(payload, dict) and "action" not in payload:
                        result = MLInsightResult.model_validate(payload)
                    else:
                        decision = ACTION_ADAPTER.validate_python(payload)
                        result = decision.result if isinstance(decision, FinalAction) else None
            except (ValidationError, ValueError, TypeError, RecursionError, AgentResponseError):
                message = (
                    "Финализация: LLM не вернул валидный MLInsightResult в пределах лимита размера."
                    if final_only else "LLM вернул некорректный JSON действия или MLInsightResult."
                )
                await tracer.emit_step("Ошибка валидации", message)
                raise AgentResponseError(message) from None

            if result is not None:
                try:
                    bank_cited = context.validate_evidences(result)
                    if not context.has_bank_materials:
                        if result.bank_view != BANK_VIEW_UNAVAILABLE:
                            await tracer.emit_step(
                                "Ограничение источников", "Банковских материалов нет; bank_view заменён сообщением об отсутствии позиции банка.",
                            )
                        result.bank_view = BANK_VIEW_UNAVAILABLE
                    elif result.bank_view != BANK_VIEW_UNAVAILABLE and not bank_cited:
                        raise AgentEvidenceError("Для bank_view требуется evidence на полученный банковский материал.")
                except AgentEvidenceError:
                    await tracer.emit_step("Ошибка evidence", "Происхождение evidence или банковского материала не подтверждено контекстом.")
                    raise
                if result.relevant_bank_product is not None:
                    await tracer.emit_step("Ограничение продуктов", "Каталог продуктов отсутствует; relevant_bank_product установлен в null.")
                    result.relevant_bank_product = None
                await tracer.emit_step(
                    "Проверка формата", "MLInsightResult и происхождение evidence проверены; смысл утверждений не проверялся.",
                )
                return result

            tool_calls += 1
            try:
                request_arguments = validate_arguments(decision).model_dump(mode="json")
            except ValidationError:
                request_arguments = decision.arguments
            request_key = json.dumps(
                [decision.tool, request_arguments], sort_keys=True, ensure_ascii=False,
            )
            await tracer.emit_step("Выбор инструмента", f"Запрошен {decision.tool}; запрос {tool_calls}/{self.config.max_tool_calls}.")
            if request_key in seen_requests:
                observation = ToolObservation(
                    tool=decision.tool, arguments=decision.arguments, status="error", error="DUPLICATE_REQUEST",
                )
                context.force_finalize = True
            else:
                seen_requests.add(request_key)
                observation = await execute_tool(
                    decision, source=self.analytics_source, company=company, event=event,
                    counterparties=context.counterparties, config=self.config,
                )
            try:
                observation = context.add_observation(observation)
            except AgentContextError:
                await tracer.emit_step("Ошибка контекста", "Не удалось записать наблюдение в пределах лимита контекста.")
                raise
            if observation.status == "error":
                await tracer.emit_step("Ошибка инструмента", f"{decision.tool}: {observation.error}.")
            else:
                await tracer.emit_step("Результат инструмента", f"Результат {decision.tool} добавлен в контекст.")

        raise AgentResponseError("Лимит шагов исчерпан без итогового MLInsightResult.")
