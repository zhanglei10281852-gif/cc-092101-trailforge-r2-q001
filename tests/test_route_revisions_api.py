from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from trailforge.config import Settings
from trailforge.database.session import Database
from trailforge.main import create_app


def _create_user(client) -> int:
    response = client.post(
        "/api/v1/users",
        json={"email": "rev@example.com", "display_name": "Rev Hiker"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_draft_route(client, user_id: int, name: str = "HTTP Ridge") -> int:
    response = client.post(
        "/api/v1/routes",
        params={"actor_id": user_id},
        json={
            "name": name,
            "region": "HTTP Mountains",
            "description": "draft body",
            "distance_km": 10,
            "elevation_gain_m": 500,
            "elevation_loss_m": 500,
            "min_altitude_m": 100,
            "max_altitude_m": 600,
            "estimated_duration_minutes": 240,
            "difficulty": "moderate",
            "is_loop": True,
            "segments": [
                {
                    "sequence": 1,
                    "name": "Main ridge",
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
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_published"] is False
    return body["id"]


def test_publish_list_get_diff_derive_flow(client) -> None:
    user_id = _create_user(client)
    route_id = _create_draft_route(client, user_id)

    # An unpublished route cannot back an expedition.
    bad = client.post(
        "/api/v1/expeditions",
        json=_expedition_body(user_id, route_id),
    )
    assert bad.status_code == 422

    published = client.post(
        f"/api/v1/routes/{route_id}/publish",
        params={"actor_id": user_id},
        json={"change_summary": "first release"},
    )
    assert published.status_code == 201, published.text
    revision = published.json()
    assert revision["revision_no"] == 1
    assert revision["segments"][0]["name"] == "Main ridge"

    listing = client.get(f"/api/v1/routes/{route_id}/revisions")
    assert listing.status_code == 200
    assert [item["revision_no"] for item in listing.json()] == [1]

    detail = client.get(f"/api/v1/routes/{route_id}")
    assert detail.json()["current_revision_no"] == 1
    assert detail.json()["distance_km"] == 10

    # Edit the draft directly on the segment table isn't possible over HTTP;
    # create a derived draft from revision 1 instead (identity derivation).
    derived = client.post(
        f"/api/v1/routes/{route_id}/derive-draft",
        params={"actor_id": user_id},
        json={"revision_no": 1},
    )
    assert derived.status_code == 200
    assert derived.json()["draft_source_revision_no"] == 1

    # Unknown revision -> 404.
    missing = client.get(f"/api/v1/routes/{route_id}/revisions/99")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"


def test_concurrent_publish_http_returns_one_winner_and_one_conflict(
    tmp_path,
) -> None:
    import uvicorn

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'http.db'}",
        sqlite_timeout_seconds=5,
        sqlite_busy_retries=4,
        sqlite_busy_backoff_seconds=0.01,
    )
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
        )
        user = client.post(
            "/api/v1/users",
            json={"email": "race@example.com", "display_name": "Racer"},
        ).json()
        route_id = _create_draft_route(client, user["id"], name="Race Ridge")

        def publish():
            response = client.post(
                f"/api/v1/routes/{route_id}/publish",
                params={"actor_id": user["id"]},
                json={},
            )
            return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: publish(), range(2)))

        statuses = sorted(item[0] for item in outcomes)
        assert statuses == [201, 409]
        conflict = next(item[1] for item in outcomes if item[0] == 409)
        assert conflict["detail"]["code"] == "conflict"
        assert "concurrently" in conflict["detail"]["message"]
        assert conflict["detail"]["context"]["attempted_revision_no"] == 1

        database = Database(settings)
        with database.session() as session:
            from sqlalchemy import func, select

            from trailforge.models.routes import RouteRevision

            count = session.scalar(select(func.count()).where(RouteRevision.route_id == route_id))
            assert count == 1
        database.engine.dispose()
        client.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_expedition_pins_revision_and_exposes_it(client) -> None:
    user_id = _create_user(client)
    route_id = _create_draft_route(client, user_id, name="Pin Ridge")
    client.post(
        f"/api/v1/routes/{route_id}/publish",
        params={"actor_id": user_id},
        json={},
    )

    created = client.post("/api/v1/expeditions", json=_expedition_body(user_id, route_id))
    assert created.status_code == 201, created.text
    expedition = created.json()
    assert expedition["route_revision_no"] == 1
    assert expedition["route_revision_id"] is not None

    pinned = client.get(f"/api/v1/expeditions/{expedition['id']}/route-revision")
    assert pinned.status_code == 200
    assert pinned.json()["revision_no"] == 1
    assert pinned.json()["distance_km"] == 10


def _expedition_body(user_id: int, route_id: int, offset_days: int = 10) -> dict:
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC) + timedelta(days=offset_days)
    return {
        "organizer_id": user_id,
        "route_id": route_id,
        "name": "Revision Expedition",
        "meeting_location": "Trailhead",
        "meeting_at": (start - timedelta(hours=1)).isoformat(),
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=6)).isoformat(),
        "registration_deadline": (start - timedelta(days=1)).isoformat(),
        "capacity": 5,
        "minimum_fitness_level": 1,
        "risk_level": "moderate",
    }
