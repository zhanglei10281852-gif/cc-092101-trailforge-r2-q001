from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge.errors import NotFoundError
from trailforge.models.routes import RouteRevision


class RevisionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, route_id: int, revision_no: int) -> RouteRevision | None:
        return self.session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == route_id,
                RouteRevision.revision_no == revision_no,
            )
        )

    def require(self, route_id: int, revision_no: int) -> RouteRevision:
        revision = self.get(route_id, revision_no)
        if revision is None:
            raise NotFoundError(
                f"RouteRevision {revision_no} of route {route_id} was not found",
                context={"route_id": route_id, "revision_no": revision_no},
            )
        return revision

    def list_for_route(self, route_id: int) -> list[RouteRevision]:
        return list(
            self.session.scalars(
                select(RouteRevision)
                .where(RouteRevision.route_id == route_id)
                .order_by(RouteRevision.revision_no)
            )
        )

    def latest_no(self, route_id: int) -> int | None:
        return self.session.scalar(
            select(func.max(RouteRevision.revision_no)).where(RouteRevision.route_id == route_id)
        )
