from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import create_expedition, create_route, create_user
from trailforge.config import Settings
from trailforge.database.migrations import (
    INITIAL_REVISION_SUMMARY,
    assert_database_integrity,
    initialize_database,
)
from trailforge.database.session import Database
from trailforge.errors import ConflictError, ValidationError
from trailforge.models.activities import Expedition
from trailforge.models.audit import AuditLog
from trailforge.models.routes import RouteRevision, TrailRoute
from trailforge.schemas.activities import ExpeditionCreate
from trailforge.schemas.routes import (
    DeriveDraftRequest,
    RouteFilter,
    RoutePublishRequest,
    RouteSegmentCreate,
    TrailRouteCreate,
    TrailRouteUpdate,
)
from trailforge.services.activities import ExpeditionService
from trailforge.services.routes import RouteService

UTC = UTC


def _route_payload(name: str = "Revision Ridge", distance: float = 10.0) -> TrailRouteCreate:
    return TrailRouteCreate(
        name=name,
        region="Revision Mountains",
        description="First draft",
        distance_km=distance,
        elevation_gain_m=500,
        elevation_loss_m=500,
        min_altitude_m=100,
        max_altitude_m=600,
        estimated_duration_minutes=240,
        difficulty="moderate",
        is_loop=True,
        segments=[
            RouteSegmentCreate(
                sequence=1,
                name="Main ridge",
                description="rocky",
                distance_km=distance,
                elevation_gain_m=500,
                estimated_duration_minutes=240,
                difficulty="moderate",
                start_latitude=30.0,
                start_longitude=120.0,
                end_latitude=30.1,
                end_longitude=120.1,
            )
        ],
    )


def _publish(session, route_id: int, actor_id: int, **kwargs):
    return RouteService(session).publish_route(
        route_id, RoutePublishRequest(**kwargs), actor_id=actor_id
    )


def test_publish_creates_immutable_revision_one(session) -> None:
    user_id = create_user(session)
    route = RouteService(session).create_route(
        _route_payload(),
        actor_id=user_id,
    )
    assert route.is_published is False
    assert route.current_revision_no is None

    revision = _publish(session, route.id, user_id, change_summary="initial release")
    assert revision.revision_no == 1
    assert revision.segments[0].name == "Main ridge"
    assert revision.change_summary == "initial release"

    stored = session.get(RouteRevision, revision.id)
    assert stored.segments[0]["distance_km"] == 10.0
    audit = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "route_revision",
            AuditLog.action == "published",
        )
    )
    assert audit.context["revision_no"] == 1


def test_published_route_query_returns_current_revision(session) -> None:
    user_id = create_user(session)
    route = RouteService(session).create_route(_route_payload(), actor_id=user_id)
    _publish(session, route.id, user_id)

    # Draft diverges from the published revision.
    segment = route_segments(session, route.id)[0]
    segment.distance_km = 12.0
    session.flush()
    RouteService(session).update_route(
        route.id,
        TrailRouteUpdate(description="draft edit", distance_km=12.0),
        actor_id=user_id,
    )
    detail = RouteService(session).get_route(route.id)
    assert detail.description == "First draft"
    assert detail.distance_km == 10.0
    assert detail.current_revision_no == 1

    draft = RouteService(session).get_draft(route.id)
    assert draft.description == "draft edit"
    assert draft.distance_km == 12.0

    listing = RouteService(session).list_routes(RouteFilter(region="Revision"))
    assert listing.items[0].distance_km == 10.0


def test_revision_numbers_are_continuous_and_content_freezes(session) -> None:
    user_id = create_user(session)
    route = RouteService(session).create_route(_route_payload(), actor_id=user_id)
    first = _publish(session, route.id, user_id)
    assert first.revision_no == 1

    # Make the draft internally inconsistent at the ORM level: the service
    # would reject this edit, so publishing must refuse it too.
    stored = session.get(TrailRoute, route.id)
    stored.distance_km = 12.0
    session.flush()
    with pytest.raises(ValidationError):
        # Segments still total 10 km; geometry validation blocks this draft.
        _publish(session, route.id, user_id)
    stored.distance_km = 10.0
    session.flush()

    # Adjust the draft properly: segment first, then the scalar fields.
    segment = route_segments(session, route.id)[0]
    segment.distance_km = 12.0
    segment.elevation_gain_m = 600
    segment.estimated_duration_minutes = 300
    session.flush()
    RouteService(session).update_route(
        route.id,
        TrailRouteUpdate(
            distance_km=12.0,
            elevation_gain_m=600,
            estimated_duration_minutes=300,
        ),
        actor_id=user_id,
    )
    second = _publish(session, route.id, user_id, change_summary="longer route")
    assert second.revision_no == 2
    assert second.distance_km == 12.0

    revisions = RouteService(session).list_revisions(route.id)
    assert [item.revision_no for item in revisions] == [1, 2]
    # Revision 1 is untouched.
    assert RouteService(session).get_revision(route.id, 1).distance_km == 10.0


def route_segments(session, route_id: int):
    from trailforge.models.routes import RouteSegment

    return list(session.scalars(select(RouteSegment).where(RouteSegment.route_id == route_id)))


def test_diff_reports_field_and_collection_changes(session) -> None:
    user_id = create_user(session)
    route = RouteService(session).create_route(_route_payload(), actor_id=user_id)
    _publish(session, route.id, user_id)
    segment = route_segments(session, route.id)[0]
    segment.distance_km = 12.0
    segment.elevation_gain_m = 700
    segment.estimated_duration_minutes = 300
    session.flush()
    RouteService(session).update_route(
        route.id,
        TrailRouteUpdate(
            distance_km=12.0,
            elevation_gain_m=700,
            estimated_duration_minutes=300,
            description="Rerouted",
        ),
        actor_id=user_id,
    )
    _publish(session, route.id, user_id)

    diff = RouteService(session).diff_revisions(route.id, 1, 2)
    assert diff.fields["distance_km"].old == 10.0
    assert diff.fields["distance_km"].new == 12.0
    assert diff.fields["description"].old == "First draft"
    assert diff.fields["description"].new == "Rerouted"
    changed_sequences = {item.sequence for item in diff.segments.changed}
    assert changed_sequences == {1}
    fields_changed = diff.segments.changed[0].fields
    assert fields_changed["distance_km"].new == 12.0


def test_derive_draft_from_old_revision_restores_snapshot(session) -> None:
    user_id = create_user(session)
    route = RouteService(session).create_route(_route_payload(), actor_id=user_id)
    _publish(session, route.id, user_id, change_summary="v1")
    segment = route_segments(session, route.id)[0]
    segment.distance_km = 12.0
    session.flush()
    RouteService(session).update_route(
        route.id,
        TrailRouteUpdate(distance_km=12.0, description="v2 draft"),
        actor_id=user_id,
    )
    _publish(session, route.id, user_id, change_summary="v2")

    restored = RouteService(session).derive_draft(
        route.id, DeriveDraftRequest(revision_no=1), actor_id=user_id
    )
    assert restored.distance_km == 10.0
    assert restored.description == "First draft"
    assert restored.draft_source_revision_no == 1
    # Current published revision is still v2 while the draft now mirrors v1.
    assert restored.current_revision_no == 2

    _publish(session, route.id, user_id, change_summary="revert to v1 alignment")
    revisions = RouteService(session).list_revisions(route.id)
    assert [item.revision_no for item in revisions] == [1, 2, 3]
    assert revisions[2].derived_from_revision_no == 1
    assert revisions[2].distance_km == 10.0


def test_old_activity_does_not_drift_after_new_revision(session) -> None:
    organizer = create_user(session)
    route_id = create_route(session, actor_id=organizer, name="Pinned Ridge")
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route_id)
    expedition = session.get(Expedition, expedition_id)
    assert expedition.route_revision_no == 1
    pinned_revision_id = expedition.route_revision_id

    # Edit the draft and publish a new revision.
    segment = route_segments(session, route_id)[0]
    segment.distance_km = 12.0
    segment.elevation_gain_m = 600
    segment.estimated_duration_minutes = 300
    session.flush()
    RouteService(session).update_route(
        route_id,
        TrailRouteUpdate(
            distance_km=12.0,
            elevation_gain_m=600,
            estimated_duration_minutes=300,
        ),
        actor_id=organizer,
    )
    _publish(session, route_id, organizer)

    session.expire_all()
    expedition = session.get(Expedition, expedition_id)
    assert expedition.route_revision_id == pinned_revision_id
    assert expedition.route_revision_no == 1
    pinned = ExpeditionService(session).pinned_route_revision(expedition_id)
    assert pinned.revision_no == 1
    assert pinned.distance_km == 10.0

    # A new expedition defaults to the fresh current revision.
    second_id = create_expedition(
        session, organizer_id=organizer, route_id=route_id, offset_days=20
    )
    assert session.get(Expedition, second_id).route_revision_no == 2

    # Pinning a non-existent revision number is rejected.
    start = datetime.now(UTC) + timedelta(days=30)
    with pytest.raises(ValidationError, match="revision does not exist"):
        ExpeditionService(session).create(
            ExpeditionCreate(
                organizer_id=organizer,
                route_id=route_id,
                route_revision_no=99,
                name="Future",
                meeting_location="Trailhead",
                meeting_at=start - timedelta(hours=1),
                start_at=start,
                end_at=start + timedelta(hours=6),
                registration_deadline=start - timedelta(days=1),
                capacity=3,
                risk_level="moderate",
            )
        )


def test_published_revisions_cannot_be_updated_or_deleted(database: Database) -> None:
    setup = database.session_factory()
    user_id = create_user(setup)
    route = RouteService(setup).create_route(_route_payload(), actor_id=user_id)
    revision = RouteService(setup).publish_route(route.id, RoutePublishRequest(), actor_id=user_id)
    setup.commit()
    setup.close()
    route_id, revision_id = route.id, revision.id

    session = database.session_factory()
    try:
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text("UPDATE route_revisions SET distance_km = 99 WHERE id = :id"),
                {"id": revision_id},
            )
        session.rollback()
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            session.execute(text("DELETE FROM route_revisions WHERE id = :id"), {"id": revision_id})
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM trail_routes WHERE id = :id"), {"id": route_id})
        session.rollback()
    finally:
        session.close()

    verify = database.session_factory()
    try:
        assert verify.get(RouteRevision, revision_id).distance_km == 10.0
    finally:
        verify.close()


def test_snapshot_keeps_risk_tag_even_if_tag_later_changed(session) -> None:
    user_id = create_user(session)
    from trailforge.models.routes import RiskTag
    from trailforge.schemas.routes import RiskTagCreate

    tag = RouteService(session).create_risk_tag(
        RiskTagCreate(
            code="rockfall",
            name="Rockfall",
            level="high",
            description="loose scree",
        ),
        actor_id=user_id,
    )
    payload = _route_payload(name="Tagged Ridge")
    payload.risk_tag_ids.append(tag.id)
    route = RouteService(session).create_route(payload, actor_id=user_id)
    revision = _publish(session, route.id, user_id)
    assert revision.risk_tags[0].code == "rockfall"

    session.query(RiskTag).filter_by(id=tag.id).delete()
    session.flush()
    stored = session.get(RouteRevision, revision.id)
    assert stored.risk_tags_json[0]["code"] == "rockfall"
    assert stored.risk_tags_json[0]["level"] == "high"


def test_publish_rolls_back_everything_on_failure(database: Database) -> None:
    session = database.session_factory()
    user_id = create_user(session)
    route = RouteService(session).create_route(
        _route_payload(name="Rollback Ridge"), actor_id=user_id
    )
    route_id = route.id
    session.commit()
    session.close()

    session = database.session_factory()
    try:
        revision = RouteService(session).publish_route(
            route_id,
            RoutePublishRequest(change_summary="doomed"),
            actor_id=user_id,
        )
        assert revision.revision_no == 1
        # Force a failure in the same transaction after the publish writes.
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO trail_routes (id, name, region, description, distance_km, "
                    "elevation_gain_m, elevation_loss_m, min_altitude_m, max_altitude_m, "
                    "estimated_duration_minutes, difficulty, is_loop, version, created_at, "
                    "updated_at) VALUES (:id, 'dupe', 'Revision Mountains', '', 1, 0, 0, 0, 0, "
                    "10, 'easy', 0, 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                ),
                {"id": route_id},
            )
        session.rollback()
    finally:
        session.close()

    verify = database.session_factory()
    try:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(RouteRevision)
                .where(RouteRevision.route_id == route_id)
            )
            == 0
        )
        stored = verify.get(TrailRoute, route_id)
        assert stored.current_revision_no is None
        assert (
            verify.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_type == "route_revision")
            )
            == 0
        )
    finally:
        verify.close()


def _concurrent_publish(database: Database, route_id: int, actor_id: int):
    session = database.session_factory()
    try:
        revision = RouteService(session).publish_route(
            route_id, RoutePublishRequest(), actor_id=actor_id
        )
        session.commit()
        return ("ok", revision.revision_no)
    except ConflictError as exc:
        session.rollback()
        return ("conflict", exc.message)
    finally:
        session.close()


def test_concurrent_publish_only_one_claims_next_version(database: Database) -> None:
    setup = database.session_factory()
    actor_id = create_user(setup)
    route_id = (
        RouteService(setup)
        .create_route(_route_payload(name="Contention Ridge"), actor_id=actor_id)
        .id
    )
    setup.commit()
    setup.close()

    # Deterministic interleaving: A claims revision 1 but has not committed
    # when B starts its own claim for the same next number.
    session_a = database.session_factory()
    revision_a = RouteService(session_a).publish_route(
        route_id, RoutePublishRequest(), actor_id=actor_id
    )
    assert revision_a.revision_no == 1

    b_started = threading.Event()

    def publish_b():
        session_b = database.session_factory()
        b_started.set()
        try:
            RouteService(session_b).publish_route(
                route_id, RoutePublishRequest(), actor_id=actor_id
            )
            session_b.commit()
            return "ok"
        except ConflictError as exc:
            session_b.rollback()
            return exc.message
        finally:
            session_b.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish_b)
        b_started.wait(timeout=2)
        # Give B time to block on SQLite's write lock, then let A commit.
        threading.Event().wait(0.3)
        session_a.commit()
        session_a.close()
        outcome = future.result(timeout=10)

    assert "concurrently" in outcome
    checker = database.session_factory()
    try:
        numbers = [
            row.revision_no
            for row in checker.scalars(
                select(RouteRevision).where(RouteRevision.route_id == route_id)
            )
        ]
        assert numbers == [1]
    finally:
        checker.close()


def test_concurrent_publish_barrier_never_duplicates_version(database: Database) -> None:
    setup = database.session_factory()
    actor_id = create_user(setup)
    route_id = (
        RouteService(setup).create_route(_route_payload(name="Barrier Ridge"), actor_id=actor_id).id
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(6)

    def publish():
        barrier.wait()
        return _concurrent_publish(database, route_id, actor_id)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda _: publish(), range(6)))

    successes = [item for item in outcomes if item[0] == "ok"]
    conflicts = [item for item in outcomes if item[0] == "conflict"]
    assert len(successes) >= 1
    assert len(successes) + len(conflicts) == 6
    claimed_numbers = sorted(item[1] for item in successes)
    assert claimed_numbers == list(range(1, len(successes) + 1))
    checker = database.session_factory()
    try:
        numbers = sorted(
            checker.scalars(
                select(RouteRevision.revision_no).where(RouteRevision.route_id == route_id)
            )
        )
        assert numbers == claimed_numbers
    finally:
        checker.close()


def test_expedition_creation_is_atomic(database: Database) -> None:
    session = database.session_factory()
    organizer = create_user(session)
    draft = RouteService(session).create_route(
        _route_payload(name="Unpublished"), actor_id=organizer
    )
    session.commit()

    start = datetime.now(UTC) + timedelta(days=10)
    with pytest.raises(ValidationError):
        ExpeditionService(session).create(
            ExpeditionCreate(
                organizer_id=organizer,
                route_id=draft.id,
                name="Should not exist",
                meeting_location="Trailhead",
                meeting_at=start - timedelta(hours=1),
                start_at=start,
                end_at=start + timedelta(hours=6),
                registration_deadline=start - timedelta(days=1),
                capacity=3,
                risk_level="moderate",
            )
        )
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Expedition)) == 0
    session.close()


# ---------------------------------------------------------------------------
# Legacy database migration
# ---------------------------------------------------------------------------

LEGACY_DDL = [
    "CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "version VARCHAR(60) NOT NULL UNIQUE, description TEXT NOT NULL, "
    "applied_at VARCHAR(32) NOT NULL)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email VARCHAR(254) NOT NULL "
    "UNIQUE, display_name VARCHAR(100) NOT NULL, phone VARCHAR(40), birth_date DATE, "
    "locale VARCHAR(16) NOT NULL, timezone VARCHAR(64) NOT NULL, is_active BOOLEAN NOT NULL, "
    "created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL)",
    "CREATE TABLE trail_routes (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(180) NOT NULL, "
    "region VARCHAR(120) NOT NULL, description TEXT NOT NULL, distance_km FLOAT NOT NULL, "
    "elevation_gain_m INTEGER NOT NULL, elevation_loss_m INTEGER NOT NULL, "
    "min_altitude_m INTEGER NOT NULL, max_altitude_m INTEGER NOT NULL, "
    "estimated_duration_minutes INTEGER NOT NULL, difficulty VARCHAR(24) NOT NULL, "
    "is_loop BOOLEAN NOT NULL, is_published BOOLEAN NOT NULL, version INTEGER NOT NULL, "
    "created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL)",
    "CREATE TABLE route_segments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE CASCADE, "
    "sequence INTEGER NOT NULL, name VARCHAR(160) NOT NULL, description TEXT NOT NULL, "
    "distance_km FLOAT NOT NULL, elevation_gain_m INTEGER NOT NULL, "
    "estimated_duration_minutes INTEGER NOT NULL, difficulty VARCHAR(24) NOT NULL, "
    "start_latitude FLOAT NOT NULL, start_longitude FLOAT NOT NULL, "
    "end_latitude FLOAT NOT NULL, end_longitude FLOAT NOT NULL, "
    "created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL)",
    "CREATE TABLE route_points (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE CASCADE, "
    "sequence INTEGER NOT NULL, name VARCHAR(160) NOT NULL, point_type VARCHAR(24) NOT NULL, "
    "latitude FLOAT NOT NULL, longitude FLOAT NOT NULL, altitude_m INTEGER, "
    "distance_from_start_km FLOAT NOT NULL, description TEXT NOT NULL, "
    "supply_details TEXT NOT NULL, created_at VARCHAR(32) NOT NULL, "
    "updated_at VARCHAR(32) NOT NULL)",
    "CREATE TABLE risk_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, code VARCHAR(64) NOT NULL "
    "UNIQUE, name VARCHAR(120) NOT NULL, level VARCHAR(24) NOT NULL, description TEXT NOT NULL, "
    "mitigation TEXT NOT NULL, created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL)",
    "CREATE TABLE route_risk_tags (route_id INTEGER NOT NULL "
    "REFERENCES trail_routes(id) ON DELETE CASCADE, risk_tag_id INTEGER NOT NULL "
    "REFERENCES risk_tags(id) ON DELETE CASCADE, PRIMARY KEY (route_id, risk_tag_id))",
    "CREATE TABLE expeditions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "organizer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT, "
    "route_id INTEGER NOT NULL REFERENCES trail_routes(id) ON DELETE RESTRICT, "
    "name VARCHAR(180) NOT NULL, description TEXT NOT NULL, "
    "meeting_location VARCHAR(240) NOT NULL, meeting_at VARCHAR(32) NOT NULL, "
    "start_at VARCHAR(32) NOT NULL, end_at VARCHAR(32) NOT NULL, "
    "registration_deadline VARCHAR(32) NOT NULL, capacity INTEGER NOT NULL, "
    "minimum_fitness_level INTEGER NOT NULL, status VARCHAR(24) NOT NULL, "
    "risk_level VARCHAR(24) NOT NULL, cancellation_reason TEXT NOT NULL, "
    "version INTEGER NOT NULL, created_at VARCHAR(32) NOT NULL, "
    "updated_at VARCHAR(32) NOT NULL)",
]

TS = "2026-01-01T00:00:00.000000Z"


def _build_legacy_database(settings: Settings) -> None:
    import sqlite3

    path = settings.database_path
    connection = sqlite3.connect(path)
    try:
        for statement in LEGACY_DDL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, description, applied_at) "
            "VALUES ('0001', 'Initial TrailForge schema', ?)",
            (TS,),
        )
        connection.execute(
            "INSERT INTO users (id, email, display_name, locale, timezone, is_active, "
            "created_at, updated_at) VALUES (1, 'lead@example.com', 'Legacy Lead', 'zh-CN', "
            "'Asia/Shanghai', 1, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO trail_routes (id, name, region, description, distance_km, "
            "elevation_gain_m, elevation_loss_m, min_altitude_m, max_altitude_m, "
            "estimated_duration_minutes, difficulty, is_loop, is_published, version, "
            "created_at, updated_at) VALUES (1, 'Legacy Alpine', 'Old Range', 'legacy desc', "
            "15.5, 900, 900, 200, 1100, 420, 'hard', 0, 1, 3, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO trail_routes (id, name, region, description, distance_km, "
            "elevation_gain_m, elevation_loss_m, min_altitude_m, max_altitude_m, "
            "estimated_duration_minutes, difficulty, is_loop, is_published, version, "
            "created_at, updated_at) VALUES (2, 'Legacy Draft', 'Old Range', 'unpublished', "
            "8.0, 100, 100, 100, 200, 180, 'easy', 1, 0, 1, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO route_segments (id, route_id, sequence, name, description, distance_km, "
            "elevation_gain_m, estimated_duration_minutes, difficulty, start_latitude, "
            "start_longitude, end_latitude, end_longitude, created_at, updated_at) "
            "VALUES (1, 1, 1, 'Legacy climb', 'scree', 15.5, 900, 420, 'hard', 30.0, 120.0, "
            "30.2, 120.2, ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO route_points (id, route_id, sequence, name, point_type, latitude, "
            "longitude, altitude_m, distance_from_start_km, description, supply_details, "
            "created_at, updated_at) VALUES (1, 1, 1, 'Spring camp', 'water', 30.1, 120.1, "
            "850, 8.0, 'Mountain spring', '2 litres per person', ?, ?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO risk_tags (id, code, name, level, description, mitigation, "
            "created_at, updated_at) VALUES (1, 'rockfall', 'Rockfall', 'high', 'loose scree', "
            "'helmet required', ?, ?)",
            (TS, TS),
        )
        connection.execute("INSERT INTO route_risk_tags (route_id, risk_tag_id) VALUES (1, 1)")
        start = "2026-02-10T00:00:00.000000Z"
        end = "2026-02-10T08:00:00.000000Z"
        meeting = "2026-02-09T23:00:00.000000Z"
        deadline = "2026-02-09T00:00:00.000000Z"
        connection.execute(
            "INSERT INTO expeditions (id, organizer_id, route_id, name, description, "
            "meeting_location, meeting_at, start_at, end_at, registration_deadline, capacity, "
            "minimum_fitness_level, status, risk_level, cancellation_reason, version, "
            "created_at, updated_at) VALUES (1, 1, 1, 'Legacy Expedition', '', 'Trailhead', "
            "?, ?, ?, ?, 8, 1, 'open', 'moderate', '', 1, ?, ?)",
            (meeting, start, end, deadline, TS, TS),
        )
        connection.commit()
    finally:
        connection.close()


def test_legacy_database_backfills_first_revision_and_pins_activities(
    tmp_path,
) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'legacy.db'}")
    _build_legacy_database(settings)

    database = Database(settings)
    applied = initialize_database(database)
    assert applied == ["0002"]

    with database.session() as session:
        published = session.get(TrailRoute, 1)
        assert published.current_revision_no == 1
        draft = session.get(TrailRoute, 2)
        assert draft.current_revision_no is None

        revision = session.scalar(
            select(RouteRevision).where(RouteRevision.route_id == 1, RouteRevision.revision_no == 1)
        )
        assert revision is not None
        assert revision.distance_km == 15.5
        assert revision.difficulty == "hard"
        assert revision.segments[0]["name"] == "Legacy climb"
        assert revision.points[0]["point_type"] == "water"
        assert revision.points[0]["supply_details"] == "2 litres per person"
        assert revision.risk_tags_json[0]["code"] == "rockfall"
        assert revision.risk_tags_json[0]["level"] == "high"
        assert revision.change_summary == INITIAL_REVISION_SUMMARY

        expedition = session.get(Expedition, 1)
        assert expedition.route_revision_id == revision.id
        assert expedition.route_revision_no == 1

    # Restart: version relationships stay consistent, migration is idempotent.
    database.engine.dispose()
    restarted = Database(settings)
    assert initialize_database(restarted) == []
    from trailforge.database.migrations import migration_status

    assert migration_status(restarted)["applied"] == ["0001", "0002"]
    with restarted.session() as session:
        expedition = session.get(Expedition, 1)
        pinned = session.get(RouteRevision, expedition.route_revision_id)
        assert pinned.revision_no == 1
        assert pinned.distance_km == 15.5
        # The old activity cannot drift: publishing a new revision keeps it on v1.
        route = session.get(TrailRoute, 1)
        assert route.current_revision_no == 1

    integrity = assert_database_integrity(restarted)
    assert integrity["healthy"] is True
    assert integrity["revision_issues"] == []
    restarted.engine.dispose()


def test_legacy_revision_is_immutable_after_migration(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'legacy2.db'}")
    _build_legacy_database(settings)
    database = Database(settings)
    initialize_database(database)
    with database.session() as session:
        revision_id = session.scalar(select(RouteRevision.id).where(RouteRevision.route_id == 1))
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text("UPDATE route_revisions SET distance_km = 1 WHERE id = :id"),
                {"id": revision_id},
            )
    database.engine.dispose()
