from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from trailforge.domain.enums import Difficulty, PointType, RiskLevel
from trailforge.schemas.common import ORMModel


class RouteRevisionPublish(BaseModel):
    """Publish the route's current working copy as the next immutable revision.

    expected_version is the optimistic-concurrency token of the trail_routes
    row (RouteResponse.version). When two publishes race for the same next
    version number, the one whose expected_version no longer matches fails
    with a 409 conflict instead of silently creating a later version.
    """

    expected_version: int | None = Field(default=None, ge=1)
    change_summary: str = Field(default="", max_length=2000)

    @field_validator("change_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        return " ".join(value.strip().split())


class RouteRevisionSegmentResponse(ORMModel):
    sequence: int
    name: str
    description: str
    distance_km: float
    elevation_gain_m: int
    estimated_duration_minutes: int
    difficulty: Difficulty
    start_latitude: float
    start_longitude: float
    end_latitude: float
    end_longitude: float


class RouteRevisionPointResponse(ORMModel):
    sequence: int
    name: str
    point_type: PointType
    latitude: float
    longitude: float
    altitude_m: int | None
    distance_from_start_km: float
    description: str
    supply_details: str


class RouteRevisionRiskTagResponse(ORMModel):
    risk_tag_id: int | None
    code: str
    name: str
    level: RiskLevel
    description: str
    mitigation: str


class RouteRevisionResponse(ORMModel):
    id: int
    route_id: int
    version_number: int
    change_summary: str
    published_by: int | None
    published_at: datetime
    name: str
    region: str
    description: str
    distance_km: float
    elevation_gain_m: int
    elevation_loss_m: int
    min_altitude_m: int
    max_altitude_m: int
    estimated_duration_minutes: int
    difficulty: Difficulty
    is_loop: bool
    segments: list[RouteRevisionSegmentResponse] = Field(default_factory=list)
    points: list[RouteRevisionPointResponse] = Field(default_factory=list)
    risk_tags: list[RouteRevisionRiskTagResponse] = Field(default_factory=list)


class RouteRevisionSummary(ORMModel):
    id: int
    route_id: int
    version_number: int
    change_summary: str
    published_by: int | None
    published_at: datetime
    distance_km: float
    difficulty: Difficulty


class RevisionChildDiff(BaseModel):
    added: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)
    changed: list[dict[str, Any]] = Field(default_factory=list)


class RouteRevisionDiff(BaseModel):
    route_id: int
    from_version: int
    to_version: int
    changed_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    segments: RevisionChildDiff = Field(default_factory=RevisionChildDiff)
    points: RevisionChildDiff = Field(default_factory=RevisionChildDiff)
    risk_tags_added: list[str] = Field(default_factory=list)
    risk_tags_removed: list[str] = Field(default_factory=list)
