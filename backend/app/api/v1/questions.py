from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.api.deps import get_db, get_current_user_role, UserRole
from backend.app.services.insight_service import InsightService
from backend.app.schemas.question import QuestionAskRequest, QuestionAnswerResponse

router = APIRouter(prefix="/questions", tags=["Q&A"])


@router.post("", response_model=QuestionAnswerResponse)
async def ask_follow_up_question(
    request: QuestionAskRequest,
    db: AsyncSession = Depends(get_db),
    role: UserRole = Depends(get_current_user_role)
):
    """Ask interactive follow-up question on a prepared insight (drill-down)."""
    service = InsightService(db)
    insight = await service.get_insight(request.insight_id)
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")

    # Structured answer generation referencing the insight and bank expertise
    answer = (
        f"Ответ для роли {role.value} на вопрос: «{request.question}».\n"
        f"На основе анализа инсайта #{insight.id}: позиция банка ({insight.bank_view[:120]}...) "
        f"подтверждает, что ключевым фактором остается операционная ликвидность. "
        f"Рекомендуемое действие: {insight.recommended_action}"
    )

    return QuestionAnswerResponse(
        insight_id=request.insight_id,
        question=request.question,
        answer=answer,
        relevant_source="Аналитика Альфа-Банка и финансовый профиль компании"
    )
