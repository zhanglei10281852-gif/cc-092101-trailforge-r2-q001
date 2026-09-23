"""Pure helpers for building immutable route revision snapshots."""

from __future__ import annotations

from typing import Any

ROUTE_SCALAR_FIELDS = (
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

TAG_FIELDS = ("id", "code", "name", "level", "description", "mitigation")


def _value(item: Any, field: str) -> Any:
    value = getattr(item, field)
    return value.value if hasattr(value, "value") else value


def segment_payload(segment: Any) -> dict[str, Any]:
    return {field: _value(segment, field) for field in SEGMENT_FIELDS}


def point_payload(point: Any) -> dict[str, Any]:
    return {field: _value(point, field) for field in POINT_FIELDS}


def tag_payload(tag: Any) -> dict[str, Any]:
    return {field: _value(tag, field) for field in TAG_FIELDS}


def route_snapshot(route: Any) -> dict[str, Any]:
    scalars = {field: _value(route, field) for field in ROUTE_SCALAR_FIELDS}
    scalars["segments"] = [segment_payload(item) for item in route.segments]
    scalars["points"] = [point_payload(item) for item in route.points]
    scalars["risk_tags"] = [tag_payload(item) for item in route.risk_tags]
    return scalars


def revision_snapshot(revision: Any) -> dict[str, Any]:
    scalars = {field: _value(revision, field) for field in ROUTE_SCALAR_FIELDS}
    scalars["segments"] = list(revision.segments_json)
    scalars["points"] = list(revision.points_json)
    scalars["risk_tags"] = list(revision.risk_tags_json)
    return scalars


def _field_changes(old: dict[str, Any], new: dict[str, Any], fields: tuple[str, ...]):
    changes: dict[str, Any] = {}
    for field in fields:
        old_value = old.get(field)
        new_value = new.get(field)
        if old_value != new_value:
            changes[field] = {"old": old_value, "new": new_value}
    return changes


def _collection_changes(
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    fields: tuple[str, ...],
):
    old_by_seq = {item["sequence"]: item for item in old_items}
    new_by_seq = {item["sequence"]: item for item in new_items}
    added = [item for seq, item in sorted(new_by_seq.items()) if seq not in old_by_seq]
    removed = [item for seq, item in sorted(old_by_seq.items()) if seq not in new_by_seq]
    changed = []
    for seq in sorted(old_by_seq.keys() & new_by_seq.keys()):
        fields_changed = _field_changes(old_by_seq[seq], new_by_seq[seq], fields)
        if fields_changed:
            changed.append({"sequence": seq, "fields": fields_changed})
    return {"added": added, "removed": removed, "changed": changed}


def diff_snapshots(
    old: dict[str, Any],
    new: dict[str, Any],
) -> dict[str, Any]:
    """Compute field/segment/point/risk-tag differences between two snapshots."""
    scalar_fields = tuple(field for field in ROUTE_SCALAR_FIELDS)
    fields = _field_changes(old, new, scalar_fields)
    segments = _collection_changes(old["segments"], new["segments"], tuple(SEGMENT_FIELDS[1:]))
    points = _collection_changes(old["points"], new["points"], tuple(POINT_FIELDS[1:]))
    old_tags = {item["code"]: item for item in old["risk_tags"]}
    new_tags = {item["code"]: item for item in new["risk_tags"]}
    return {
        "fields": fields,
        "segments": segments,
        "points": points,
        "risk_tags_added": [new_tags[code] for code in sorted(new_tags.keys() - old_tags.keys())],
        "risk_tags_removed": [old_tags[code] for code in sorted(old_tags.keys() - new_tags.keys())],
    }
