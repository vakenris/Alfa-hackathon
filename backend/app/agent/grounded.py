"""Bounded grounded path, sharing the existing tool boundary and context clipping."""

import asyncio
import json
import math

from pydantic import ValidationError

from .context import GroundedRunContext, json_text
from .models import (
    AgentAnalysisResult, AgentContextError, AgentEvidenceError, AgentLLMError,
    AgentLLMTimeoutError, AgentResponseError, AgentError, CheckIssue, DraftInsight,
    GROUNDED_ACTION_ADAPTER, ToolAction, ToolObservation,
)
from .prompts import build_grounded_prompt
from .runtime import ObservedLLMClient, ValidationAttempt
from .tools import MockAnalyticsSource, execute_tool, validate_arguments
from .verification import project_insight, verify_draft


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def decode_action(response, *, config, draft_only=False):
    if not isinstance(response, str) or not response.strip() or len(response) > config.max_response_chars:
        raise AgentResponseError("INVALID_RESPONSE_SIZE")
    try:
        def reject(value):
            raise ValueError("Nonfinite JSON")
        def finite_float(value):
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("Nonfinite JSON number")
            return parsed
        payload = json.loads(response, parse_constant=reject, parse_float=finite_float, object_pairs_hook=_object_pairs)
        return DraftInsight.model_validate(payload) if draft_only else GROUNDED_ACTION_ADAPTER.validate_python(payload)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise AgentResponseError("INVALID_GROUNDED_JSON") from None


async def emit_safe(tracer, config, name, description):
    try:
        async with asyncio.timeout(config.trace_timeout_seconds):
            await tracer.emit_step(name, description)
    except Exception:
        raise AgentError("TRACE_UNAVAILABLE") from None


async def run_grounded(engine, company, event, counterparties, tracer, telemetry):
    config = engine.config
    # Providers cannot mutate the caller's DTOs while executing a tool.
    company = company.model_copy(deep=True)
    event = event.model_copy(deep=True)
    counterparties = [item.model_copy(deep=True) for item in counterparties]
    context = GroundedRunContext(company, event, counterparties, config)
    observed = ObservedLLMClient(engine.llm_client, telemetry=telemetry)
    call_limit = min(config.max_steps, config.max_llm_calls)
    seen = set()
    tools_used = 0
    recovery_used = False
    repairs = 0
    searches_after_draft = 0
    draft = None
    issues = None
    available_tools = []
    # Only the exact fixed fixture can be known empty without performing I/O.
    # Custom/production sources remain searchable, including empty query results.
    if not (type(engine.analytics_source) is MockAnalyticsSource and not engine.analytics_source.search_available):
        available_tools.append("get_bank_analytics")
    if context.counterparties:
        available_tools.append("summarize_counterparties")
    phase = "gather" if call_limit > 1 and config.max_tool_calls and available_tools else "draft"
    await emit_safe(tracer, config, "Бизнес-контекст", "Подготовлены известные признаки, пробелы и ограничения данных.")
    for index in range(call_limit):
        remaining = call_limit - index
        # Reserve a draft and, where possible, one repair rather than spending every slot on gathering.
        reserved = 1 if recovery_used and draft is not None else 2
        if phase == "gather" and (remaining <= reserved and index > 0 or tools_used >= config.max_tool_calls or context.force_finalize or not available_tools):
            phase = "draft"
        prompt = build_grounded_prompt(
            context, phase=phase, remaining=remaining,
            tools_remaining=config.max_tool_calls - tools_used, draft=draft if phase == "repair" else None,
            issues=issues if phase == "repair" else None,
            available_tools=available_tools,
        )
        if len(json_text(context.payload)) > config.max_context_chars:
            raise AgentContextError("ACCUMULATED_CONTEXT_LIMIT")
        if len(prompt) > config.max_prompt_chars:
            raise AgentContextError("FULL_PROMPT_LIMIT")
        await emit_safe(tracer, config, "Вызов LLM", f"Этап {phase}; вызов {index + 1}/{call_limit}.")
        try:
            async with asyncio.timeout(config.llm_timeout_seconds) as deadline:
                # Shared low-level engines serialize calls through capture of last_* metadata.
                async with engine._llm_lock:
                    response = await observed.generate(prompt)
        except TimeoutError:
            if deadline.expired():
                telemetry.record_deadline("llm_deadline")
                raise AgentLLMTimeoutError("LLM_DEADLINE") from None
            raise AgentLLMError("LLM_CALL_FAILED") from None
        except Exception:
            raise AgentLLMError("LLM_CALL_FAILED") from None
        try:
            decision = decode_action(response, config=config, draft_only=phase != "gather")
            if isinstance(decision, ToolAction) and decision.tool not in available_tools:
                raise AgentResponseError("UNAVAILABLE_TOOL")
        except AgentResponseError as error:
            telemetry.validation_attempts.append(ValidationAttempt(
                call_number=index + 1, phase=phase, issue_codes=[str(error)], facts=0, impacts=0,
            ))
            if remaining > 1 and not recovery_used and config.max_recovery_attempts:
                recovery_used = True
                repairs += 1
                issues = [CheckIssue(code=str(error))]
                phase = "repair"
                await emit_safe(tracer, config, "Исправление", "Запрошено единственное исправление структуры ответа.")
                continue
            raise
        if isinstance(decision, ToolAction):
            tools_used += 1
            try:
                args = validate_arguments(decision).model_dump(mode="json")
            except ValidationError:
                args = decision.arguments
            key = json_text([decision.tool, args]) if not args else json.dumps([decision.tool, args], sort_keys=True, ensure_ascii=False)
            if key in seen:
                observation = ToolObservation(tool=decision.tool, arguments={}, status="error", error="DUPLICATE_REQUEST")
                context.force_finalize = True
            else:
                seen.add(key)
                observation = await execute_tool(decision, source=engine.analytics_source, company=company,
                                                 event=event, counterparties=context.counterparties, config=config)
            observation = context.add_observation(observation)
            context.payload["analysis_plan"]["completed_tools"].append({
                "tool": decision.tool, "status": observation.status, "error": observation.error,
            })
            await emit_safe(tracer, config, "Инструмент", f"{decision.tool}: {observation.status}; запрос {tools_used}.")
            # A recovery search must be followed by a draft, not another search.
            if draft is not None:
                searches_after_draft += 1
                phase = "draft"
            continue
        draft = decision
        verification = verify_draft(draft, context)
        telemetry.validation_attempts.append(ValidationAttempt(
            call_number=index + 1, phase=phase, issue_codes=[item.code for item in verification.issues],
            facts=len(draft.facts), impacts=len(draft.impacts),
        ))
        await emit_safe(tracer, config, "Проверка оснований", f"Фактов: {verification.checked_facts}; нарушений: {len(verification.issues)}.")
        if verification.passed:
            result = project_insight(draft, context, verification)
            report = telemetry.build_report()
            report.validation_summary = {"passed": True, "facts": len(draft.facts), "impacts": len(draft.impacts),
                                         "repairs": repairs, "recovery_searches": searches_after_draft, "tool_calls": tools_used}
            report.sources_summary = {"visible": len(context.sources), "verified_bank": sum(
                bool(item.is_bank_analytics and item.provenance_status == "verified" and item.source_document_id) for item in context.sources.values())}
            return AgentAnalysisResult(company_id=company.id, event_id=event.id, insight=result, analysis=draft,
                                       business_context=context.business, sources=list(context.sources.values()),
                                       verification=verification, report=report.model_dump(mode="json"))
        if remaining <= 1 or recovery_used or not config.max_recovery_attempts:
            raise AgentEvidenceError("GROUNDED_VERIFICATION_FAILED")
        recovery_used = True
        issues = verification.issues
        # Only source/contradiction gaps justify extra retrieval. It costs a selection and a redraft.
        can_search = (remaining >= 3 and tools_used < config.max_tool_calls and not context.force_finalize
                      and "get_bank_analytics" in available_tools)
        if can_search and any(item.code in ("UNKNOWN_SOURCE", "UNACKNOWLEDGED_CONTRADICTION") for item in issues):
            context.payload["recovery"] = {"issue_codes": [item.code for item in issues], "strategy": "search_then_redraft"}
            phase = "gather"
        else:
            repairs += 1
            phase = "repair"
        await emit_safe(tracer, config, "Исправление", "Выполняется единственная ограниченная recovery-ветка.")
    raise AgentResponseError("RUN_CALL_LIMIT")
