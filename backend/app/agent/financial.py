"""Explicit caller-supplied inputs, Decimal arithmetic, and inspectable provenance."""

from decimal import Decimal, localcontext

from .action_models import FinancialInputs, LoanSensitivityInput
from .context import json_text
from .models import AgentContextError, Evidence, GroundedSource


def financial_calculations(company_id, *, financial_inputs=None, loan_sensitivity=None) -> list[dict]:
    """No debt, contract rate, investment share, or input is inferred from a news/product flag."""
    provided = []
    for item, model in ((financial_inputs, FinancialInputs), (loan_sensitivity, LoanSensitivityInput)):
        if item is None:
            provided.append(None)
            continue
        value = model.model_validate(item.model_dump(mode="python") if isinstance(item, model) else item)
        if value.company_id != company_id:
            raise ValueError("FINANCIAL_COMPANY_MISMATCH")
        provided.append(value)
    financial, loan = provided
    result = []
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        if loan is not None:
            if loan.rate_type == "fixed" and loan.delta_contract_rate != 0:
                raise ValueError("FIXED_RATE_RENEGOTIATION_TERMS_REQUIRED")
            amount = loan.principal * loan.delta_contract_rate * Decimal(loan.days) / Decimal(loan.day_count_basis)
            result.append({
                "kind": "simple_interest_cost_delta", "company_id": company_id,
                "formula": "principal * delta_contract_rate * days / day_count_basis",
                **loan.model_dump(mode="json", exclude={"company_id"}), "amount": str(amount),
                "is_financial_forecast": False,
                "description": (
                    f"Чувствительность простых процентных расходов: {loan.principal} × "
                    f"{loan.delta_contract_rate} × {loan.days} / {loan.day_count_basis} = {amount} {loan.currency}. "
                    f"Параметры: {loan.provenance.data_mode}; заявлены пользователем ({loan.provenance.reference}), "
                    "независимо не проверены. Договорная разница ставки применяется ко всему указанному периоду; "
                    "комиссии, капитализация и погашение не учитываются. Это не прогноз изменения ключевой ставки."
                ),
            })
        if financial is not None and financial.provenance.data_mode == "actual":
            amount = financial.available_cash - financial.planned_capex
            result.append({
                "kind": "cash_after_planned_capex", "company_id": company_id,
                "formula": "available_cash - planned_capex", **financial.model_dump(mode="json", exclude={"company_id"}),
                "amount": str(amount), "is_financial_forecast": False,
                "description": (
                    f"Сопоставление переданных средств и CAPEX: {financial.available_cash} − "
                    f"{financial.planned_capex} = {amount} {financial.currency}. "
                    f"Параметры объявлены пользователем фактическими ({financial.provenance.reference}); "
                    "независимо не проверены. Прочие обязательства и источники финансирования неизвестны; "
                    "результат не характеризует полный бюджет или платёжеспособность."
                ),
            })
    return result


def add_calculations(context, *, financial_inputs=None, loan_sensitivity=None) -> list[dict]:
    """Add only supplied arithmetic as visible evidence; retain the source registry contract."""
    try:
        calculations = financial_calculations(context.company.id, financial_inputs=financial_inputs,
                                               loan_sensitivity=loan_sensitivity)
    except (ValueError, TypeError):
        raise AgentContextError("INVALID_OR_UNSUPPORTED_FINANCIAL_INPUTS") from None
    entries = []
    for calculation in calculations:
        identifier = "calculation:" + calculation["kind"]
        if identifier in context.evidence:
            raise AgentContextError("DUPLICATE_CALCULATION")
        source = GroundedSource(source_id=f"calculation:{context.company.id}:{calculation['kind']}",
                                source_title="Расчёт Python по параметрам пользователя")
        entry = Evidence(id=identifier, source_id=source.source_id, source_title=source.source_title,
                         quote=json_text(calculation), kind="calculation", provenance_status="unverified")
        entries.append((source, entry))
    # Validate the complete replacement before mutating the context.
    payload = {**context.payload, "calculations": calculations,
               "evidence": [item.model_dump(mode="json", exclude_none=True)
                            for item in [*context.evidence.values(), *(entry for _, entry in entries)]]}
    json_text(payload)
    for source, entry in entries:
        context.sources.append(source)
        context.evidence[entry.id] = entry
    context.payload = payload
    return calculations
