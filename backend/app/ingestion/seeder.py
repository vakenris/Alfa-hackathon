import os
import csv
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.core.logging import logger
from backend.app.db.models.company import Company
from backend.app.db.models.transaction import CounterpartyTransaction
from backend.app.db.models.news import NewsArticle
from backend.app.db.models.agent_trace import JobRecord, AgentTraceStep
from backend.app.db.models.insight import Insight, Evidence

SAMPLE_COMPANIES = [
    {
        "synthetic_u": "U_1001",
        "name": "ПАО «Северный Металлургический Холдинг»",
        "org_type": "ПАО",
        "okfs_name": "Частная собственность",
        "company_age_years": "15+",
        "eco_mark_label": "Зрелый бизнес",
        "mainokved_code": "24.10",
        "mainokved_name": "Производство чугуна, стали и ферросплавов",
        "negative_mark": False,
        "amount_workers_fns": "5000+",
        "cl_type": "Действующий",
        "cl_product_rko_mark": True,
        "cl_product_gar_mark": True,
        "cl_product_loa_mark": True,
        "cl_product_lea_mark": True,
        "cl_product_dep_mark": False,
        "cl_product_zpp_mark": True,
        "factoring_mark": False,
        "fts_flag": True,
        "fts_country_name": "Китай, Индия, Турция",
        "revenue_company": 68500000000.0,
        "group_id": "GRP_METALL",
        "city": "Череповец",
        "region": "Вологодская область"
    },
    {
        "synthetic_u": "U_1002",
        "name": "ООО «Евразия Логистик Групп»",
        "org_type": "ООО",
        "okfs_name": "Частная собственность",
        "company_age_years": "8",
        "eco_mark_label": "Активный рост",
        "mainokved_code": "52.29",
        "mainokved_name": "Деятельность вспомогательная прочая, связанная с перевозками",
        "negative_mark": False,
        "amount_workers_fns": "1200",
        "cl_type": "Действующий",
        "cl_product_rko_mark": True,
        "cl_product_gar_mark": False,
        "cl_product_loa_mark": True,
        "cl_product_lea_mark": True,
        "cl_product_dep_mark": True,
        "cl_product_zpp_mark": True,
        "factoring_mark": True,
        "fts_flag": True,
        "fts_country_name": "Казахстан, Узбекистан",
        "revenue_company": 24300000000.0,
        "group_id": "GRP_TRANS",
        "city": "Москва",
        "region": "Москва"
    },
    {
        "synthetic_u": "U_1003",
        "name": "АО «АгроПромХолдинг Волга»",
        "org_type": "АО",
        "okfs_name": "Частная собственность",
        "company_age_years": "12",
        "eco_mark_label": "Стабильный бизнес",
        "mainokved_code": "10.61",
        "mainokved_name": "Производство продуктов мукомольной и крупяной промышленности",
        "negative_mark": False,
        "amount_workers_fns": "2800",
        "cl_type": "Действующий",
        "cl_product_rko_mark": True,
        "cl_product_gar_mark": True,
        "cl_product_loa_mark": True,
        "cl_product_lea_mark": False,
        "cl_product_dep_mark": True,
        "cl_product_zpp_mark": True,
        "factoring_mark": False,
        "fts_flag": True,
        "fts_country_name": "Египет, ОАЭ",
        "revenue_company": 37100000000.0,
        "group_id": "GRP_AGRO",
        "city": "Самара",
        "region": "Самарская область"
    }
]

SAMPLE_TRANSACTIONS = [
    {"payer_u": "U_1001", "beneficiary_u": "U_SUPP_1", "pay_amt": 5200000000.0, "pay_cnt": 142},
    {"payer_u": "U_1001", "beneficiary_u": "U_SUPP_2", "pay_amt": 3100000000.0, "pay_cnt": 87},
    {"payer_u": "U_CUST_1", "beneficiary_u": "U_1001", "pay_amt": 8900000000.0, "pay_cnt": 210},
    {"payer_u": "U_1002", "beneficiary_u": "U_SUPP_3", "pay_amt": 1800000000.0, "pay_cnt": 65},
    {"payer_u": "U_CUST_2", "beneficiary_u": "U_1002", "pay_amt": 4200000000.0, "pay_cnt": 115},
]

SAMPLE_NEWS_AND_ANALYTICS = [
    {
        "title": "Ключевая ставка ЦБ РФ: сохранение на уровне 14,0%, риторика ужесточается",
        "source": "Центр макроэкономических исследований Альфа-Банка",
        "content": "Совет директоров Банка России принял решение сохранить ключевую ставку на уровне 14% годовых. "
                   "Регулятор отметил сохранение инфляционного давления и перегрев на рынке труда. "
                   "Для корпоративного сегмента это означает продолжение периода дорогих заемных средств. "
                   "Командам казначейства рекомендуется диверсифицировать источники фондирования.",
        "summary": "ЦБ сохранил ставку 14%. Период дорогого кредитования продолжается.",
        "category": "Макроэкономика",
        "target_okved_prefix": None,
        "is_bank_analytics": True
    },
    {
        "title": "Тактический взгляд на 3-й квартал 2026 года: время собирать камни",
        "source": "Центр макроэкономических исследований Альфа-Банка",
        "content": "Аналитики Альфа-Банка выпустили ежеквартальный стратегический обзор. "
                   "Ключевые вызовы: трансформация логистических коридоров на Восток, волатильность цен на сырьевые товары и рост транзакционных издержек. "
                   "Предприятиям металлургического и аграрного секторов рекомендуется использовать инструменты факторинга и торгового финансирования.",
        "summary": "Анализ макротрендов 3-го квартала 2026 года для крупнейших экспортеров и импортеров.",
        "category": "Макроэкономика",
        "target_okved_prefix": None,
        "is_bank_analytics": True
    },
    {
        "title": "Минпромторг расширяет программы льготного лизинга для промышленного оборудования",
        "source": "Интерфакс",
        "content": "Министерство промышленности и торговли РФ объявило о дополнительном выделении 40 млрд рублей на субсидирование договоров лизинга "
                   "тяжелого промышленного и металлургического оборудования отечественного производства.",
        "summary": "Субсидии на лизинг оборудования для промышленных предприятий.",
        "category": "Господдержка",
        "target_okved_prefix": "24",
        "is_bank_analytics": False
    },
    {
        "title": "Резкий рост ставок фрахта на дальневосточном направлении",
        "source": "Коммерсантъ",
        "content": "Стоимость контейнерных железнодорожных и морских перевозок из портов Дальнего Востока в европейскую часть России выросла на 18% за август. "
                   "Операторы отмечают дефицит порожних платформ и загрузку узловых терминалов.",
        "summary": "Ставки фрахта на Дальнем Востоке выросли на 18%.",
        "category": "Логистика",
        "target_okved_prefix": "52",
        "is_bank_analytics": False
    }
]


async def seed_data(session: AsyncSession):
    """Seed initial demonstration datasets into database if empty."""
    # 1. Seed Companies
    existing_companies = await session.execute(select(Company))
    if not existing_companies.scalars().first():
        logger.info("Seeding sample companies...")
        for comp_data in SAMPLE_COMPANIES:
            session.add(Company(**comp_data))
        await session.commit()
    
    # 2. Seed Transactions
    existing_tx = await session.execute(select(CounterpartyTransaction))
    if not existing_tx.scalars().first():
        logger.info("Seeding sample counterparty transactions...")
        for tx in SAMPLE_TRANSACTIONS:
            session.add(CounterpartyTransaction(**tx))
        await session.commit()

    # 3. Seed News and Bank Analytics
    existing_news = await session.execute(select(NewsArticle))
    if not existing_news.scalars().first():
        logger.info("Seeding sample news articles and bank analytics...")
        for item in SAMPLE_NEWS_AND_ANALYTICS:
            session.add(NewsArticle(**item))
        await session.commit()

    # 4. Seed sample Job & Insight (event_id=999) for initial verification and testing
    existing_job = await session.execute(select(JobRecord).where(JobRecord.id == "test-job-uuid"))
    if not existing_job.scalars().first():
        logger.info("Seeding sample test job and trace steps...")
        job = JobRecord(
            id="test-job-uuid",
            company_id=1,
            event_id=999,
            status="SUCCESS"
        )
        session.add(job)

        step = AgentTraceStep(
            job_id="test-job-uuid",
            step_number=1,
            step_name="Анализ профиля",
            description="Профиль загружен"
        )
        session.add(step)

        ins = Insight(
            id=1,
            job_id="test-job-uuid",
            company_id=1,
            event_id=999,
            what_happened="ЦБ сохранил ставку на уровне 14%",
            why_important="Влияет на обслуживание кредитного портфеля",
            consequences="Рост стоимости заимствований",
            bank_view="Контролировать ликвидность",
            ai_view="Оптимизировать оборотный капитал",
            recommended_action="Провести совет казначейства",
            relevant_bank_product="Факторинг"
        )
        session.add(ins)

        ev = Evidence(
            insight_id=1,
            claim="Дорогой кредит",
            source_title="ЦБ РФ"
        )
        session.add(ev)
        await session.commit()

    logger.info("Database seeding completed.")
