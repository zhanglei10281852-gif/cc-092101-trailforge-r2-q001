from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from trailforge.database.session import Database
from trailforge.models.audit import SchemaMigration


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    # Optional data/DDL step executed in the same transaction that records
    # the migration version, so a migration applies atomically.
    apply: Callable[[Session], None] | None = None


def _apply_0002(session: Session) -> None:
    """Introduce immutable route revisions on top of a legacy database.

    create_all has already created the new revision tables; this step adds
    the new columns to pre-existing tables, snapshots every already-published
    route as its first revision, pins existing expeditions to that revision,
    and installs triggers that make revision rows immutable.
    """
    from trailforge.database.base import utc_now
    from trailforge.models.revisions import (
        RouteRevision,
        RouteRevisionPoint,
        RouteRevisionRiskTag,
        RouteRevisionSegment,
    )
    from trailforge.models.routes import TrailRoute
    from trailforge.services.routes import (
        POINT_FIELDS,
        RISK_TAG_FIELDS,
        ROUTE_SNAPSHOT_FIELDS,
        SEGMENT_FIELDS,
    )

    connection = session.connection()
    new_columns = {
        "trail_routes": (
            "draft_open",
            "ALTER TABLE trail_routes ADD COLUMN draft_open BOOLEAN NOT NULL DEFAULT 1",
        ),
        "expeditions": (
            "route_revision_id",
            "ALTER TABLE expeditions ADD COLUMN route_revision_id INTEGER "
            "REFERENCES route_revisions(id)",
        ),
    }
    for table, (column, ddl) in new_columns.items():
        existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.exec_driver_sql(ddl)

    # Every route that was published before this upgrade becomes revision 1.
    # Its working copy is closed for drafting because it now represents the
    # published content; changes require deriving a new draft first.
    legacy_routes = list(
        session.scalars(select(TrailRoute).where(TrailRoute.is_published.is_(True)))
    )
    for route in legacy_routes:
        if route.revisions:
            continue
        revision = RouteRevision(
            route_id=route.id,
            version_number=1,
            change_summary="Migrated from legacy published route",
            published_by=None,
            published_at=utc_now(),
            **{field: getattr(route, field) for field in ROUTE_SNAPSHOT_FIELDS},
        )
        revision.segments = [
            RouteRevisionSegment(**{field: getattr(item, field) for field in SEGMENT_FIELDS})
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
        route.draft_open = False
        session.add(revision)
    session.flush()

    # Pin existing expeditions to the first revision of their route.
    connection.exec_driver_sql(
        """
        UPDATE expeditions
        SET route_revision_id = (
            SELECT id FROM route_revisions
            WHERE route_revisions.route_id = expeditions.route_id
            ORDER BY version_number ASC
            LIMIT 1
        )
        WHERE route_revision_id IS NULL
        """
    )

    # Revisions are immutable: no UPDATE, no DELETE, on any revision table.
    for table in (
        "route_revisions",
        "route_revision_segments",
        "route_revision_points",
        "route_revision_risk_tags",
    ):
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'route revisions are immutable');
            END
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'route revisions are immutable');
            END
            """
        )


MIGRATIONS = [
    Migration(version="0001", description="Initial TrailForge schema"),
    Migration(
        version="0002",
        description="Immutable route revisions and expedition revision pins",
        apply=_apply_0002,
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
            if migration.apply is not None:
                migration.apply(session)
            session.add(
                SchemaMigration(
                    version=migration.version,
                    description=migration.description,
                )
            )
            applied.append(migration.version)
    return applied


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
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "healthy": integrity == "ok" and not foreign_key_rows,
    }
