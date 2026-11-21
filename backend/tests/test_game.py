"""Game-mode simulator tests: deterministic spawns, crew lifecycle, speed scaling,
game endpoints, freshness under skipped standby pings. Injected epoch clock, fixed seed —
game mode replays identically without any internet (docs/siren.md §9, rules.md §5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from siren import game
from siren.game import GameClock, Simulator
from siren.main import REPO_ROOT, build_app
from siren.ws import Hub
from fastapi.testclient import TestClient


class EpochClock:
    def __init__(self, base_epoch: float = 1_700_000_000.0) -> None:
        self.dt = datetime.fromtimestamp(base_epoch, timezone.utc)

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, minutes: float) -> float:
        self.dt = self.dt + timedelta(minutes=minutes)
        return self.dt.timestamp()


def make_env(tmp_path, mode="game", routing_provider="estimate"):
    clock = EpochClock()
    app, store, hub = build_app(
        db_path=tmp_path / "siren.sqlite",
        config_path=REPO_ROOT / "contracts" / "config.yaml",
        seed_dir=REPO_ROOT / "seed",
        routing_provider=routing_provider,
        now=clock,
        mode=mode,
    )
    return TestClient(app), store, hub, clock, app


def login(client, username="operator", password="siren-dev") -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_spawn_is_deterministic_for_one_seed(tmp_path):
    client1, store1, hub1, clock1, _ = make_env(tmp_path)
    sim1 = Simulator(store1, store1.config, hub1, seed=4242)
    client2, store2, hub2, clock2, _ = make_env(tmp_path / "b")
    sim2 = Simulator(store2, store2.config, hub2, seed=4242)
    a = sim1.spawn_now()
    b = sim2.spawn_now()
    assert (a["type"], a["address"], a["lat"], a["lon"], a["reporter_name"]) == \
           (b["type"], b["address"], b["lat"], b["lon"], b["reporter_name"])
    c = sim1.spawn_now()
    assert (c["type"], c["address"]) != (a["type"], a["address"])


def test_game_endpoint_only_in_game_mode(tmp_path):
    client, store, hub, clock, app = make_env(tmp_path, mode="game")
    ops = login(client)
    resp = client.post("/game/incident", headers=ops)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "Baru"
    app.state.sim.tick(store.now_seconds())
    state = client.get("/game/state", headers=ops).json()
    assert state["mode"] == "game" and state["ticks"] >= 1
    real_client, _store, _hub, _clock, _app = make_env(tmp_path / "r", mode="real")
    ops = login(real_client)
    resp = real_client.post("/game/incident", headers=ops)
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert real_client.get("/game/state", headers=ops).json()["mode"] == "real"


def test_game_speed_validation_and_effect(tmp_path, monkeypatch):
    client, store, hub, clock, app = make_env(tmp_path)
    ops = login(client)
    bad = client.put("/config/game.speed", json={"value": 6}, headers=ops)
    assert bad.status_code == 400
    assert bad.json()["allowed"] == [1, 2, 4, 8]
    ok = client.put("/config/game.speed", json={"value": 8}, headers=ops)
    assert ok.status_code == 200
    assert store.config.get("game.speed") == 8

    class FakeTime:
        value = 100.0

        @staticmethod
        def time():
            return FakeTime.value

    monkeypatch.setattr(game, "time", FakeTime)
    clock8 = GameClock(base=100.0, speed=lambda: 8.0)
    FakeTime.value = 101.0
    assert clock8().timestamp() == 108.0
    FakeTime.value = 100.0
    clock1 = GameClock(base=100.0, speed=lambda: 1.0)
    FakeTime.value = 101.0
    assert clock1().timestamp() == 101.0


def test_crew_lifecycle_dispatch_to_selesai(tmp_path):
    client, store, hub, clock, app = make_env(tmp_path)
    ops = login(client)
    sim = app.state.sim
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.25, "lon": 107.75},
                      headers=ops).json()["id"]
    resp = client.post(f"/incidents/{iid}/assignments",
                       json={"members": ["A", "B"], "vehicle_id": "v1", "water_source_id": "w1"},
                       headers=ops)
    assert resp.status_code == 201, resp.text
    route_min = resp.json()["route"]["total_minutes"]

    clock.advance(store.config.get("game.departure_delay_minutes", 1) + 0.1)
    sim.tick(store.now_seconds())
    assert store.member("A")["status"] == "EnRoute"
    assert store.incident(iid)["status"] == "EnRoute"

    clock.advance(route_min + 0.1)
    sim.tick(store.now_seconds())
    assert store.member("A")["status"] == "OnScene"
    assert store.incident(iid)["status"] == "OnScene"
    assert store.incident(iid)["arrived_at"] is not None
    veh = store.vehicle("v1")
    assert veh["km_today"] > 0
    assert veh["position"] is not None

    clock.advance(store.config.get("game.scene_minutes", 12) + 0.1)
    sim.tick(store.now_seconds())
    after_scene = store.member("A")["status"]
    assert after_scene in ("IsiUlang", "Returning")
    if after_scene == "IsiUlang":
        clock.advance(store.config.get("water.refill_minutes", 3) + 0.1)
        sim.tick(store.now_seconds())
        assert store.vehicle("v1")["water_level"] == 4
        assert store.member("A")["status"] == "Returning"

    clock.advance(store.config.get("game.scene_minutes", 12) + 30)
    sim.tick(store.now_seconds())
    assert store.member("A")["status"] == "Standby"
    assert store.member("B")["status"] == "Standby"
    assert store.incident(iid)["status"] == "Selesai"
    assert store.incident(iid)["finished_at"] is not None
    assert store.vehicle("v1")["status"] == "Standby"
    assert store.vehicle("v1")["eta_minutes"] is None
    log_types = [row["type"] for row in store.event_log(50)]
    assert "incident.updated" in log_types and "member.status" in log_types


def test_standby_pings_keep_freshness_live(tmp_path):
    client, store, hub, clock, app = make_env(tmp_path)
    sim = app.state.sim
    interval = float(store.config.get("position.ping_standby_seconds", 300))
    for _ in range(12):
        clock.advance(interval / 2)
        sim.tick(store.now_seconds())
    live = [m for m in store.members() if m["status"] == "Standby" and m["freshness"] == "live"]
    assert len(live) >= 1


def test_officer_reports_restriction_while_enroute(tmp_path):
    client, store, hub, clock, app = make_env(tmp_path)
    ops = login(client)
    store.config = store.config.replaced("game.restriction_report_probability", 1.0)
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.25, "lon": 107.75},
                      headers=ops).json()["id"]
    client.post(f"/incidents/{iid}/assignments",
                json={"members": ["A"], "vehicle_id": "v1"}, headers=ops)
    clock.advance(store.config.get("game.departure_delay_minutes", 1) + 0.5)
    sim = app.state.sim
    sim.tick(store.now_seconds())
    sim.tick(store.now_seconds())
    pending = [r for r in store.restrictions("pending")]
    assert any(r["reported_by"] == "Asep" and r["status"] == "pending" for r in pending)
