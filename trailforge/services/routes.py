from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import AuditAction, PointType
from trailforge.errors import ConflictError, NotFoundError, ValidationError
from trailforge.models.routes import RiskTag, RoutePoint, RouteRevision, RouteSegment, TrailRoute
from trailforge.repositories.base import apply_version
from trailforge.repositories.revisions import RevisionRepository
from trailforge.repositories.routes import RouteRepository
from trailforge.revisioning import (
    ROUTE_SCALAR_FIELDS,
    diff_snapshots,
    revision_snapshot,
    route_snapshot,
)
from trailforge.schemas.common import Page
from trailforge.schemas.routes import (
    DeriveDraftRequest,
    RevisionDiff,
    RiskTagCreate,
    RiskTagResponse,
    RouteFilter,
    RoutePublishRequest,
    RouteRevisionResponse,
    RouteRevisionSummary,
    TrailRouteCreate,
    TrailRouteResponse,
    TrailRouteUpdate,
)
from trailforge.services.base import ServiceBase


class RouteService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.routes = RouteRepository(session)
        self.revisions = RevisionRepository(session)

    def create_risk_tag(self, data: RiskTagCreate, *, actor_id: int) -> RiskTagResponse:
        if self.routes.get_risk_tag_by_code(data.code) is not None:
            raise ConflictError("risk tag code already exists", context={"code": data.code})
        tag = RiskTag(**data.model_dump())
        self.session.add(tag)
        self.session.flush()
        self.audit(
            actor_id=actor_id,
            entity_type="risk_tag",
            entity_id=tag.id,
            action=AuditAction.CREATED,
            after=self.snapshot(tag),
        )
        return RiskTagResponse.model_validate(tag)

    def list_risk_tags(self) -> list[RiskTagResponse]:
        return [RiskTagResponse.model_validate(item) for item in self.routes.list_risk_tags()]

    def create_route(self, data: TrailRouteCreate, *, actor_id: int) -> TrailRouteResponse:
        tags = self.routes.get_risk_tags(data.risk_tag_ids)
        if len(tags) != len(data.risk_tag_ids):
            found = {item.id for item in tags}
            missing = sorted(set(data.risk_tag_ids) - found)
            raise ValidationError(
                "one or more risk tags do not exist", context={"missing": missing}
            )
        route_data = data.model_dump(exclude={"segments", "points", "risk_tag_ids", "is_published"})
        route = TrailRoute(**route_data)
        route.segments = [RouteSegment(**item.model_dump()) for item in data.segments]
        route.points = [RoutePoint(**item.model_dump()) for item in data.points]
        try:
            with self.session.begin_nested():
                self.session.add(route)
                route.risk_tags = tags
                self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("route name must be unique within a region") from exc
        self.audit(
            actor_id=actor_id,
            entity_type="trail_route",
            entity_id=route.id,
            action=AuditAction.CREATED,
            after=self.snapshot(route),
            context={
                "segment_count": len(route.segments),
                "point_count": len(route.points),
                "risk_tags": [tag.code for tag in tags],
            },
        )
        if data.is_published:
            if not route.segments:
                raise ValidationError("cannot publish a route that has no segments")
            self._freeze_revision(route, actor_id=actor_id)
        return self.get_route(route.id)

    def get_route(self, route_id: int) -> TrailRouteResponse:
        route = self.routes.get_detail(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        return self._route_response(route)

    def get_draft(self, route_id: int) -> TrailRouteResponse:
        route = self.routes.get_detail(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        return self._route_response(route, draft=True)

    def list_routes(self, filters: RouteFilter) -> Page[TrailRouteResponse]:
        result = self.routes.list_routes(filters)
        current = self.routes.current_revisions([item.id for item in result.items])
        return Page[TrailRouteResponse].build(
            [self._route_response(item, current.get(item.id)) for item in result.items],
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )

    def update_route(
        self, route_id: int, data: TrailRouteUpdate, *, actor_id: int
    ) -> TrailRouteResponse:
        route = self.routes.get_detail(route_id, for_update=True)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        apply_version(route, data.expected_version)
        before = self.snapshot(route)
        changes = data.model_dump(
            exclude_unset=True,
            exclude={"risk_tag_ids", "expected_version"},
        )
        for field, value in changes.items():
            setattr(route, field, value)
        if data.risk_tag_ids is not None:
            tags = self.routes.get_risk_tags(data.risk_tag_ids)
            if len(tags) != len(data.risk_tag_ids):
                raise ValidationError("one or more risk tags do not exist")
            route.risk_tags = tags
        self._validate_existing_geometry(route)
        self.session.flush()
        self.audit(
            actor_id=actor_id,
            entity_type="trail_route",
            entity_id=route.id,
            action=AuditAction.UPDATED,
            before=before,
            after=self.snapshot(route),
            context={"working_copy": "draft"},
        )
        return self.get_draft(route.id)

    def publish_route(
        self, route_id: int, data: RoutePublishRequest, *, actor_id: int
    ) -> RouteRevisionResponse:
        """Freeze the current draft into the next immutable revision.

        Concurrency control is a compare-and-swap claim: the draft is validated
        normally, then a single conditional UPDATE advances
        current_revision_no. Two concurrent publishers both compute the same
        next number: on SQLite/WAL the loser either re-evaluates the predicate
        against the winner's commit (0 rows updated) or hits a stale-snapshot
        write conflict, and both paths surface the same comprehensible 409.
        """
        route = self.routes.get_detail(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        if not route.segments:
            raise ValidationError("cannot publish a route that has no segments")
        self._validate_existing_geometry(route)
        if data.expected_version is not None and route.version != data.expected_version:
            raise ConflictError(
                "the draft changed before this publish completed; refetch the draft and "
                "retry with its current version",
                context={
                    "expected_version": data.expected_version,
                    "current_version": route.version,
                },
            )
        base_revision_no = route.current_revision_no or 0
        revision_no = base_revision_no + 1
        timestamp = utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")
        try:
            claimed = self.session.execute(
                text(
                    "UPDATE trail_routes "
                    "SET current_revision_no = :next_no, version = version + 1, "
                    "updated_at = :now "
                    "WHERE id = :route_id "
                    "AND COALESCE(current_revision_no, 0) = :base_no "
                    "AND (:expected_version IS NULL OR version = :expected_version)"
                ),
                {
                    "next_no": revision_no,
                    "now": timestamp,
                    "route_id": route_id,
                    "base_no": base_revision_no,
                    "expected_version": data.expected_version,
                },
            )
        except OperationalError as exc:
            # SQLite WAL stale-snapshot conflict: another writer committed
            # between our read and this write.
            if "locked" not in str(exc).lower():
                raise
            raise ConflictError(
                "another revision of this route was published concurrently; only one request "
                "can claim the next version number. Refetch the latest revision and publish "
                "again if the new draft still needs releasing",
                context={
                    "route_id": route_id,
                    "attempted_revision_no": revision_no,
                },
            ) from exc
        if claimed.rowcount != 1:
            self._raise_publish_conflict(route_id, data.expected_version, revision_no)

        # The claim bypassed the ORM; reload the route graph before snapshotting.
        self.session.expire_all()
        route = self.routes.get_detail(route_id)
        revision = self._build_revision(
            route,
            revision_no=revision_no,
            actor_id=actor_id,
            change_summary=data.change_summary.strip(),
        )
        route.draft_source_revision_no = None
        self.session.flush()
        return self._revision_response(revision)

    def _freeze_revision(self, route: TrailRoute, *, actor_id: int) -> RouteRevision:
        """Publish a brand-new route inside its creation transaction."""
        revision_no = (route.current_revision_no or 0) + 1
        revision = self._build_revision(
            route,
            revision_no=revision_no,
            actor_id=actor_id,
            change_summary="",
        )
        route.current_revision_no = revision_no
        route.draft_source_revision_no = None
        self.session.flush()
        return revision

    def _build_revision(
        self,
        route: TrailRoute,
        *,
        revision_no: int,
        actor_id: int,
        change_summary: str,
    ) -> RouteRevision:
        snapshot = route_snapshot(route)
        revision = RouteRevision(
            route_id=route.id,
            revision_no=revision_no,
            published_by=actor_id,
            derived_from_revision_no=route.draft_source_revision_no,
            change_summary=change_summary,
            **{field: snapshot[field] for field in ROUTE_SCALAR_FIELDS},
            segments_json=snapshot["segments"],
            points_json=snapshot["points"],
            risk_tags_json=snapshot["risk_tags"],
        )
        self.session.add(revision)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "another revision of this route was published concurrently; refetch the "
                "latest revision and publish again if the new draft still needs releasing",
                context={"route_id": route.id, "attempted_revision_no": revision_no},
            ) from exc
        self.audit(
            actor_id=actor_id,
            entity_type="route_revision",
            entity_id=revision.id,
            action=AuditAction.PUBLISHED,
            after={"revision_no": revision_no, **snapshot},
            context={
                "route_id": route.id,
                "revision_no": revision_no,
                "derived_from_revision_no": revision.derived_from_revision_no,
                "change_summary": change_summary,
                "segment_count": len(revision.segments_json),
                "point_count": len(revision.points_json),
                "risk_tags": revision.risk_tag_codes,
            },
        )
        return revision

    def _raise_publish_conflict(
        self, route_id: int, expected_version: int | None, attempted_revision_no: int
    ) -> None:
        current = self.routes.get(route_id)
        if current is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        if expected_version is not None and current.version != expected_version:
            raise ConflictError(
                "the draft changed before this publish completed; refetch the draft and "
                "retry with its current version",
                context={
                    "expected_version": expected_version,
                    "current_version": current.version,
                },
            )
        raise ConflictError(
            "another revision of this route was published concurrently; only one request can "
            "claim the next version number, and this one lost. Refetch the latest revision and "
            "publish again if the new draft still needs releasing",
            context={
                "route_id": route_id,
                "attempted_revision_no": attempted_revision_no,
                "current_revision_no": current.current_revision_no,
            },
        )

    def list_revisions(self, route_id: int) -> list[RouteRevisionSummary]:
        route = self.routes.get(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        return [self._revision_summary(item) for item in self.revisions.list_for_route(route_id)]

    def get_revision(self, route_id: int, revision_no: int) -> RouteRevisionResponse:
        self.routes.require(route_id)
        revision = self.revisions.require(route_id, revision_no)
        return self._revision_response(revision)

    def diff_revisions(
        self, route_id: int, from_revision_no: int, to_revision_no: int
    ) -> RevisionDiff:
        self.routes.require(route_id)
        old = revision_snapshot(self.revisions.require(route_id, from_revision_no))
        new = revision_snapshot(self.revisions.require(route_id, to_revision_no))
        changes = diff_snapshots(old, new)
        return RevisionDiff(
            route_id=route_id,
            from_revision_no=from_revision_no,
            to_revision_no=to_revision_no,
            **changes,
        )

    def derive_draft(
        self, route_id: int, data: DeriveDraftRequest, *, actor_id: int
    ) -> TrailRouteResponse:
        """Reset the editable draft working copy to a published revision."""
        route = self.routes.get_detail(route_id, for_update=True)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        if route.current_revision_no is None:
            raise ValidationError("route has no published revision to derive a draft from")
        revision = self.revisions.require(route_id, data.revision_no)
        snapshot = revision_snapshot(revision)
        tag_ids = [item["id"] for item in snapshot["risk_tags"]]
        tags = self.routes.get_risk_tags(tag_ids)
        if len(tags) != len(tag_ids):
            raise ConflictError("revision references risk tags that no longer exist")
        for field in ROUTE_SCALAR_FIELDS:
            setattr(route, field, snapshot[field])
        # Delete the working-copy children and flush before inserting the
        # derived ones, otherwise the (route_id, sequence) unique constraint
        # fires because the unit of work inserts before it deletes.
        for child in list(route.segments):
            self.session.delete(child)
        for child in list(route.points):
            self.session.delete(child)
        route.segments = []
        route.points = []
        self.session.flush()
        route.segments = [RouteSegment(**item) for item in snapshot["segments"]]
        route.points = [RoutePoint(**item) for item in snapshot["points"]]
        route.risk_tags = tags
        route.draft_source_revision_no = revision.revision_no
        apply_version(route, None)
        self.session.flush()
        self.audit(
            actor_id=actor_id,
            entity_type="trail_route",
            entity_id=route.id,
            action=AuditAction.DRAFT_DERIVED,
            after=self.snapshot(route),
            context={"source_revision_no": revision.revision_no},
        )
        return self.get_draft(route.id)

    def _route_response(
        self, route: TrailRoute, current: RouteRevision | None = None, *, draft: bool = False
    ) -> TrailRouteResponse:
        """Project a route response.

        Published routes expose the current revision snapshot by default; the
        draft working copy is available through the explicit draft view.
        """
        revision = None if draft else (current or self._current_revision(route))
        base = {
            "id": route.id,
            "created_at": route.created_at,
            "updated_at": route.updated_at,
            "version": route.version,
            "is_published": route.is_published,
            "current_revision_no": route.current_revision_no,
            "draft_source_revision_no": route.draft_source_revision_no,
        }
        content = route_snapshot(route) if revision is None else revision_snapshot(revision)
        return TrailRouteResponse(**base, **content)

    def _current_revision(self, route: TrailRoute) -> RouteRevision | None:
        if route.current_revision_no is None:
            return None
        return self.revisions.get(route.id, route.current_revision_no)

    @staticmethod
    def _revision_summary(revision: RouteRevision) -> RouteRevisionSummary:
        return RouteRevisionSummary(
            id=revision.id,
            route_id=revision.route_id,
            revision_no=revision.revision_no,
            published_at=revision.published_at,
            published_by=revision.published_by,
            derived_from_revision_no=revision.derived_from_revision_no,
            change_summary=revision.change_summary,
            name=revision.name,
            region=revision.region,
            distance_km=revision.distance_km,
            elevation_gain_m=revision.elevation_gain_m,
            estimated_duration_minutes=revision.estimated_duration_minutes,
            difficulty=revision.difficulty,
            is_loop=revision.is_loop,
            segment_count=len(revision.segments_json),
            point_count=len(revision.points_json),
            risk_tag_count=len(revision.risk_tags_json),
        )

    @staticmethod
    def _revision_response(revision: RouteRevision) -> RouteRevisionResponse:
        return RouteRevisionResponse(
            id=revision.id,
            route_id=revision.route_id,
            revision_no=revision.revision_no,
            published_at=revision.published_at,
            published_by=revision.published_by,
            derived_from_revision_no=revision.derived_from_revision_no,
            change_summary=revision.change_summary,
            **{field: getattr(revision, field) for field in ROUTE_SCALAR_FIELDS},
            segments=revision.segments_json,
            points=revision.points_json,
            risk_tags=revision.risk_tags_json,
        )

    @staticmethod
    def _validate_existing_geometry(route: TrailRoute) -> None:
        if route.max_altitude_m < route.min_altitude_m:
            raise ValidationError("maximum altitude cannot be below minimum altitude")
        if route.segments:
            segment_distance = sum(item.distance_km for item in route.segments)
            tolerance = max(0.5, route.distance_km * 0.05)
            if abs(segment_distance - route.distance_km) > tolerance:
                raise ValidationError("updated distance no longer matches route segments")
        if any(item.distance_from_start_km > route.distance_km for item in route.points):
            raise ValidationError("updated distance ends before an existing route point")

    def route_readiness(self, route_id: int) -> dict[str, object]:
        route = self.routes.get_detail(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        warnings: list[str] = []
        supply_types = {PointType.WATER, PointType.FOOD, PointType.SHELTER}
        supplies = [point for point in route.points if point.point_type in supply_types]
        exits = [point for point in route.points if point.point_type == PointType.EXIT]
        if not route.segments:
            warnings.append("route has no segments")
        if not route.points:
            warnings.append("route has no key points")
        if route.distance_km >= 15 and not supplies:
            warnings.append("long route has no documented supply point")
        if route.difficulty in {"hard", "extreme"} and not route.risk_tags:
            warnings.append("difficult route has no risk tags")
        if route.distance_km >= 20 and not exits:
            warnings.append("long route has no documented emergency exit")
        return {
            "route_id": route.id,
            "is_publishable": len(warnings) == 0,
            "is_published": route.is_published,
            "current_revision_no": route.current_revision_no,
            "draft_source_revision_no": route.draft_source_revision_no,
            "segment_count": len(route.segments),
            "point_count": len(route.points),
            "supply_point_count": len(supplies),
            "risk_tag_count": len(route.risk_tags),
            "warnings": warnings,
        }
