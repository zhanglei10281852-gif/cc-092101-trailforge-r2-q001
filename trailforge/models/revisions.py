from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trailforge.database.base import Base, UTCDateTime
from trailforge.domain.enums import Difficulty, PointType, RiskLevel
from trailforge.models.mixins import IntegerPrimaryKeyMixin, TimestampMixin


class RouteRevision(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable snapshot of a trail route captured at publish time.

    Rows in this table (and its child tables) are never updated or deleted;
    database triggers installed by migration 0002 enforce that. The current
    published version of a route is the revision with the highest
    version_number, so no mutable "current" pointer can drift.
    """

    __tablename__ = "route_revisions"
    __table_args__ = (
        UniqueConstraint("route_id", "version_number", name="uq_revision_route_version"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("distance_km > 0", name="distance_positive"),
        CheckConstraint("elevation_gain_m >= 0", name="gain_nonnegative"),
        CheckConstraint("elevation_loss_m >= 0", name="loss_nonnegative"),
        CheckConstraint("estimated_duration_minutes > 0", name="duration_positive"),
        CheckConstraint("min_altitude_m <= max_altitude_m", name="altitude_order"),
    )

    route_id: Mapped[int] = mapped_column(
        ForeignKey("trail_routes.id", ondelete="RESTRICT"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    name: Mapped[str] = mapped_column(String(180), nullable=False)
    region: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_gain_m: Mapped[int] = mapped_column(Integer, nullable=False)
    elevation_loss_m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_altitude_m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_altitude_m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty: Mapped[Difficulty] = mapped_column(String(24), nullable=False)
    is_loop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    segments: Mapped[list[RouteRevisionSegment]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="RouteRevisionSegment.sequence",
    )
    points: Mapped[list[RouteRevisionPoint]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="RouteRevisionPoint.sequence",
    )
    risk_tags: Mapped[list[RouteRevisionRiskTag]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="RouteRevisionRiskTag.code",
    )


class RouteRevisionSegment(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_revision_segments"
    __table_args__ = (
        UniqueConstraint("revision_id", "sequence", name="uq_revision_segment_sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("distance_km > 0", name="distance_positive"),
        CheckConstraint("elevation_gain_m >= 0", name="gain_nonnegative"),
        CheckConstraint("estimated_duration_minutes > 0", name="duration_positive"),
        CheckConstraint("start_latitude BETWEEN -90 AND 90", name="start_latitude_range"),
        CheckConstraint("end_latitude BETWEEN -90 AND 90", name="end_latitude_range"),
        CheckConstraint("start_longitude BETWEEN -180 AND 180", name="start_longitude_range"),
        CheckConstraint("end_longitude BETWEEN -180 AND 180", name="end_longitude_range"),
    )

    revision_id: Mapped[int] = mapped_column(
        ForeignKey("route_revisions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_gain_m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty: Mapped[Difficulty] = mapped_column(String(24), nullable=False)
    start_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    start_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    end_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    end_longitude: Mapped[float] = mapped_column(Float, nullable=False)

    revision: Mapped[RouteRevision] = relationship(back_populates="segments")


class RouteRevisionPoint(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_revision_points"
    __table_args__ = (
        UniqueConstraint("revision_id", "sequence", name="uq_revision_point_sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
        CheckConstraint("distance_from_start_km >= 0", name="distance_nonnegative"),
    )

    revision_id: Mapped[int] = mapped_column(
        ForeignKey("route_revisions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    point_type: Mapped[PointType] = mapped_column(String(24), nullable=False, index=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_m: Mapped[int | None] = mapped_column(Integer)
    distance_from_start_km: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    supply_details: Mapped[str] = mapped_column(Text, default="", nullable=False)

    revision: Mapped[RouteRevision] = relationship(back_populates="points")


class RouteRevisionRiskTag(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """Snapshot of a risk tag as it looked when the revision was published."""

    __tablename__ = "route_revision_risk_tags"
    __table_args__ = (
        UniqueConstraint("revision_id", "code", name="uq_revision_risk_tag_code"),
    )

    revision_id: Mapped[int] = mapped_column(
        ForeignKey("route_revisions.id", ondelete="CASCADE"), index=True
    )
    risk_tag_id: Mapped[int | None] = mapped_column(
        ForeignKey("risk_tags.id", ondelete="RESTRICT")
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[RiskLevel] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mitigation: Mapped[str] = mapped_column(Text, default="", nullable=False)

    revision: Mapped[RouteRevision] = relationship(back_populates="risk_tags")
