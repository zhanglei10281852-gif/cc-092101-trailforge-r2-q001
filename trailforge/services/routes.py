from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import AuditAction, PointType
from trailforge.errors import ConflictError, InvalidStateError, NotFoundError, ValidationError
from trailforge.models.revisions import (
    RouteRevision,
    RouteRevisionPoint,
    RouteRevisionRiskTag,
    RouteRevisionSegment,
)
from trailforge.models.routes import RiskTag, RoutePoint, RouteSegment, TrailRoute
from trailforge.repositories.base import apply_version
from trailforge.repositories.revisions import RouteRevisionRepository
from trailforge.repositories.routes import RouteRepository
from trailforge.schemas.common import Page
from trailforge.schemas.revisions import (
    RevisionChildDiff,
    RouteRevisionDiff,
    RouteRevisionPublish,
    RouteRevisionResponse,
    RouteRevisionSummary,
)
from trailforge.schemas.routes import (
    RiskTagCreate,
    RiskTagResponse,
    RouteFilter,
    RoutePointResponse,
    RouteSegmentResponse,
    TrailRouteCreate,
    TrailRouteResponse,
    TrailRouteUpdate,
)
from trailforge.services.base import ServiceBase

ROUTE_SNAPSHOT_FIELDS = (
    "name",
    "region",
    "description",
    "distance_km",
    "elevation_gain_m",
    "elevation_loss_m",
    "min_altitude_m",
    "max_altitude_m",
    "estimated_duration_minutes",
    "difficulty",
    "is_loop",
)

SEGMENT_FIELDS = (
    "sequence",
    "name",
    "description",
    "distance_km",
    "elevation_gain_m",
    "estimated_duration_minutes",
    "difficulty",
    "start_latitude",
    "start_longitude",
    "end_latitude",
    "end_longitude",
)

POINT_FIELDS = (
    "sequence",
    "name",
    "point_type",
    "latitude",
    "longitude",
    "altitude_m",
    "distance_from_start_km",
    "description",
    "supply_details",
)

RISK_TAG_FIELDS = ("code", "name", "level", "description", "mitigation")


class RouteService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.routes = RouteRepository(session)
        self.revisions = RouteRevisionRepository(session)

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
        route_data = data.model_dump(exclude={"segments", "points", "risk_tag_ids"})
        route = TrailRoute(**route_data)
        self.session.add(route)
        route.segments = [RouteSegment(**item.model_dump()) for item in data.segments]
        route.points = [RoutePoint(**item.model_dump()) for item in data.points]
        route.risk_tags = tags
        try:
            with self.session.begin_nested():
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
        revision: RouteRevision | None = None
        if route.is_published:
            # Creating an already-published route atomically publishes its
            # first immutable revision in the same transaction.
            revision = self._create_revision(
                route, actor_id=actor_id, change_summary="Initial publication"
            )
        return self._serialize(route, revision=revision)

    def get_route(self, route_id: int) -> TrailRouteResponse:
        route = self.routes.get_detail(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        return self._serialize(route, revision=self._current_revision(route))

    def get_draft(self, route_id: int) -> TrailRouteResponse:
        """The editable working copy, regardless of publication state."""
        route = self.routes.get_detail(route_id)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        current = self.revisions.latest_for_route(route.id)
        return self._serialize(
            route,
            revision=None,
            current_version=current.version_number if current is not None else None,
        )

    def list_routes(self, filters: RouteFilter) -> Page[TrailRouteResponse]:
        result = self.routes.list_routes(filters)
        latest = self.revisions.latest_for_routes(
            [route.id for route in result.items if route.is_published]
        )
        return Page[TrailRouteResponse].build(
            [
                self._serialize(route, revision=latest.get(route.id))
                for route in result.items
            ],
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
        if not route.draft_open:
            raise InvalidStateError(
                "a published route cannot be edited directly; "
                "derive a new draft from one of its revisions first"
            )
        apply_version(route, data.expected_version)
        before = self.snapshot(route)
        changes = data.model_dump(
            exclude_unset=True,
            exclude={"risk_tag_ids", "expected_version", "segments", "points"},
        )
        for field, value in changes.items():
            setattr(route, field, value)
        if data.risk_tag_ids is not None:
            tags = self.routes.get_risk_tags(data.risk_tag_ids)
            if len(tags) != len(data.risk_tag_ids):
                raise ValidationError("one or more risk tags do not exist")
            route.risk_tags = tags
        if data.segments is not None:
            route.segments.clear()
            self.session.flush()
            route.segments = [RouteSegment(**item.model_dump()) for item in data.segments]
        if data.points is not None:
            route.points.clear()
            self.session.flush()
            route.points = [RoutePoint(**item.model_dump()) for item in data.points]
        self._validate_existing_geometry(route)
        self.session.flush()
        self.audit(
            actor_id=actor_id,
            entity_type="trail_route",
            entity_id=route.id,
            action=AuditAction.UPDATED,
            before=before,
            after=self.snapshot(route),
        )
        current = self.revisions.latest_for_route(route.id)
        return self._serialize(
            route,
            revision=None,
            current_version=current.version_number if current is not None else None,
        )

    def publish_route(
        self, route_id: int, data: RouteRevisionPublish, *, actor_id: int
    ) -> RouteRevisionResponse:
        route = self.routes.get_detail(route_id, for_update=True)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        if not route.draft_open:
            raise InvalidStateError(
                "route has no open draft to publish; "
                "derive a new draft from one of its revisions first"
            )
        self._claim_route_version(route, data.expected_version)
        revision = self._create_revision(
            route, actor_id=actor_id, change_summary=data.change_summary
        )
        return RouteRevisionResponse.model_validate(revision)

    def list_revisions(self, route_id: int) -> list[RouteRevisionSummary]:
        if self.routes.get(route_id) is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        return [
            RouteRevisionSummary.model_validate(item)
            for item in self.revisions.list_for_route(route_id)
        ]

    def get_revision(self, route_id: int, version_number: int) -> RouteRevisionResponse:
        revision = self.revisions.get_by_version(route_id, version_number)
        if revision is None:
            raise NotFoundError(
                f"TrailRoute {route_id} has no revision {version_number}",
                context={"route_id": route_id, "version_number": version_number},
            )
        return RouteRevisionResponse.model_validate(revision)

    def diff_revisions(
        self, route_id: int, from_version: int, to_version: int
    ) -> RouteRevisionDiff:
        if self.routes.get(route_id) is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        if from_version == to_version:
            raise ValidationError("diff requires two different version numbers")
        source = self.revisions.get_by_version(route_id, from_version)
        target = self.revisions.get_by_version(route_id, to_version)
        missing = [
            version
            for version, revision in ((from_version, source), (to_version, target))
            if revision is None
        ]
        if missing:
            raise NotFoundError(
                f"TrailRoute {route_id} is missing revision(s) {missing}",
                context={"route_id": route_id, "missing": missing},
            )
        assert source is not None and target is not None
        changed_fields: dict[str, dict[str, Any]] = {}
        for field in ROUTE_SNAPSHOT_FIELDS:
            old = self._json_value(getattr(source, field))
            new = self._json_value(getattr(target, field))
            if old != new:
                changed_fields[field] = {"from": old, "to": new}
        source_tags = {tag.code for tag in source.risk_tags}
        target_tags = {tag.code for tag in target.risk_tags}
        return RouteRevisionDiff(
            route_id=route_id,
            from_version=from_version,
            to_version=to_version,
            changed_fields=changed_fields,
            segments=self._diff_children(source.segments, target.segments, SEGMENT_FIELDS),
            points=self._diff_children(source.points, target.points, POINT_FIELDS),
            risk_tags_added=sorted(target_tags - source_tags),
            risk_tags_removed=sorted(source_tags - target_tags),
        )

    def derive_draft(
        self, route_id: int, version_number: int, *, actor_id: int
    ) -> TrailRouteResponse:
        """Replace the working copy with an old revision and reopen the draft.

        The revision itself is untouched; publishing the derived draft later
        creates the next version in the route's chain.
        """
        route = self.routes.get_detail(route_id, for_update=True)
        if route is None:
            raise NotFoundError(f"TrailRoute {route_id} was not found")
        revision = self.revisions.get_by_version(route_id, version_number)
        if revision is None:
            raise NotFoundError(
                f"TrailRoute {route_id} has no revision {version_number}",
                context={"route_id": route_id, "version_number": version_number},
            )
        before = self.snapshot(route)
        for field in ROUTE_SNAPSHOT_FIELDS:
            setattr(route, field, getattr(revision, field))
        # Replace the working-copy children. Orphans must be flushed away
        # before the new rows are inserted, otherwise the (route_id, sequence)
        # unique constraints would see both generations at once.
        route.segments.clear()
        route.points.clear()
        self.session.flush()
        route.segments = [
            RouteSegment(**{field: getattr(item, field) for field in SEGMENT_FIELDS})
            for item in revision.segments
        ]
        route.points = [
            RoutePoint(**{field: getattr(item, field) for field in POINT_FIELDS})
            for item in revision.points
        ]
        tag_ids = [
            item.risk_tag_id for item in revision.risk_tags if item.risk_tag_id is not None
        ]
        route.risk_tags = self.routes.get_risk_tags(tag_ids)
        route.draft_open = True
        apply_version(route, None)
        self.session.flush()
        self.audit(
            actor_id=actor_id,
            entity_type="trail_route",
            entity_id=route.id,
            action=AuditAction.UPDATED,
            before=before,
            after=self.snapshot(route),
            context={"derived_from_version": version_number},
        )
        current = self.revisions.latest_for_route(route.id)
        return self._serialize(
            route,
            revision=None,
            current_version=current.version_number if current is not None else None,
        )

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
            "segment_count": len(route.segments),
            "point_count": len(route.points),
            "supply_point_count": len(supplies),
            "risk_tag_count": len(route.risk_tags),
            "warnings": warnings,
        }

    def _claim_route_version(self, route: TrailRoute, expected_version: int | None) -> None:
        """Atomically bump the route row's version as the publish guard.

        A plain read-check-write is not enough here: SQLite runs the reads and
        the write in different snapshots, so a concurrent publisher could slip
        its commit in between. The conditional UPDATE evaluates its WHERE
        clause against the newest committed state while holding the write
        lock, so exactly one racing publish can claim a given row version;
        the loser gets an understandable 409 conflict.
        """
        expected = expected_version if expected_version is not None else route.version
        if route.version != expected:
            raise ConflictError(
                "route was modified or published by another request; "
                "reload it and publish again",
                context={
                    "route_id": route.id,
                    "expected_version": expected,
                    "current_version": route.version,
                },
            )
        result = self.session.execute(
            update(TrailRoute)
            .where(TrailRoute.id == route.id, TrailRoute.version == expected)
            .values(version=expected + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            # Our transaction's snapshot is current as of the failed UPDATE,
            # so this re-read shows the version the winning request wrote.
            current = self.session.scalar(
                select(TrailRoute.version).where(TrailRoute.id == route.id)
            )
            raise ConflictError(
                "route was modified or published by another request; "
                "reload it and publish again",
                context={
                    "route_id": route.id,
                    "expected_version": expected,
                    "current_version": current,
                },
            )
        route.version = expected + 1

    def _create_revision(
        self, route: TrailRoute, *, actor_id: int, change_summary: str
    ) -> RouteRevision:
        """Snapshot the working copy as the next immutable revision.

        The unique (route_id, version_number) constraint is the database-level
        guard that makes concurrent publishes safe: only one transaction can
        create a given version. Everything (revision, route state, audit) is
        flushed inside the caller's transaction and commits atomically with it.
        """
        version_number = self.revisions.next_version_number(route.id)
        revision = RouteRevision(
            route_id=route.id,
            version_number=version_number,
            change_summary=change_summary,
            published_by=actor_id,
            published_at=utc_now(),
            **{field: getattr(route, field) for field in ROUTE_SNAPSHOT_FIELDS},
        )
        revision.segments = [
            RouteRevisionSegment(
                **{field: getattr(item, field) for field in SEGMENT_FIELDS},
            )
            for item in route.segments
        ]
        revision.points = [
            RouteRevisionPoint(**{field: getattr(item, field) for field in POINT_FIELDS})
            for item in route.points
        ]
        revision.risk_tags = [
            RouteRevisionRiskTag(
                risk_tag_id=tag.id,
                **{field: getattr(tag, field) for field in RISK_TAG_FIELDS},
            )
            for tag in route.risk_tags
        ]
        try:
            with self.session.begin_nested():
                self.session.add(revision)
                route.is_published = True
                route.draft_open = False
                self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "another request published this route version concurrently; "
                "reload the route and publish again",
                context={"route_id": route.id, "version_number": version_number},
            ) from exc
        self.audit(
            actor_id=actor_id,
            entity_type="route_revision",
            entity_id=revision.id,
            action=AuditAction.PUBLISHED,
            after=self.snapshot(revision),
            context={"route_id": route.id, "version_number": version_number},
        )
        return revision

    def _current_revision(self, route: TrailRoute) -> RouteRevision | None:
        if not route.is_published:
            return None
        return self.revisions.latest_for_route(route.id)

    def _serialize(
        self,
        route: TrailRoute,
        *,
        revision: RouteRevision | None,
        current_version: int | None = None,
    ) -> TrailRouteResponse:
        """Build the public route view.

        Published routes are serialized from their current (latest) revision so
        existing queries always return the currently published version; drafts
        are serialized from the editable working copy.
        """
        if revision is not None:
            return TrailRouteResponse(
                id=route.id,
                created_at=route.created_at,
                updated_at=route.updated_at,
                version=route.version,
                is_published=route.is_published,
                draft_open=route.draft_open,
                current_version_number=revision.version_number,
                segments=[
                    RouteSegmentResponse(
                        id=item.id,
                        route_id=route.id,
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                        **{field: getattr(item, field) for field in SEGMENT_FIELDS},
                    )
                    for item in revision.segments
                ],
                points=[
                    RoutePointResponse(
                        id=item.id,
                        route_id=route.id,
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                        **{field: getattr(item, field) for field in POINT_FIELDS},
                    )
                    for item in revision.points
                ],
                risk_tags=[
                    RiskTagResponse(
                        id=item.risk_tag_id if item.risk_tag_id is not None else item.id,
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                        **{field: getattr(item, field) for field in RISK_TAG_FIELDS},
                    )
                    for item in revision.risk_tags
                ],
                **{field: getattr(revision, field) for field in ROUTE_SNAPSHOT_FIELDS},
            )
        return TrailRouteResponse(
            id=route.id,
            created_at=route.created_at,
            updated_at=route.updated_at,
            version=route.version,
            is_published=route.is_published,
            draft_open=route.draft_open,
            current_version_number=current_version,
            segments=[RouteSegmentResponse.model_validate(item) for item in route.segments],
            points=[RoutePointResponse.model_validate(item) for item in route.points],
            risk_tags=[RiskTagResponse.model_validate(item) for item in route.risk_tags],
            **{field: getattr(route, field) for field in ROUTE_SNAPSHOT_FIELDS},
        )

    @staticmethod
    def _diff_children(
        source_items: list[Any], target_items: list[Any], fields: tuple[str, ...]
    ) -> RevisionChildDiff:
        source_by_sequence = {item.sequence: item for item in source_items}
        target_by_sequence = {item.sequence: item for item in target_items}
        diff = RevisionChildDiff()
        for sequence in sorted(source_by_sequence.keys() - target_by_sequence.keys()):
            item = source_by_sequence[sequence]
            diff.removed.append(
                {"sequence": sequence, "name": item.name, "fields": _fields_of(item, fields)}
            )
        for sequence in sorted(target_by_sequence.keys() - source_by_sequence.keys()):
            item = target_by_sequence[sequence]
            diff.added.append(
                {"sequence": sequence, "name": item.name, "fields": _fields_of(item, fields)}
            )
        for sequence in sorted(source_by_sequence.keys() & target_by_sequence.keys()):
            old = source_by_sequence[sequence]
            new = target_by_sequence[sequence]
            changes: dict[str, dict[str, Any]] = {}
            for field in fields:
                old_value = ServiceBase._json_value(getattr(old, field))
                new_value = ServiceBase._json_value(getattr(new, field))
                if old_value != new_value:
                    changes[field] = {"from": old_value, "to": new_value}
            if changes:
                diff.changed.append(
                    {"sequence": sequence, "name": new.name, "changes": changes}
                )
        return diff

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


def _fields_of(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: ServiceBase._json_value(getattr(item, field)) for field in fields}
