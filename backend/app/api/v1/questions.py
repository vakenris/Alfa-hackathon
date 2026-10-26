from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.core.logging import logger
from backend.app.services.insight_service import InsightService
from backend.app.db.repositories.company_repo import CompanyRepository
from backend.app.schemas.company import CompanyResponse
from backend.app.schemas.question import QuestionAskRequest, QuestionAnswerResponse
from backend.app.agent.companion import companion_store

router = APIRouter(prefix="/questions", tags=["Q&A"])


@router.post("", response_model=QuestionAnswerResponse)
async def ask_follow_up_question(
    request: QuestionAskRequest,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Ask interactive follow-up question on a prepared insight (drill-down).

    Uses ManagementActionEngine over trusted companion analysis when available,
    or returns structured role-tailored answer grounded in the verified insight facts.
    """
    service = InsightService(db)
    insight = await service.get_insight(request.insight_id)
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    company_repo = CompanyRepository(db)
    company = await company_repo.get_by_id(insight.company_id)
    company_schema = CompanyResponse.model_validate(company) if company else None

    # Check for companion AgentAnalysisResult
    analysis = companion_store.get_by_insight_id(insight.id)
    if not analysis:
        analysis = companion_store.get_by_pair(insight.company_id, insight.event_id)

    if analysis and company_schema:
        try:
            from backend.app.agent.actions import ManagementActionEngine
            from backend.app.worker.tracer import AgentTracer
            from backend.app.db.repositories.job_repo import JobRepository
            from backend.app.worker.tasks import current_ml_engine

            tracer = AgentTracer(job_id=f"qa-{insight.id}", job_repo=JobRepository(db))
            action_engine = ManagementActionEngine(core=current_ml_engine)
            qa_result = await action_engine.answer_question(
                request.question,
                analysis=analysis,
                company=company_schema,
                tracer=tracer
            )
            parts = [f"Роль: {role.value} | Статус: {qa_result.status}", qa_result.summary]
            if qa_result.missing_data:
                parts += [f"Нужны данные: {item}" for item in qa_result.missing_data]
            if qa_result.proposed_actions:
                parts += [
                    f"Предложение ({item.owner_role}): {item.title}. {item.rationale}"
                    for item in qa_result.proposed_actions
                ]
            parts += qa_result.limitations
            if qa_result.disclaimer:
                parts.append(qa_result.disclaimer)

            relevant_source = "; ".join(
                f"{ref.source_title} [{ref.fact_id}]" + (f" ({ref.source_url})" if ref.source_url else "")
                for ref in qa_result.source_refs
            ) or "Аналитика Альфа-Банка и подтвержденные источники компании"

            return QuestionAnswerResponse(
                insight_id=request.insight_id,
                question=request.question,
                answer="\n\n".join(parts),
                relevant_source=relevant_source
            )
        except Exception as ex:
            logger.warning(f"ManagementActionEngine execution fallback: {ex}")

    # Grounded role-specific framing when companion is not cached
    role_focus = {
        UserRole.CEO: "Стратегический фокус: оценка влияния на бизнес-модель, контрагентов и масштабирование.",
        UserRole.CFO: "Финансовый фокус: управление ликвидностью, оптимизация оборотного капитала и процентных расходов.",
        UserRole.TREASURY: "Казначейский фокус: мониторинг денежных потоков, ковенанты и процентные риски.",
        UserRole.ACCOUNTANT: "Учетный фокус: налоговые последствия, бухгалтерские проводки и закрытие периодов.",
        UserRole.ANALYST: "Аналитический фокус: стресс-тестирование модели, чувствительность метрик и макроэкономические факторы.",
    }.get(role, f"Фокус роли {role.value}")

    evidences_str = "; ".join(
        f"{ev.source_title}: «{ev.claim[:100]}»" for ev in insight.evidences
    ) if insight.evidences else "Аналитика Альфа-Банка и финансовый профиль компании"

    answer = (
        f"Ответ для роли {role.value} на вопрос: «{request.question}».\n\n"
        f"{role_focus}\n\n"
        f"• Суть события: {insight.what_happened}\n"
        f"• Важность для бизнеса: {insight.why_important}\n"
        f"• Последствия и риски: {insight.consequences}\n"
        f"• Экспертиза Альфа-Банка: {insight.bank_view}\n"
        f"• Рекомендуемые шаги: {insight.recommended_action}\n"
        f"• Продуктовое решение: {insight.relevant_bank_product or 'Не требуется'}\n\n"
        f"Ограничение: Аналитика подготовлена на основе верифицированных данных и не является индивидуальной инвестиционной рекомендацией."
    )

    return QuestionAnswerResponse(
        insight_id=request.insight_id,
        question=request.question,
        answer=answer,
        relevant_source=evidences_str
    )
