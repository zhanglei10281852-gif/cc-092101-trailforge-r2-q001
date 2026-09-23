from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from trailforge.models.revisions import RouteRevision
from trailforge.repositories.base import BaseRepository


class RouteRevisionRepository(BaseRepository[RouteRevision]):
    model = RouteRevision
    sortable = {
        "version_number": RouteRevision.version_number,
        "published_at": RouteRevision.published_at,
    }

    @staticmethod
    def _with_children(statement):
        return statement.options(
            selectinload(RouteRevision.segments),
            selectinload(RouteRevision.points),
            selectinload(RouteRevision.risk_tags),
        )

    def get_by_version(self, route_id: int, version_number: int) -> RouteRevision | None:
        statement = self._with_children(
            select(RouteRevision).where(
                RouteRevision.route_id == route_id,
                RouteRevision.version_number == version_number,
            )
        )
        return self.session.scalar(statement)

    def get_detail(self, revision_id: int) -> RouteRevision | None:
        statement = self._with_children(
            select(RouteRevision).where(RouteRevision.id == revision_id)
        )
        return self.session.scalar(statement)

    def list_for_route(self, route_id: int) -> list[RouteRevision]:
        statement = (
            select(RouteRevision)
            .where(RouteRevision.route_id == route_id)
            .order_by(RouteRevision.version_number.asc())
        )
        return list(self.session.scalars(statement))

    def latest_for_route(self, route_id: int) -> RouteRevision | None:
        statement = self._with_children(
            select(RouteRevision)
            .where(RouteRevision.route_id == route_id)
            .order_by(RouteRevision.version_number.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def latest_for_routes(self, route_ids: list[int]) -> dict[int, RouteRevision]:
        """Batch-fetch the highest-version revision per route for list views."""
        if not route_ids:
            return {}
        latest_numbers = (
            select(
                RouteRevision.route_id.label("route_id"),
                func.max(RouteRevision.version_number).label("version_number"),
            )
            .where(RouteRevision.route_id.in_(route_ids))
            .group_by(RouteRevision.route_id)
            .subquery()
        )
        statement = self._with_children(
            select(RouteRevision).join(
                latest_numbers,
                (RouteRevision.route_id == latest_numbers.c.route_id)
                & (RouteRevision.version_number == latest_numbers.c.version_number),
            )
        )
        return {revision.route_id: revision for revision in self.session.scalars(statement)}

    def next_version_number(self, route_id: int) -> int:
        statement = select(func.coalesce(func.max(RouteRevision.version_number), 0)).where(
            RouteRevision.route_id == route_id
        )
        return int(self.session.scalar(statement) or 0) + 1
