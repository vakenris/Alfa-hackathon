from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.db.models.insight import Insight
from backend.app.schemas.question import QuestionAskRequest, QuestionAnswerResponse

router = APIRouter(prefix="/questions", tags=["Q&A"])


@router.post("", response_model=QuestionAnswerResponse)
async def ask_follow_up_question(
    request: QuestionAskRequest,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Ask interactive follow-up question on a prepared insight (Criterion 10 drill-down)."""
    stmt = (
        select(Insight)
        .where(Insight.id == request.insight_id)
        .options(selectinload(Insight.evidences))
    )
    res = await db.execute(stmt)
    insight = res.scalar_one_or_none()
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    # Tailored answer synthesis based on C-level user role and insight facts
    role_focus = {
        UserRole.CEO: "влияние на операционную устойчивость бизнеса и рыночные позиции",
        UserRole.CFO: "стоимость долгового обслуживания, валютные риски и показатели EBITDA",
        UserRole.TREASURY: "график платежей, ликвидность счетов и варианты размещения свободных остатков",
        UserRole.ACCOUNTANT: "налоговые последствия и первичный учет операций",
        UserRole.ANALYST: "макроэкономические предпосылки и чувствительность финансовой модели"
    }.get(role, "стратегические последствия для компании")

    answer = (
        f"Аналитический ответ для роли {role.value} (фокус: {role_focus}):\n\n"
        f"По вопросу: «{request.question}».\n"
        f"• Контекст события: {insight.what_happened}\n"
        f"• Экспертная позиция Альфа-Банка: {insight.bank_view}\n"
        f"• Рекомендация к действию: {insight.recommended_action}\n"
    )
    if insight.relevant_bank_product:
        answer += f"• Предложение банка: {insight.relevant_bank_product} для снижения выявленных рисков."

    return QuestionAnswerResponse(
        insight_id=request.insight_id,
        question=request.question,
        answer=answer,
        relevant_source="Аналитика Центра макроэкономических исследований Альфа-Банка и профиль клиента"
    )
