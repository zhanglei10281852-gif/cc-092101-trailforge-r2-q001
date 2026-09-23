from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import create_expedition, create_route, create_user
from trailforge.config import Settings
from trailforge.database.migrations import (
    assert_database_integrity,
    initialize_database,
    migration_status,
)
from trailforge.database.session import Database
from trailforge.errors import ConflictError, InvalidStateError, NotFoundError, ValidationError
from trailforge.models.activities import Expedition
from trailforge.models.audit import AuditLog
from trailforge.models.revisions import RouteRevision
from trailforge.models.routes import TrailRoute
from trailforge.schemas.activities import ActivityStateChange, ExpeditionCreate
from trailforge.schemas.revisions import RouteRevisionPublish
from trailforge.schemas.routes import (
    RiskTagCreate,
    RouteFilter,
    RoutePointCreate,
    RouteSegmentCreate,
    TrailRouteCreate,
    TrailRouteUpdate,
)
from trailforge.services.activities import ExpeditionService
from trailforge.services.routes import RouteService
from trailforge.services.statistics import StatisticsService

UTC = UTC


def _create_rich_route(
    session,
    *,
    actor_id: int,
    name: str = "Rich Ridge",
    published: bool = True,
    tag_code: str = "rockfall",
) -> tuple[int, int]:
    """A published route with segments, a supply point and a risk tag."""
    service = RouteService(session)
    tag = service.create_risk_tag(
        RiskTagCreate(code=tag_code, name="Rockfall", level="high"),
        actor_id=actor_id,
    )
    route = service.create_route(
        TrailRouteCreate(
            name=name,
            region="Test Mountains",
            description="original description",
            distance_km=12,
            elevation_gain_m=600,
            elevation_loss_m=600,
            min_altitude_m=200,
            max_altitude_m=800,
            estimated_duration_minutes=300,
            difficulty="hard",
            is_loop=True,
            is_published=published,
            segments=[
                RouteSegmentCreate(
                    sequence=1,
                    name="Ascent",
                    distance_km=7,
                    elevation_gain_m=500,
                    estimated_duration_minutes=180,
                    difficulty="hard",
                    start_latitude=30,
                    start_longitude=120,
                    end_latitude=30.05,
                    end_longitude=120.05,
                ),
                RouteSegmentCreate(
                    sequence=2,
                    name="Descent",
                    distance_km=5,
                    elevation_gain_m=100,
                    estimated_duration_minutes=120,
                    difficulty="moderate",
                    start_latitude=30.05,
                    start_longitude=120.05,
                    end_latitude=30,
                    end_longitude=120,
                ),
            ],
            points=[
                RoutePointCreate(
                    sequence=1,
                    name="Spring",
                    point_type="water",
                    latitude=30.02,
                    longitude=120.02,
                    distance_from_start_km=4,
                    supply_details="seasonal spring, unreliable in autumn",
                ),
                RoutePointCreate(
                    sequence=2,
                    name="Summit",
                    point_type="summit",
                    latitude=30.05,
                    longitude=120.05,
                    altitude_m=800,
                    distance_from_start_km=7,
                ),
            ],
            risk_tag_ids=[tag.id],
        ),
        actor_id=actor_id,
    )
    return route.id, tag.id


def test_draft_route_remains_editable(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=False)
    service = RouteService(session)
    updated = service.update_route(
        route_id,
        TrailRouteUpdate(description="still drafting", elevation_gain_m=650),
        actor_id=user_id,
    )
    assert updated.description == "still drafting"
    assert updated.draft_open is True
    assert updated.is_published is False
    assert updated.current_version_number is None


def test_publish_creates_revision_with_full_snapshot(session) -> None:
    user_id = create_user(session)
    route_id, tag_id = _create_rich_route(session, actor_id=user_id, published=False)
    service = RouteService(session)
    revision = service.publish_route(
        route_id, RouteRevisionPublish(change_summary="first release"), actor_id=user_id
    )
    assert revision.version_number == 1
    assert revision.change_summary == "first release"
    assert revision.published_by == user_id
    assert revision.distance_km == 12
    assert [item.name for item in revision.segments] == ["Ascent", "Descent"]
    assert [item.name for item in revision.points] == ["Spring", "Summit"]
    supply = revision.points[0]
    assert supply.point_type == "water"
    assert supply.supply_details == "seasonal spring, unreliable in autumn"
    assert len(revision.risk_tags) == 1
    assert revision.risk_tags[0].code == "rockfall"
    assert revision.risk_tags[0].risk_tag_id == tag_id
    route = session.get(TrailRoute, route_id)
    assert route.is_published is True
    assert route.draft_open is False
    audit = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "route_revision",
            AuditLog.action == "published",
        )
    )
    assert audit is not None
    assert audit.context["version_number"] == 1


def test_create_with_is_published_atomically_creates_first_revision(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    revisions = service.list_revisions(route_id)
    assert [item.version_number for item in revisions] == [1]
    route = service.get_route(route_id)
    assert route.current_version_number == 1
    assert route.draft_open is False


def test_published_route_cannot_be_edited_directly(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    with pytest.raises(InvalidStateError, match="derive a new draft"):
        service.update_route(
            route_id, TrailRouteUpdate(description="sneaky edit"), actor_id=user_id
        )


def test_sequential_publishes_get_consecutive_version_numbers(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    for expected in (2, 3):
        service.derive_draft(route_id, expected - 1, actor_id=user_id)
        revision = service.publish_route(route_id, RouteRevisionPublish(), actor_id=user_id)
        assert revision.version_number == expected
    assert [item.version_number for item in service.list_revisions(route_id)] == [1, 2, 3]


def test_publish_without_open_draft_is_rejected(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    with pytest.raises(InvalidStateError, match="no open draft"):
        service.publish_route(route_id, RouteRevisionPublish(), actor_id=user_id)


def test_route_queries_return_current_published_version(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    service.derive_draft(route_id, 1, actor_id=user_id)
    service.update_route(
        route_id,
        TrailRouteUpdate(description="second edition", elevation_gain_m=700),
        actor_id=user_id,
    )
    service.publish_route(route_id, RouteRevisionPublish(), actor_id=user_id)
    detail = service.get_route(route_id)
    assert detail.current_version_number == 2
    assert detail.description == "second edition"
    assert detail.elevation_gain_m == 700
    listed = service.list_routes(RouteFilter(region="Test Mountains"))
    assert listed.meta.total == 1
    assert listed.items[0].current_version_number == 2
    assert listed.items[0].description == "second edition"
    # The in-progress draft is visible separately once a new draft is opened.
    service.derive_draft(route_id, 1, actor_id=user_id)
    service.update_route(
        route_id, TrailRouteUpdate(description="unpublished work"), actor_id=user_id
    )
    assert service.get_route(route_id).description == "second edition"
    assert service.get_draft(route_id).description == "unpublished work"


def test_revision_list_detail_and_missing_version(session) -> None:
    user_id = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    service.derive_draft(route_id, 1, actor_id=user_id)
    service.update_route(
        route_id, TrailRouteUpdate(description="second edition"), actor_id=user_id
    )
    service.publish_route(
        route_id, RouteRevisionPublish(change_summary="wording"), actor_id=user_id
    )
    summaries = service.list_revisions(route_id)
    assert [item.version_number for item in summaries] == [1, 2]
    assert summaries[1].change_summary == "wording"
    first = service.get_revision(route_id, 1)
    assert first.description == "original description"
    assert len(first.segments) == 2
    with pytest.raises(NotFoundError):
        service.get_revision(route_id, 99)
    with pytest.raises(NotFoundError):
        service.list_revisions(9999)


def test_revision_diff_reports_field_segment_point_and_tag_changes(session) -> None:
    user_id = create_user(session)
    route_id, tag_id = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    other_tag = service.create_risk_tag(
        RiskTagCreate(code="river-crossing", name="River crossing", level="moderate"),
        actor_id=user_id,
    )
    service.derive_draft(route_id, 1, actor_id=user_id)
    service.update_route(
        route_id,
        TrailRouteUpdate(
            description="rerouted descent",
            distance_km=14,
            segments=[
                RouteSegmentCreate(
                    sequence=1,
                    name="Ascent",
                    distance_km=9,
                    elevation_gain_m=500,
                    estimated_duration_minutes=180,
                    difficulty="hard",
                    start_latitude=30,
                    start_longitude=120,
                    end_latitude=30.05,
                    end_longitude=120.05,
                ),
                RouteSegmentCreate(
                    sequence=2,
                    name="Descent",
                    distance_km=5,
                    elevation_gain_m=100,
                    estimated_duration_minutes=120,
                    difficulty="moderate",
                    start_latitude=30.05,
                    start_longitude=120.05,
                    end_latitude=30,
                    end_longitude=120,
                ),
            ],
            points=[
                RoutePointCreate(
                    sequence=1,
                    name="Spring",
                    point_type="water",
                    latitude=30.02,
                    longitude=120.02,
                    distance_from_start_km=4,
                    supply_details="seasonal spring, unreliable in autumn",
                ),
                RoutePointCreate(
                    sequence=2,
                    name="Summit",
                    point_type="summit",
                    latitude=30.05,
                    longitude=120.05,
                    altitude_m=800,
                    distance_from_start_km=7,
                ),
                RoutePointCreate(
                    sequence=3,
                    name="Emergency exit",
                    point_type="exit",
                    latitude=30.03,
                    longitude=120.04,
                    distance_from_start_km=10,
                ),
            ],
            risk_tag_ids=[other_tag.id],
        ),
        actor_id=user_id,
    )
    service.publish_route(route_id, RouteRevisionPublish(), actor_id=user_id)
    diff = service.diff_revisions(route_id, 1, 2)
    assert diff.changed_fields["description"] == {
        "from": "original description",
        "to": "rerouted descent",
    }
    assert diff.changed_fields["distance_km"] == {"from": 12, "to": 14}
    assert [item["sequence"] for item in diff.segments.changed] == [1]
    assert diff.segments.changed[0]["changes"]["distance_km"] == {"from": 7, "to": 9}
    assert diff.segments.added == []
    assert diff.segments.removed == []
    assert [item["sequence"] for item in diff.points.added] == [3]
    assert diff.risk_tags_added == ["river-crossing"]
    assert diff.risk_tags_removed == ["rockfall"]
    with pytest.raises(ValidationError):
        service.diff_revisions(route_id, 1, 1)
    with pytest.raises(NotFoundError):
        service.diff_revisions(route_id, 1, 5)


def test_derive_draft_from_old_revision_restores_snapshot(session) -> None:
    user_id = create_user(session)
    route_id, tag_id = _create_rich_route(session, actor_id=user_id, published=True)
    service = RouteService(session)
    service.derive_draft(route_id, 1, actor_id=user_id)
    service.update_route(
        route_id,
        TrailRouteUpdate(description="second edition", elevation_gain_m=700),
        actor_id=user_id,
    )
    service.publish_route(route_id, RouteRevisionPublish(), actor_id=user_id)
    # Derive a new draft from the first revision: the working copy goes back
    # to the v1 snapshot while v2 stays untouched.
    draft = service.derive_draft(route_id, 1, actor_id=user_id)
    assert draft.draft_open is True
    assert draft.description == "original description"
    assert draft.elevation_gain_m == 600
    assert draft.current_version_number == 2
    assert [item.name for item in draft.segments] == ["Ascent", "Descent"]
    assert [tag.id for tag in draft.risk_tags] == [tag_id]
    republished = service.publish_route(route_id, RouteRevisionPublish(), actor_id=user_id)
    assert republished.version_number == 3
    assert republished.description == "original description"
    assert service.get_revision(route_id, 2).description == "second edition"


def test_revision_rows_cannot_be_updated_or_deleted(session) -> None:
    user_id = create_user(session)
    _create_rich_route(session, actor_id=user_id, published=True)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text("UPDATE route_revisions SET distance_km = 99"))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text("DELETE FROM route_revisions"))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text("UPDATE route_revision_segments SET distance_km = 99"))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text("DELETE FROM route_revision_points"))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text("DELETE FROM route_revision_risk_tags"))
    # The savepoint rollbacks discarded only the blocked statements; the
    # revision itself is still there.
    assert session.scalar(select(func.count()).select_from(RouteRevision)) == 1


def test_expedition_pins_current_revision_at_creation(session) -> None:
    organizer = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route_id)
    expedition = ExpeditionService(session).get(expedition_id)
    revision = RouteService(session).get_revision(route_id, 1)
    assert expedition.route_revision_id == revision.id
    assert expedition.route_version_number == 1


def test_expedition_can_pin_an_explicit_revision(session) -> None:
    organizer = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=organizer)
    service = RouteService(session)
    service.derive_draft(route_id, 1, actor_id=organizer)
    service.update_route(
        route_id, TrailRouteUpdate(description="second edition"), actor_id=organizer
    )
    service.publish_route(route_id, RouteRevisionPublish(), actor_id=organizer)
    first = service.get_revision(route_id, 1)
    start = datetime.now(UTC) + timedelta(days=10)
    expedition = ExpeditionService(session).create(
        ExpeditionCreate(
            organizer_id=organizer,
            route_id=route_id,
            route_revision_id=first.id,
            name="Pinned Expedition",
            meeting_location="North trailhead",
            meeting_at=start - timedelta(hours=1),
            start_at=start,
            end_at=start + timedelta(hours=8),
            registration_deadline=start - timedelta(days=1),
            capacity=4,
        )
    )
    assert expedition.route_revision_id == first.id
    assert expedition.route_version_number == 1


def test_expedition_rejects_revision_from_another_route(session) -> None:
    organizer = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=organizer)
    other_route_id, _ = _create_rich_route(
        session, actor_id=organizer, name="Other Ridge", tag_code="scree"
    )
    foreign_revision = RouteService(session).get_revision(other_route_id, 1)
    start = datetime.now(UTC) + timedelta(days=10)
    with pytest.raises(ValidationError, match="does not reference"):
        ExpeditionService(session).create(
            ExpeditionCreate(
                organizer_id=organizer,
                route_id=route_id,
                route_revision_id=foreign_revision.id,
                name="Mismatched Expedition",
                meeting_location="North trailhead",
                meeting_at=start - timedelta(hours=1),
                start_at=start,
                end_at=start + timedelta(hours=8),
                registration_deadline=start - timedelta(days=1),
                capacity=4,
            )
        )


def test_new_version_does_not_drift_existing_expedition(session) -> None:
    organizer = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route_id)
    service = RouteService(session)
    service.derive_draft(route_id, 1, actor_id=organizer)
    service.update_route(
        route_id,
        TrailRouteUpdate(
            description="rerouted after incidents",
            distance_km=14,
            elevation_gain_m=900,
            segments=[
                RouteSegmentCreate(
                    sequence=1,
                    name="Ascent",
                    distance_km=14,
                    elevation_gain_m=900,
                    estimated_duration_minutes=360,
                    difficulty="extreme",
                    start_latitude=30,
                    start_longitude=120,
                    end_latitude=30.05,
                    end_longitude=120.05,
                )
            ],
        ),
        actor_id=organizer,
    )
    service.publish_route(route_id, RouteRevisionPublish(), actor_id=organizer)
    # The route now publishes v2, but the expedition still reads v1.
    expedition = ExpeditionService(session).get(expedition_id)
    assert expedition.route_version_number == 1
    pinned = service.get_revision(route_id, expedition.route_version_number)
    assert pinned.distance_km == 12
    assert pinned.elevation_gain_m == 600
    assert pinned.description == "original description"
    current = service.get_route(route_id)
    assert current.current_version_number == 2
    assert current.distance_km == 14


def test_completed_expedition_statistics_use_pinned_revision(session) -> None:
    organizer = create_user(session)
    route_id, _ = _create_rich_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route_id)
    expedition_service = ExpeditionService(session)
    for target in ("open", "assembling", "departed", "in_progress", "completed"):
        expedition_service.change_status(
            expedition_id,
            ActivityStateChange(target_status=target, actor_id=organizer),
        )
    route_service = RouteService(session)
    route_service.derive_draft(route_id, 1, actor_id=organizer)
    route_service.update_route(
        route_id,
        TrailRouteUpdate(
            distance_km=20,
            elevation_gain_m=1200,
            segments=[
                RouteSegmentCreate(
                    sequence=1,
                    name="Ascent",
                    distance_km=20,
                    elevation_gain_m=1200,
                    estimated_duration_minutes=600,
                    difficulty="extreme",
                    start_latitude=30,
                    start_longitude=120,
                    end_latitude=30.05,
                    end_longitude=120.05,
                )
            ],
        ),
        actor_id=organizer,
    )
    route_service.publish_route(route_id, RouteRevisionPublish(), actor_id=organizer)
    dashboard = StatisticsService(session).dashboard()
    assert dashboard.completed_expeditions == 1
    assert dashboard.total_hiking_distance_km == 12
    assert dashboard.total_elevation_gain_m == 600


def test_failed_publish_rolls_back_revision_route_state_and_audit(database) -> None:
    with database.session() as session:
        user_id = create_user(session)
        route_id, _ = _create_rich_route(session, actor_id=user_id, published=False)
    with pytest.raises(RuntimeError, match="downstream failure"), database.session() as session:
        RouteService(session).publish_route(
            route_id, RouteRevisionPublish(), actor_id=user_id
        )
        raise RuntimeError("downstream failure")
    with database.session() as session:
        route = session.get(TrailRoute, route_id)
        assert route.is_published is False
        assert route.draft_open is True
        assert session.scalar(select(func.count()).select_from(RouteRevision)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_type == "route_revision")
            )
            == 0
        )


def test_failed_expedition_create_rolls_back_binding_and_audit(database) -> None:
    with database.session() as session:
        organizer = create_user(session)
        route_id, _ = _create_rich_route(session, actor_id=organizer)
        start = datetime.now(UTC) + timedelta(days=10)
        payload = ExpeditionCreate(
            organizer_id=organizer,
            route_id=route_id,
            name="Doomed Expedition",
            meeting_location="North trailhead",
            meeting_at=start - timedelta(hours=1),
            start_at=start,
            end_at=start + timedelta(hours=8),
            registration_deadline=start - timedelta(days=1),
            capacity=4,
        )
    with pytest.raises(RuntimeError, match="downstream failure"), database.session() as session:
        ExpeditionService(session).create(payload)
        raise RuntimeError("downstream failure")
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Expedition)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_type == "expedition")
            )
            == 0
        )
        # The published revision itself is untouched by the rollback.
        assert session.scalar(select(func.count()).select_from(RouteRevision)) == 1


def test_concurrent_publish_allows_only_one_next_version(database) -> None:
    with database.session() as session:
        user_id = create_user(session)
        route_id = create_route(session, actor_id=user_id, published=True)
        RouteService(session).derive_draft(route_id, 1, actor_id=user_id)
        route_version = session.get(TrailRoute, route_id).version

    publish = RouteRevisionPublish(
        expected_version=route_version, change_summary="racing publish"
    )

    def attempt(_: int) -> tuple[str, object]:
        try:
            revision = database.run_write(
                lambda session: RouteService(session).publish_route(
                    route_id, publish, actor_id=user_id
                )
            )
        except ConflictError as exc:
            return ("conflict", exc)
        return ("published", revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    outcomes = sorted(kind for kind, _ in results)
    assert outcomes == ["conflict", "published"]
    winner = next(revision for kind, revision in results if kind == "published")
    assert winner.version_number == 2
    conflict = next(exc for kind, exc in results if kind == "conflict")
    assert conflict.context["expected_version"] == route_version
    with database.session() as session:
        versions = list(
            session.scalars(
                select(RouteRevision.version_number)
                .where(RouteRevision.route_id == route_id)
                .order_by(RouteRevision.version_number)
            )
        )
        assert versions == [1, 2]
        route = session.get(TrailRoute, route_id)
        assert route.draft_open is False


def test_revision_relations_survive_restart(settings: Settings) -> None:
    first = Database(settings)
    initialize_database(first)
    with first.session() as session:
        organizer = create_user(session)
        route_id, _ = _create_rich_route(session, actor_id=organizer)
        service = RouteService(session)
        service.derive_draft(route_id, 1, actor_id=organizer)
        service.update_route(
            route_id, TrailRouteUpdate(description="second edition"), actor_id=organizer
        )
        service.publish_route(route_id, RouteRevisionPublish(), actor_id=organizer)
        expedition_id = create_expedition(session, organizer_id=organizer, route_id=route_id)
        pinned_revision_id = session.get(Expedition, expedition_id).route_revision_id
    first.engine.dispose()

    second = Database(settings)
    assert initialize_database(second) == []
    with second.session() as session:
        expedition = session.get(Expedition, expedition_id)
        assert expedition.route_revision_id == pinned_revision_id
        assert expedition.route_version_number == 2
        service = RouteService(session)
        assert [item.version_number for item in service.list_revisions(route_id)] == [1, 2]
        current = service.get_route(route_id)
        assert current.current_version_number == 2
        assert current.description == "second edition"
        assert service.get_revision(route_id, 1).description == "original description"
    second.engine.dispose()


LEGACY_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    email VARCHAR(254) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    phone VARCHAR(40),
    birth_date DATE,
    locale VARCHAR(16) NOT NULL DEFAULT 'zh-CN',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    is_active BOOLEAN NOT NULL DEFAULT 1
);
CREATE TABLE risk_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(120) NOT NULL,
    level VARCHAR(24) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    mitigation TEXT NOT NULL DEFAULT ''
);
CREATE TABLE trail_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    name VARCHAR(180) NOT NULL,
    region VARCHAR(120) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    distance_km FLOAT NOT NULL,
    elevation_gain_m INTEGER NOT NULL,
    elevation_loss_m INTEGER NOT NULL DEFAULT 0,
    min_altitude_m INTEGER NOT NULL DEFAULT 0,
    max_altitude_m INTEGER NOT NULL DEFAULT 0,
    estimated_duration_minutes INTEGER NOT NULL,
    difficulty VARCHAR(24) NOT NULL,
    is_loop BOOLEAN NOT NULL DEFAULT 0,
    is_published BOOLEAN NOT NULL DEFAULT 0
);
CREATE TABLE route_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    name VARCHAR(160) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    distance_km FLOAT NOT NULL,
    elevation_gain_m INTEGER NOT NULL DEFAULT 0,
    estimated_duration_minutes INTEGER NOT NULL,
    difficulty VARCHAR(24) NOT NULL,
    start_latitude FLOAT NOT NULL,
    start_longitude FLOAT NOT NULL,
    end_latitude FLOAT NOT NULL,
    end_longitude FLOAT NOT NULL
);
CREATE TABLE route_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    name VARCHAR(160) NOT NULL,
    point_type VARCHAR(24) NOT NULL,
    latitude FLOAT NOT NULL,
    longitude FLOAT NOT NULL,
    altitude_m INTEGER,
    distance_from_start_km FLOAT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    supply_details TEXT NOT NULL DEFAULT ''
);
CREATE TABLE route_risk_tags (
    route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE CASCADE,
    risk_tag_id INTEGER NOT NULL REFERENCES risk_tags(id) ON DELETE CASCADE,
    PRIMARY KEY (route_id, risk_tag_id)
);
CREATE TABLE expeditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    organizer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE RESTRICT,
    name VARCHAR(180) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    meeting_location VARCHAR(240) NOT NULL,
    meeting_at VARCHAR(32) NOT NULL,
    start_at VARCHAR(32) NOT NULL,
    end_at VARCHAR(32) NOT NULL,
    registration_deadline VARCHAR(32) NOT NULL,
    capacity INTEGER NOT NULL,
    minimum_fitness_level INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(24) NOT NULL DEFAULT 'draft',
    risk_level VARCHAR(24) NOT NULL DEFAULT 'moderate',
    cancellation_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE schema_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version VARCHAR(60) NOT NULL UNIQUE,
    description TEXT NOT NULL,
    applied_at VARCHAR(32) NOT NULL
);
"""

LEGACY_DATA = """
INSERT INTO users (id, created_at, updated_at, email, display_name, is_active)
VALUES (1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
        'legacy@example.com', 'Legacy Maintainer', 1);
INSERT INTO risk_tags (id, created_at, updated_at, code, name, level, description, mitigation)
VALUES (1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
        'rockfall', 'Rockfall', 'high', 'loose rock', 'helmets on');
INSERT INTO trail_routes (
    id, created_at, updated_at, version, name, region, description,
    distance_km, elevation_gain_m, elevation_loss_m, min_altitude_m, max_altitude_m,
    estimated_duration_minutes, difficulty, is_loop, is_published
) VALUES (
    1, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 3,
    'Legacy Ridge', 'Legacy Mountains', 'published before revisions existed',
    11.5, 550, 550, 250, 750, 280, 'moderate', 1, 1
);
INSERT INTO route_segments (
    id, created_at, updated_at, route_id, sequence, name, description,
    distance_km, elevation_gain_m, estimated_duration_minutes, difficulty,
    start_latitude, start_longitude, end_latitude, end_longitude
) VALUES (
    1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1, 1, 'Legacy loop', '',
    11.5, 550, 280, 'moderate', 30.0, 120.0, 30.0, 120.0
);
INSERT INTO route_points (
    id, created_at, updated_at, route_id, sequence, name, point_type,
    latitude, longitude, altitude_m, distance_from_start_km, description, supply_details
) VALUES (
    1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1, 1, 'Legacy spring', 'water',
    30.01, 120.01, 400, 5.0, '', 'reliable year-round'
);
INSERT INTO route_risk_tags (route_id, risk_tag_id) VALUES (1, 1);
INSERT INTO expeditions (
    id, created_at, updated_at, version, organizer_id, route_id, name, description,
    meeting_location, meeting_at, start_at, end_at, registration_deadline,
    capacity, minimum_fitness_level, status, risk_level, cancellation_reason
) VALUES (
    1, '2026-01-03T00:00:00Z', '2026-01-03T00:00:00Z', 1, 1, 1, 'Legacy Expedition', '',
    'South gate', '2026-02-01T01:00:00Z', '2026-02-01T02:00:00Z', '2026-02-01T10:00:00Z',
    '2026-01-31T12:00:00Z', 8, 1, 'open', 'moderate', ''
);
INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('0001', 'Initial TrailForge schema', '2026-01-01T00:00:00Z');
"""


def _legacy_database(settings: Settings) -> Database:
    database = Database(settings)
    with database.engine.connect() as connection:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            if statement.strip():
                connection.exec_driver_sql(statement.strip())
        for statement in LEGACY_DATA.strip().split(";"):
            if statement.strip():
                connection.exec_driver_sql(statement.strip())
        connection.commit()
    return database


def test_legacy_upgrade_creates_first_revision_and_pins_expeditions(
    settings: Settings,
) -> None:
    database = _legacy_database(settings)
    applied = initialize_database(database)
    assert applied == ["0002"]
    assert migration_status(database) == {
        "initialized": True,
        "applied": ["0001", "0002"],
        "pending": [],
    }
    with database.session() as session:
        route = session.get(TrailRoute, 1)
        assert route.is_published is True
        assert route.draft_open is False
        revision = session.scalar(select(RouteRevision).where(RouteRevision.route_id == 1))
        assert revision.version_number == 1
        assert revision.distance_km == 11.5
        assert revision.description == "published before revisions existed"
        assert [item.name for item in revision.segments] == ["Legacy loop"]
        assert [item.name for item in revision.points] == ["Legacy spring"]
        assert revision.points[0].supply_details == "reliable year-round"
        assert [item.code for item in revision.risk_tags] == ["rockfall"]
        expedition = session.get(Expedition, 1)
        assert expedition.route_revision_id == revision.id
        assert expedition.route_version_number == 1
        # The migrated route now follows the new rules end to end.
        service = RouteService(session)
        with pytest.raises(InvalidStateError):
            service.update_route(
                1, TrailRouteUpdate(description="direct edit"), actor_id=1
            )
        service.derive_draft(1, 1, actor_id=1)
        service.update_route(1, TrailRouteUpdate(description="post-upgrade"), actor_id=1)
        v2 = service.publish_route(1, RouteRevisionPublish(), actor_id=1)
        assert v2.version_number == 2
    integrity = assert_database_integrity(database)
    assert integrity["healthy"] is True
    database.engine.dispose()

    # A restart keeps the migrated version relations intact.
    restarted = Database(settings)
    assert initialize_database(restarted) == []
    with restarted.session() as session:
        expedition = session.get(Expedition, 1)
        assert expedition.route_version_number == 1
        service = RouteService(session)
        assert [item.version_number for item in service.list_revisions(1)] == [1, 2]
        assert service.get_revision(1, 1).distance_km == 11.5
    restarted.engine.dispose()


def _api_route_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "API Ridge",
        "region": "API Mountains",
        "description": "first edition",
        "distance_km": 10,
        "elevation_gain_m": 500,
        "elevation_loss_m": 500,
        "min_altitude_m": 100,
        "max_altitude_m": 600,
        "estimated_duration_minutes": 240,
        "difficulty": "moderate",
        "is_loop": True,
        "is_published": False,
        "segments": [
            {
                "sequence": 1,
                "name": "Loop",
                "distance_km": 10,
                "elevation_gain_m": 500,
                "estimated_duration_minutes": 240,
                "difficulty": "moderate",
                "start_latitude": 30,
                "start_longitude": 120,
                "end_latitude": 30.1,
                "end_longitude": 120.1,
            }
        ],
        "points": [],
        "risk_tag_ids": [],
    }
    payload.update(overrides)
    return payload


def test_api_full_revision_workflow(client) -> None:
    user = client.post(
        "/api/v1/users", json={"email": "api@example.com", "display_name": "API User"}
    ).json()
    actor = {"actor_id": user["id"]}
    created = client.post("/api/v1/routes", params=actor, json=_api_route_payload())
    assert created.status_code == 201, created.text
    route = created.json()
    assert route["draft_open"] is True
    assert route["current_version_number"] is None
    route_id = route["id"]

    published = client.post(
        f"/api/v1/routes/{route_id}/publish",
        params=actor,
        json={"change_summary": "first release"},
    )
    assert published.status_code == 201, published.text
    revision = published.json()
    assert revision["version_number"] == 1
    assert revision["change_summary"] == "first release"
    assert revision["segments"][0]["name"] == "Loop"

    # A published route rejects direct edits with an understandable conflict.
    blocked = client.patch(
        f"/api/v1/routes/{route_id}", params=actor, json={"description": "sneaky"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "invalid_state_transition"

    # Derive a new draft from v1, edit it, publish v2.
    derived = client.post(
        f"/api/v1/routes/{route_id}/revisions/1/derive", params=actor
    )
    assert derived.status_code == 200, derived.text
    assert derived.json()["draft_open"] is True
    edited = client.patch(
        f"/api/v1/routes/{route_id}", params=actor, json={"description": "second edition"}
    )
    assert edited.status_code == 200, edited.text
    republished = client.post(
        f"/api/v1/routes/{route_id}/publish",
        params=actor,
        json={"expected_version": edited.json()["version"]},
    )
    assert republished.status_code == 201, republished.text
    assert republished.json()["version_number"] == 2

    # Existing route queries return the current published version.
    current = client.get(f"/api/v1/routes/{route_id}")
    assert current.json()["current_version_number"] == 2
    assert current.json()["description"] == "second edition"
    listed = client.get("/api/v1/routes", params={"region": "API Mountains"})
    assert listed.json()["items"][0]["current_version_number"] == 2

    revisions = client.get(f"/api/v1/routes/{route_id}/revisions")
    assert [item["version_number"] for item in revisions.json()] == [1, 2]
    first = client.get(f"/api/v1/routes/{route_id}/revisions/1")
    assert first.json()["description"] == "first edition"
    diff = client.get(
        f"/api/v1/routes/{route_id}/revisions/diff",
        params={"from_version": 1, "to_version": 2},
    )
    assert diff.status_code == 200
    assert diff.json()["changed_fields"]["description"] == {
        "from": "first edition",
        "to": "second edition",
    }

    # The expedition pins the current revision (v2) at creation time.
    start = datetime.now(UTC) + timedelta(days=10)
    expedition = client.post(
        "/api/v1/expeditions",
        json={
            "organizer_id": user["id"],
            "route_id": route_id,
            "name": "API Expedition",
            "meeting_location": "Trailhead",
            "meeting_at": (start - timedelta(hours=1)).isoformat(),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=6)).isoformat(),
            "registration_deadline": (start - timedelta(days=1)).isoformat(),
            "capacity": 5,
        },
    )
    assert expedition.status_code == 201, expedition.text
    assert expedition.json()["route_revision_id"] == republished.json()["id"]
    assert expedition.json()["route_version_number"] == 2


def test_api_publish_with_stale_expected_version_returns_conflict(client) -> None:
    user = client.post(
        "/api/v1/users", json={"email": "stale@example.com", "display_name": "Stale"}
    ).json()
    actor = {"actor_id": user["id"]}
    route = client.post(
        "/api/v1/routes", params=actor, json=_api_route_payload(name="Stale Ridge")
    ).json()
    response = client.post(
        f"/api/v1/routes/{route['id']}/publish",
        params=actor,
        json={"expected_version": route["version"] + 9},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "conflict"
    assert "publish again" in detail["message"]


def test_api_revision_endpoints_validate_targets(client) -> None:
    user = client.post(
        "/api/v1/users", json={"email": "targets@example.com", "display_name": "Targets"}
    ).json()
    actor = {"actor_id": user["id"]}
    route = client.post(
        "/api/v1/routes",
        params=actor,
        json=_api_route_payload(name="Target Ridge", is_published=True),
    ).json()
    assert client.get(f"/api/v1/routes/{route['id']}/revisions/7").status_code == 404
    assert (
        client.post(
            f"/api/v1/routes/{route['id']}/revisions/7/derive", params=actor
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/routes/{route['id']}/revisions/diff",
            params={"from_version": 1, "to_version": 1},
        ).status_code
        == 422
    )
    missing = client.get("/api/v1/routes/9999/revisions")
    assert missing.status_code == 404
