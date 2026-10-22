from sqlalchemy import Column, Integer, String, Float, Boolean
from backend.app.db.base import Base


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    synthetic_u = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    org_type = Column(String(128), nullable=True)
    okfs_name = Column(String(128), nullable=True)
    company_age_years = Column(String(64), nullable=True)
    eco_mark_label = Column(String(128), nullable=True)
    mainokved_code = Column(String(32), index=True, nullable=True)
    mainokved_name = Column(String(512), nullable=True)
    negative_mark = Column(Boolean, default=False)
    amount_workers_fns = Column(String(64), nullable=True)
    amount_workers_spark = Column(String(64), nullable=True)
    cl_type = Column(String(64), nullable=True)  # Действующий / Потенциальный
    
    # Banking products
    cl_product_rko_mark = Column(Boolean, default=False)
    cl_product_gar_mark = Column(Boolean, default=False)
    cl_product_loa_mark = Column(Boolean, default=False)  # Кредитные продукты
    cl_product_lea_mark = Column(Boolean, default=False)  # Лизинг
    cl_product_dep_mark = Column(Boolean, default=False)  # Депозиты
    cl_product_zpp_mark = Column(Boolean, default=False)  # Зарплатный проект
    factoring_mark = Column(Boolean, default=False)       # Факторинг
    
    fts_flag = Column(Boolean, default=False)
    fts_country_name = Column(String(128), nullable=True)
    revenue_company = Column(Float, nullable=True)
    group_id = Column(String(64), index=True, nullable=True)
    city = Column(String(128), nullable=True)
    region = Column(String(128), nullable=True)
