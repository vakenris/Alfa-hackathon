from typing import List, Optional, Protocol, runtime_checkable
from pydantic import BaseModel
from backend.app.schemas.company import CompanyResponse, CounterpartyItem
from backend.app.schemas.event import NewsArticleResponse
from backend.app.schemas.insight import EvidenceItem


class MLInsightResult(BaseModel):
    what_happened: str
    why_important: str
    consequences: str
    bank_view: str
    ai_view: str
    recommended_action: str
    relevant_bank_product: Optional[str] = None
    evidences: List[EvidenceItem] = []


@runtime_checkable
class AgentTracerProtocol(Protocol):
    async def emit_step(self, step_name: str, description: str):
        ...


@runtime_checkable
class MLInsightEngineProtocol(Protocol):
    async def generate_insight(
        self,
        company: CompanyResponse,
        event: NewsArticleResponse,
        counterparties: List[CounterpartyItem],
        tracer: AgentTracerProtocol
    ) -> MLInsightResult:
        ...


class MockMLInsightEngine:
    """Default mock implementation of ML engine for backend testing & integration."""
    async def generate_insight(
        self,
        company: CompanyResponse,
        event: NewsArticleResponse,
        counterparties: List[CounterpartyItem],
        tracer: AgentTracerProtocol
    ) -> MLInsightResult:
        await tracer.emit_step(
            step_name="Шаг 1: Сбор контекста",
            description=f"Загружен профиль компании {company.name} (ОКВЭД: {company.mainokved_name}, выручка: {company.revenue_company:,.0f} руб.)."
        )
        
        await tracer.emit_step(
            step_name="Шаг 2: Анализ цепочки связей",
            description=f"Проанализировано {len(counterparties)} ключевых контрагентов компании."
        )

        await tracer.emit_step(
            step_name="Шаг 3: Сопоставление с аналитикой Альфа-Банка",
            description="Извлечены релевантные тезисы макроэкономического обзора Центра исследований Альфа-Банка."
        )

        await tracer.emit_step(
            step_name="Шаг 4: Самопроверка и синтез выводов",
            description="Проведена проверка фактов: отделена официальная позиция банка от экспертных выводов AI."
        )

        # Realistic mock response tailored to company context
        what_happened = (
            f"Опубликовано значимое событие: «{event.title}». "
            "Ключевые факты зафиксированы из официальных источников."
        )
        
        has_loans = company.cl_product_loa_mark
        loan_context = "Компания имеет действующие кредитные обязательства" if has_loans else "Компания опирается на операционный денежный поток"

        why_important = (
            f"Для бизнеса {company.name} ({company.mainokved_name or 'крупный бизнес'}) событие имеет критический характер. "
            f"{loan_context}, а в структуре расчетов участвуют контрагенты с общим объемом оборота свыше {sum(c.total_amount for c in counterparties):,.0f} руб."
        )

        consequences = (
            "1. Риск изменения стоимости заемного финансирования и пересмотра условий договоров.\n"
            "2. Возможность оптимизации оборотного капитала за счет безрегрессного факторинга и гибких депозитных траншей."
        )

        bank_view = (
            "Позиция Центра макроэкономических исследований Альфа-Банка: "
            "в условиях жесткой денежно-кредитной политики ключевым приоритетом становится контроль ликвидности и диверсификация цепочек поставок."
        )

        ai_view = (
            "Сгенерированный AI-анализ: компании рекомендуется оперативно актуализировать платежный календарь на ближайшие 2 квартала "
            "и хеджировать процентные и валютные риски."
        )

        recommended_action = (
            "Провести внеочередной финансовый комитет казначейства, пересмотреть лимиты овердрафта и запросить у Альфа-Банка индивидуальный график рефинансирования."
        )

        product = "Факторинг и Овердрафт для крупного бизнеса" if not company.factoring_mark else "Хеджирование процентных ставок"

        evidences = [
            EvidenceItem(
                claim="Высокая чувствительность к стоимости фондирования",
                source_title="Кредитный профиль клиента",
                company_feature=f"Кредиты={company.cl_product_loa_mark}, Выручка={company.revenue_company}"
            ),
            EvidenceItem(
                claim="Макроэкономический ориентир Альфа-Банка",
                source_title="Тактический взгляд ЦМИ Альфа-Банка на 2026 год",
                source_url="https://alfabank.ru/corporate/analytics"
            )
        ]

        return MLInsightResult(
            what_happened=what_happened,
            why_important=why_important,
            consequences=consequences,
            bank_view=bank_view,
            ai_view=ai_view,
            recommended_action=recommended_action,
            relevant_bank_product=product,
            evidences=evidences
        )
