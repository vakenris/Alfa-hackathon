"""Company relationships through shared anonymized people; not ownership."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.app.db.models.company import Company
from backend.app.db.models.person_link import PersonCompanyLink


class PersonLinkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_people(
        self,
        synthetic_u: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        self._validate(synthetic_u, limit)

        if type(offset) is not int or offset < 0:
            raise ValueError(
                "offset должен быть неотрицательным целым числом"
            )

        statement = (
            select(
                PersonCompanyLink.synthetic_f,
                PersonCompanyLink.city,
                PersonCompanyLink.region,
                PersonCompanyLink.location_conflict,
            )
            .where(PersonCompanyLink.synthetic_u == synthetic_u)
            .order_by(PersonCompanyLink.synthetic_f)
            .offset(offset)
            .limit(limit)
        )

        rows = (
            await self.session.execute(statement)
        ).mappings().all()

        return [dict(row) for row in rows]

    async def related_companies(
        self,
        synthetic_u: str,
        *,
        limit: int = 20,
    ) -> list[dict]:
        self._validate(synthetic_u, limit)

        own = aliased(PersonCompanyLink)
        other = aliased(PersonCompanyLink)

        count = func.count(
            func.distinct(own.synthetic_f)
        ).label("shared_people")

        statement = (
            select(
                other.synthetic_u.label("synthetic_u"),
                Company.name,
                count,
            )
            .select_from(own)
            .join(other, other.synthetic_f == own.synthetic_f)
            .outerjoin(
                Company,
                Company.synthetic_u == other.synthetic_u,
            )
            .where(
                own.synthetic_u == synthetic_u,
                other.synthetic_u != synthetic_u,
            )
            .group_by(other.synthetic_u, Company.name)
            .order_by(count.desc(), other.synthetic_u)
            .limit(limit)
        )

        rows = (
            await self.session.execute(statement)
        ).mappings().all()

        return [dict(row) for row in rows]

    @staticmethod
    def _validate(synthetic_u: str, limit: int) -> None:
        if (
            not isinstance(synthetic_u, str)
            or not synthetic_u.strip()
            or len(synthetic_u) > 64
        ):
            raise ValueError(
                "Нужен идентификатор компании длиной до 64 символов"
            )

        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit должен быть от 1 до 200")