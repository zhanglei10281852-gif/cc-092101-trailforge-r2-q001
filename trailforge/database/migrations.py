from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import selectinload

from trailforge.database.session import Database
from trailforge.models.audit import SchemaMigration
from trailforge.models.routes import RouteRevision, TrailRoute
from trailforge.revisioning import ROUTE_SCALAR_FIELDS, route_snapshot

INITIAL_REVISION_SUMMARY = "Initial revision created from the pre-revisioning published route"


@dataclass(frozen=True)
class Migration:
    version: str
    description: str


MIGRATIONS = [
    Migration(version="0001", description="Initial TrailForge schema"),
    Migration(
        version="0002",
        description="Immutable route revisions and revision-pinned expeditions",
    ),
]


def initialize_database(database: Database) -> list[str]:
    database.create_schema()
    applied: list[str] = []
    with database.session() as session:
        known = {
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        }
        for migration in MIGRATIONS:
            if migration.version in known:
                continue
            if migration.version == "0002":
                _apply_revision_migration(session)
            session.add(
                SchemaMigration(
                    version=migration.version,
                    description=migration.description,
                )
            )
            applied.append(migration.version)
    return applied


def _apply_revision_migration(session) -> None:
    """Add revision schema and freeze legacy published routes as revision 1.

    DDL and backfill run in the caller's transaction, so a crash rolls the
    whole migration back and it can be safely retried on next startup.
    """
    inspector = inspect(session.connection())
    route_columns = {column["name"] for column in inspector.get_columns("trail_routes")}
    if "current_revision_no" not in route_columns:
        session.execute(text("ALTER TABLE trail_routes ADD COLUMN current_revision_no INTEGER"))
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trail_routes_current_revision_no "
                "ON trail_routes (current_revision_no)"
            )
        )
    if "draft_source_revision_no" not in route_columns:
        session.execute(
            text("ALTER TABLE trail_routes ADD COLUMN draft_source_revision_no INTEGER")
        )
    tables = set(inspector.get_table_names())
    if "route_revisions" not in tables:
        RouteRevision.__table__.create(bind=session.connection())

    _backfill_legacy_revisions(session)

    expedition_columns = {column["name"] for column in inspector.get_columns("expeditions")}
    if "route_revision_id" not in expedition_columns:
        session.execute(
            text(
                "ALTER TABLE expeditions ADD COLUMN route_revision_id INTEGER "
                "REFERENCES route_revisions(id) ON DELETE RESTRICT"
            )
        )
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_expeditions_route_revision_id "
                "ON expeditions (route_revision_id)"
            )
        )
    if "route_revision_no" not in expedition_columns:
        session.execute(text("ALTER TABLE expeditions ADD COLUMN route_revision_no INTEGER"))
    _backfill_legacy_expedition_pins(session)
    _create_immutability_triggers(session)


def _create_immutability_triggers(session) -> None:
    session.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_route_revisions_no_update "
            "BEFORE UPDATE ON route_revisions "
            "BEGIN SELECT RAISE(ABORT, 'route revisions are immutable'); END"
        )
    )
    session.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_route_revisions_no_delete "
            "BEFORE DELETE ON route_revisions "
            "BEGIN SELECT RAISE(ABORT, 'route revisions cannot be deleted'); END"
        )
    )


def _backfill_legacy_revisions(session) -> None:
    """Every legacy published route becomes revision 1; drafts stay drafts."""
    inspector = inspect(session.connection())
    route_columns = {column["name"] for column in inspector.get_columns("trail_routes")}
    if "is_published" not in route_columns:
        # Fresh schema already created by metadata: no pre-revisioning rows.
        return
    published_ids = [
        row[0]
        for row in session.execute(text("SELECT id FROM trail_routes WHERE is_published = 1")).all()
    ]
    if not published_ids:
        return
    orm_routes = list(
        session.scalars(
            select(TrailRoute)
            .options(
                selectinload(TrailRoute.segments),
                selectinload(TrailRoute.points),
                selectinload(TrailRoute.risk_tags),
            )
            .where(TrailRoute.id.in_(published_ids))
        )
    )
    for route in orm_routes:
        existing = (
            session.query(RouteRevision).filter_by(route_id=route.id, revision_no=1).one_or_none()
        )
        if existing is not None:
            revision = existing
        else:
            snapshot = route_snapshot(route)
            revision = RouteRevision(
                route_id=route.id,
                revision_no=1,
                published_at=route.updated_at,
                published_by=None,
                derived_from_revision_no=None,
                change_summary=INITIAL_REVISION_SUMMARY,
                **{field: snapshot[field] for field in ROUTE_SCALAR_FIELDS},
                segments_json=snapshot["segments"],
                points_json=snapshot["points"],
                risk_tags_json=snapshot["risk_tags"],
            )
            session.add(revision)
            session.flush()
        if route.current_revision_no is None:
            route.current_revision_no = 1
    session.flush()


def _backfill_legacy_expedition_pins(session) -> None:
    rows = (
        session.execute(
            text(
                "SELECT e.id AS expedition_id, e.route_id AS route_id, r.id AS revision_id "
                "FROM expeditions e JOIN route_revisions r "
                "ON r.route_id = e.route_id AND r.revision_no = 1 "
                "WHERE e.route_revision_id IS NULL"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        session.execute(
            text(
                "UPDATE expeditions SET route_revision_id = :revision_id, "
                "route_revision_no = 1 WHERE id = :expedition_id"
            ),
            {"revision_id": row["revision_id"], "expedition_id": row["expedition_id"]},
        )
    orphans = session.execute(
        text("SELECT COUNT(*) FROM expeditions WHERE route_revision_id IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"migration 0002 cannot pin {orphans} expedition rows to a route revision"
        )
    session.flush()


def migration_status(database: Database) -> dict[str, object]:
    inspector = inspect(database.engine)
    if "schema_migrations" not in inspector.get_table_names():
        return {
            "initialized": False,
            "applied": [],
            "pending": [item.version for item in MIGRATIONS],
        }
    with database.session() as session:
        applied = [
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        ]
    pending = [item.version for item in MIGRATIONS if item.version not in set(applied)]
    return {"initialized": True, "applied": applied, "pending": pending}


def assert_database_integrity(database: Database) -> dict[str, object]:
    with database.engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        foreign_key_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        revision_issues = _revision_consistency_issues(connection)
    healthy = integrity == "ok" and not foreign_key_rows and not revision_issues
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "revision_issues": revision_issues,
        "healthy": healthy,
    }


def _revision_consistency_issues(connection) -> list[object]:
    inspector = inspect(connection)
    if "route_revisions" not in inspector.get_table_names():
        return []
    issues: list[object] = []
    dangling = connection.exec_driver_sql(
        "SELECT id, current_revision_no FROM trail_routes "
        "WHERE current_revision_no IS NOT NULL AND id NOT IN ("
        "SELECT route_id FROM route_revisions "
        "WHERE route_revisions.revision_no = trail_routes.current_revision_no)"
    ).all()
    for row in dangling:
        issues.append({"kind": "route_missing_current_revision", "route_id": row[0]})
    gaps = connection.exec_driver_sql(
        "SELECT route_id, GROUP_CONCAT(revision_no) FROM route_revisions GROUP BY route_id "
        "HAVING COUNT(*) != MAX(revision_no) OR MIN(revision_no) != 1"
    ).all()
    for row in gaps:
        issues.append(
            {"kind": "revision_chain_not_contiguous", "route_id": row[0], "numbers": row[1]}
        )
    pinned = connection.exec_driver_sql(
        "SELECT e.id FROM expeditions e LEFT JOIN route_revisions r "
        "ON r.id = e.route_revision_id "
        "WHERE e.route_revision_id IS NOT NULL AND r.id IS NULL"
    ).all()
    for row in pinned:
        issues.append({"kind": "expedition_revision_missing", "expedition_id": row[0]})
    return issues
