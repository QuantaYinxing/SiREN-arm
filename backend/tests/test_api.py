"""API tests against the contract: auth, master data CRUD, dispatch flow, routing
fallback, water readings, restriction verification queue, reporter links, stats, snapshot,
and the WebSocket hub. SQLite dev store, estimate routing provider, injected clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from siren.api import create_app
from siren.config import Config
from siren.main import REPO_ROOT, build_app
from siren.store import Store
from siren.ws import Hub


class Clock:
    def __init__(self, start: datetime) -> None:
        self.dt = start

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, minutes: float) -> datetime:
        self.dt = self.dt + timedelta(minutes=minutes)
        return self.dt


@pytest.fixture()
def env(tmp_path):
    clock = Clock(datetime(2026, 11, 19, 7, 32, 0, tzinfo=timezone.utc))
    app, store, hub = build_app(
        db_path=tmp_path / "siren.sqlite",
        config_path=REPO_ROOT / "contracts" / "config.yaml",
        seed_dir=REPO_ROOT / "seed",
        routing_provider="estimate",
        now=clock,
    )
    client = TestClient(app)
    return client, store, hub, clock


def login(client: TestClient, username: str, password: str = "siren-dev") -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}, resp.json()


def receive_until(ws, etype: str, limit: int = 12) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == etype:
            return msg
    raise AssertionError(f"did not receive {etype}")


# ---- auth ----

def test_login_returns_token_and_role(env):
    client, store, hub, clock = env
    headers, body = login(client, "operator")
    assert body["role"] == "operator" and body["member_id"] is None
    assert body["refresh_token"]
    headers_officer, body_officer = login(client, "a.asep")
    assert body_officer["role"] == "officer" and body_officer["member_id"] == "A"


def test_account_crud_and_reauthentication(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    created = client.post("/accounts",
                          json={"username": "g.gigi", "password": "dev2", "role": "officer", "member_id": "A"},
                          headers=ops)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["username"] == "g.gigi" and row["member_id"] == "A" and "password_hash" not in row
    _, off = login(client, "g.gigi", "dev2")
    assert off["role"] == "officer" and off["member_id"] == "A"
    dup = client.post("/accounts", json={"username": "g.gigi", "password": "x"}, headers=ops)
    assert dup.status_code == 400 and dup.json()["error"] == "conflict"
    assert client.post("/accounts", json={"username": "h.hari", "password": "x", "role": "reporter"}, headers=ops).status_code == 400
    assert client.patch(f"/accounts/{row['id']}", json={"password": "dev3"}, headers=ops).status_code == 200
    assert client.post("/auth/login", json={"username": "g.gigi", "password": "dev2"}).status_code == 401
    _, rotated = login(client, "g.gigi", "dev3")
    assert rotated["role"] == "officer"
    assert client.delete(f"/accounts/{row['id']}", headers=ops).status_code == 200
    assert client.post("/auth/login", json={"username": "g.gigi", "password": "dev3"}).status_code == 401
    assert "g.gigi" not in [a["username"] for a in client.get("/accounts", headers=ops).json()]


def test_accounts_require_operator(env):
    client, store, hub, clock = env
    officer, _ = login(client, "a.asep")
    assert client.get("/accounts", headers=officer).status_code == 403
    assert client.post("/accounts", json={"username": "x", "password": "y"}, headers=officer).status_code == 403


def test_member_add_delete_and_busy_guard(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    m = client.post("/members", json={"name": "Orang Baru", "team_id": "t1", "role": "pemadam"}, headers=ops)
    assert m.status_code == 201, m.text
    mid = m.json()["id"]
    assert client.delete(f"/members/{mid}", headers=ops).status_code == 200
    assert client.get(f"/members/{mid}", headers=ops).status_code == 404
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77}, headers=ops).json()["id"]
    client.post(f"/incidents/{iid}/assignments", json={"members": ["A"], "vehicle_id": "v1", "water_source_id": "w1"}, headers=ops)
    assert client.delete("/members/A", headers=ops).status_code == 409
    assert store.member("A") is not None


def test_config_put_requires_operator(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    off, _ = login(client, "a.asep")
    assert client.put("/config/position.stale_after_seconds", json={"value": 120}, headers=ops).status_code == 200
    assert client.get("/config/position.stale_after_seconds").json()["value"] == 120
    assert client.put("/config/position.stale_after_seconds", json={"value": 200}, headers=off).status_code == 403
    assert client.put("/config/position.stale_after_seconds", json={"value": 300}).status_code == 401


# ---- snapshot ----

def test_snapshot_matches_contract_shape(env):
    client, store, hub, clock = env
    snap = client.get("/snapshot").json()
    for key in ("server_ts", "config", "teams", "members", "vehicles", "water_sources",
                "incidents", "assignments", "road_restrictions", "stats_today"):
        assert key in snap, key
    assert snap["config"]["member_status"]["Standby"]["label"] == "Standby"
    assert snap["config"]["member_status"]["EnRoute"]["label"] == "Menuju lokasi"
    assert {t["name"] for t in snap["teams"]} == {"Regu 1", "Regu 2"}
    assert {m["id"] for m in snap["members"]} == {"A", "B", "C", "D", "E", "F"}
    assert all(m["freshness"] in {"live", "stale", "offline"} for m in snap["members"])
    seed_water = json.loads((REPO_ROOT / "seed" / "water_sources.json").read_text())
    assert sorted(w["name"] for w in snap["water_sources"] if w["status"] == "active") == sorted(
        w["name"] for w in seed_water)


# ---- master data ----

def test_water_source_crud_and_soft_delete(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    created = client.post("/water-sources", json={"name": "Sumur Cibereum", "type": "well",
                                                  "lat": -7.16, "lon": 107.74}, headers=ops)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert sid in [w["id"] for w in client.get("/water-sources?status=active").json()]
    patched = client.patch(f"/water-sources/{sid}", json={"status": "broken"}, headers=ops)
    assert patched.json()["status"] == "broken"
    removed = client.delete(f"/water-sources/{sid}", headers=ops)
    assert removed.json()["status"] == "inactive"
    assert sid not in [w["id"] for w in client.get("/water-sources?status=active").json()]
    bad = client.post("/water-sources", json={"name": "x", "type": "unknown", "lat": 1, "lon": 2}, headers=ops)
    assert bad.status_code == 400


def test_bad_enum_is_rejected_with_allowed_values(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    resp = client.post("/water-sources", json={"name": "x", "type": "kolam", "lat": 1, "lon": 2}, headers=ops)
    assert resp.status_code == 400
    assert "hydrant" in str(resp.json())


# ---- incidents ----

def test_incident_create_and_illegal_status_transition(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    created = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77,
                                             "address": "Jl. Cihideung No. 41"}, headers=ops)
    assert created.status_code == 201
    iid = created.json()["id"]
    assert iid.startswith("I-")
    illegal = client.patch(f"/incidents/{iid}", json={"status": "Selesai"}, headers=ops)
    assert illegal.status_code == 409
    assert "Dispatched" in illegal.text
    ok = client.patch(f"/incidents/{iid}", json={"description": "ruko dua lantai"}, headers=ops)
    assert ok.json()["description"] == "ruko dua lantai"
    assert iid in [i["id"] for i in client.get("/incidents?status=active").json()]


def test_reporter_link_is_single_use_and_expiring(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    iid = client.post("/incidents", json={"type": "Kebakaran rumah", "lat": -7.12, "lon": 107.73},
                      headers=ops).json()["id"]
    link = client.post(f"/incidents/{iid}/reporter-link", json={"ttl_minutes": 30}, headers=ops).json()
    assert link["url"].startswith("https://siren.id/r/")
    got = client.post(f"/reporter/{link['token']}/location", json={"lat": -7.115, "lon": 107.755,
                                                                   "accuracy_m": 12, "name": "Warga"})
    assert got.status_code == 200
    assert got.json()["lat"] == pytest.approx(-7.115) and got.json()["reporter_name"] == "Warga"
    again = client.post(f"/reporter/{link['token']}/location", json={"lat": -7.1, "lon": 107.76})
    assert again.status_code == 410
    log = client.get("/log", headers=ops).json()
    assert any(l["type"] == "incident.updated" and "tautan" in l["text"] for l in log)


def test_reporter_link_expires(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    iid = client.post("/incidents", json={"type": "Kebakaran lahan", "lat": -7.14, "lon": 107.68},
                      headers=ops).json()["id"]
    link = client.post(f"/incidents/{iid}/reporter-link", json={"ttl_minutes": 5}, headers=ops).json()
    clock.advance(6)
    assert client.post(f"/reporter/{link['token']}/location", json={"lat": -7.14, "lon": 107.68}).status_code == 410


# ---- dispatch ----

def test_dispatch_creates_assignments_route_eta_and_log(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    client.post("/vehicles/v1/water-level", json={"level": 4, "source": "operator"}, headers=ops)
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    resp = client.post(f"/incidents/{iid}/assignments",
                       json={"members": ["A", "B"], "vehicle_id": "v1", "water_source_id": "w1"},
                       headers=ops)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["assignments"]) == 2
    assert {a["member_id"] for a in body["assignments"]} == {"A", "B"}
    assert body["route"]["provider"] == "estimate"
    assert len(body["route"]["legs"]) == 2
    assert body["route"]["refill_minutes"] == 0.0
    assert body["route"]["total_minutes"] > 0
    assert client.get("/vehicles/v1").json()["eta_minutes"] == pytest.approx(body["route"]["total_minutes"])
    assert client.get(f"/incidents/{iid}").json()["status"] == "Dispatched"
    assert client.get("/members/A").json()["status"] == "Dispatched"
    assert store.member("B")["status"] == "Dispatched"
    assert any(l["type"] == "assignment.updated" and "Truk 1" in l["text"] for l in client.get("/log").json())


def test_dispatch_busy_member_is_rejected(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    i1 = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                     headers=ops).json()["id"]
    i2 = client.post("/incidents", json={"type": "Kebakaran dapur", "lat": -7.13, "lon": 107.71},
                     headers=ops).json()["id"]
    client.post(f"/incidents/{i1}/assignments", json={"members": ["A"], "vehicle_id": "v1"}, headers=ops)
    resp = client.post(f"/incidents/{i2}/assignments", json={"members": ["A"]}, headers=ops)
    assert resp.status_code == 409
    assert "already dispatched" in resp.text


def test_dispatch_warnings_are_advisory_not_blocking(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    store.record_ping("C", -7.218, 107.72, 30)
    clock.advance(10)
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    resp = client.post(f"/incidents/{iid}/assignments", json={"members": ["A", "D"]}, headers=ops)
    assert resp.status_code == 201
    warnings = resp.json()["warnings"]
    assert any("kendaraan" in w for w in warnings)
    assert any("2 regu" in w for w in warnings)
    assert any("Posisi" in w for w in warnings)


def test_assignment_withdraw_returns_member_to_standby(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    asg = client.post(f"/incidents/{iid}/assignments", json={"members": ["A", "B"]}, headers=ops).json()["assignments"][0]
    patched = client.patch(f"/assignments/{asg['id']}", json={"water_source_id": "w2"}, headers=ops)
    assert patched.json()["water_source_id"] == "w2"
    client.delete(f"/assignments/{asg['id']}", headers=ops)
    assert store.member("A")["status"] == "Standby"
    assert client.get(f"/incidents/{iid}").json()["status"] == "Dispatched"  # B is still active
    remaining = [a for a in client.get(f"/incidents/{iid}").json()["assignments"]
                 if a["status"] == "Dispatched"][0]
    assert remaining["member_id"] == "B" and remaining["status"] == "Dispatched"
    client.delete(f"/assignments/{remaining['id']}", headers=ops)
    assert store.member("B")["status"] == "Standby"
    assert client.get(f"/incidents/{iid}").json()["status"] == "Baru"
    assert client.get(f"/vehicles/v1").json()["status"] == "Standby"


def test_assignment_patch_and_delete_use_id_routes(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    asg = client.post(f"/incidents/{iid}/assignments", json={"members": ["C"]}, headers=ops).json()["assignments"][0]
    patched = client.patch(f"/assignments/{asg['id']}", json={"vehicle_id": "v2"}, headers=ops)
    assert patched.json()["vehicle_id"] == "v2"
    assert client.delete(f"/assignments/{asg['id']}", headers=ops).status_code == 200


# ---- routing ----

def test_route_endpoint_applies_verified_restrictions(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    route = client.post("/routing/route", json={"vehicle_id": "v2",
                                                "to": {"lat": -7.38, "lon": 108.211}}, headers=ops).json()
    assert route["applied_restrictions"] == ["r1"]
    assert route["provider"] == "estimate"
    assert route["legs"][0]["minutes"] > 0
    assert any("r1" in w for w in route["warnings"])


def test_tiles_byte_range_serving(env, tmp_path):
    client, _store, _hub, _clock = env
    tiles_dir = REPO_ROOT / ".sandbox" / "data" / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    archive = tiles_dir / "test-range.pmtiles"
    archive.write_bytes(b"PMT" + bytes(range(256)) * 4)  # 1027 bytes
    try:
        full = client.get("/tiles/test-range.pmtiles")
        assert full.status_code == 200 and full.content == archive.read_bytes()
        assert full.headers["accept-ranges"] == "bytes"
        part = client.get("/tiles/test-range.pmtiles", headers={"Range": "bytes=4-11"})
        assert part.status_code == 206
        assert part.content == archive.read_bytes()[4:12]
        assert part.headers["content-range"] == f"bytes 4-11/{archive.stat().st_size}"
        suffix = client.get("/tiles/test-range.pmtiles", headers={"Range": "bytes=-8"})
        assert suffix.status_code == 206 and suffix.content == archive.read_bytes()[-8:]
        assert client.get("/tiles/nope.pmtiles").status_code == 404
        assert client.get("/tiles/../etc/passwd").status_code in (404, 422)
    finally:
        archive.unlink()


def test_water_source_options_endpoint(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    # no reading yet: conservative min level, so refill-first scoring applies
    unknown = client.post("/routing/water-source-options", json={"vehicle_id": "v1", "incident_id": iid},
                          headers=ops).json()
    assert unknown["water_level"] == 0 and unknown["refill_required"] is True
    assert unknown["options"][0]["score_minutes"] == round(
        unknown["options"][0]["a"]["minutes"] + unknown["options"][0]["refill_minutes"]
        + unknown["options"][0]["b"]["minutes"], 1)
    client.post("/vehicles/v1/water-level", json={"level": 4, "source": "operator"}, headers=ops)
    full = client.post("/routing/water-source-options", json={"vehicle_id": "v1", "incident_id": iid},
                       headers=ops).json()
    assert full["refill_required"] is False
    assert len(full["options"]) <= 3
    assert full["options"][0]["rank"] == 1
    assert full["options"][0]["score_minutes"] == full["options"][0]["b"]["minutes"]
    assert all(o["status"] == "active" for o in full["options"])
    client.post("/vehicles/v1/water-level", json={"level": 1, "source": "officer"}, headers=ops)
    low = client.post("/routing/water-source-options", json={"vehicle_id": "v1", "incident_id": iid},
                      headers=ops).json()
    assert low["refill_required"] is True
    refill = low["options"][0]["refill_minutes"]
    assert low["options"][0]["score_minutes"] == pytest.approx(
        low["options"][0]["a"]["minutes"] + refill + low["options"][0]["b"]["minutes"], abs=0.1)


# ---- water ----

def test_water_level_validation_and_refill_confirm(env):
    client, store, hub, clock = env
    off, _ = login(client, "a.asep")
    bad = client.post("/vehicles/v1/water-level", json={"level": 5, "source": "officer"}, headers=off)
    assert bad.status_code == 400
    ok = client.post("/vehicles/v1/water-level", json={"level": 2, "source": "officer"}, headers=off)
    assert ok.json()["water_level"] == 2 and ok.json()["water_level_source"] == "officer"
    unconfirmed = client.post("/vehicles/v1/refill-done", json={"confirmed": False}, headers=off)
    assert unconfirmed.status_code == 409
    done = client.post("/vehicles/v1/refill-done", json={"confirmed": True}, headers=off)
    assert done.json()["water_level"] == 4
    assert store.water_state("v1")["level"] == 4


def test_pump_estimate_broadcasts_estimate_reading(env):
    client, store, hub, clock = env
    off, _ = login(client, "a.asep")
    ops, _ = login(client, "operator")
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    client.post(f"/incidents/{iid}/assignments", json={"members": ["A"], "vehicle_id": "v1"}, headers=ops)
    client.patch("/members/A", json={"status": "EnRoute"}, headers=ops)
    client.patch("/members/A", json={"status": "OnScene"}, headers=ops)
    client.post("/vehicles/v1/water-level", json={"level": 4, "source": "operator"}, headers=ops)
    clock.advance(5)
    from siren import water
    changed = water.tick(store.config, store)
    assert [c["id"] for c in changed] == ["v1"]
    assert store.water_state("v1")["level"] == 0
    assert store.water_state("v1")["source"] == "estimate"


# ---- restrictions ----

def test_restriction_report_then_verify_flow(env):
    client, store, hub, clock = env
    off, _ = login(client, "b.budi")
    ops, _ = login(client, "operator")
    reported = client.post("/road-restrictions", json={"kind": "point",
                                                       "geometry": {"kind": "point", "coordinates": [[-7.13, 107.70]]},
                                                       "reason": "narrow", "photo_url": None}, headers=off)
    assert reported.status_code == 201
    rid = reported.json()["id"]
    assert reported.json()["status"] == "pending"
    assert rid in [r["id"] for r in client.get("/road-restrictions?status=pending").json()]
    refused = client.post(f"/road-restrictions/{rid}/verify", json={"status": "verified"}, headers=off)
    assert refused.status_code == 403
    verified = client.post(f"/road-restrictions/{rid}/verify", json={"status": "verified"}, headers=ops)
    assert verified.json()["status"] == "verified"
    route = client.post("/routing/route", json={"vehicle_id": "v1", "to": {"lat": -7.102, "lon": 107.77}},
                        headers=ops).json()
    assert rid in route["applied_restrictions"]
    rejected = client.post(f"/road-restrictions/{rid}/verify", json={"status": "rejected"}, headers=ops)
    assert rejected.json()["status"] == "rejected"
    assert rid not in client.post("/routing/route", json={"vehicle_id": "v1",
                                                          "to": {"lat": -7.102, "lon": 107.77}}, headers=ops).json()["applied_restrictions"]


def test_restriction_bad_geometry_is_rejected(env):
    client, store, hub, clock = env
    off, _ = login(client, "b.budi")
    resp = client.post("/road-restrictions", json={"kind": "line",
                                                   "geometry": {"kind": "line", "coordinates": [[-7.13, 107.7]]},
                                                   "reason": "narrow"}, headers=off)
    assert resp.status_code == 400


# ---- freshness & stats ----

def test_freshness_goes_stale_then_offline(env):
    client, store, hub, clock = env
    store.record_ping("A", -7.154, 107.7025, 8)
    assert client.get("/members/A").json()["freshness"] == "live"
    clock.advance(6)
    assert client.get("/members/A").json()["freshness"] == "stale"
    assert client.get("/members/A").json()["position_age_seconds"] > 300
    clock.advance(10)
    assert client.get("/members/A").json()["freshness"] == "offline"


def test_stats_today_counts_dispatches_and_refills(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    off, _ = login(client, "a.asep")
    stats = client.get("/stats/today").json()
    assert stats["date"] == clock().date().isoformat()
    assert stats["incidents_active"] >= 1  # seeded I-27 is Baru
    iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                      headers=ops).json()["id"]
    client.post(f"/incidents/{iid}/assignments", json={"members": ["A"], "vehicle_id": "v1"}, headers=ops)
    client.post("/vehicles/v1/water-level", json={"level": 1, "source": "officer"}, headers=off)
    client.post("/vehicles/v1/refill-done", json={"confirmed": True}, headers=off)
    stats = client.get("/stats/today").json()
    assert stats["incidents_active"] >= 2
    assert stats["refills"] == 1
    assert "v1" in stats["km_per_vehicle"]
    assert stats["avg_response_minutes"] is None


# ---- WebSocket ----

def test_ws_snapshot_then_replay_gap(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    with client.websocket_connect("/ws?app=ops") as ws:
        ws.send_json({"type": "snapshot.request", "payload": {}})
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert "members" in msg["payload"]
        last = msg["payload"]["last_event_id"]
        ws.send_json({"type": "snapshot.request", "payload": {"since_event_id": last}})
        got = ws.receive_json()
        assert got["type"] in {"snapshot", "incident.created", "incident.updated", "config.updated"}


def test_ws_officer_status_set_and_ping(env):
    client, store, hub, clock = env
    _, body = login(client, "a.asep")
    token = body["token"]
    with client.websocket_connect(f"/ws?app=field&token={token}&client_id=field-a") as ws:
        ws.send_json({"type": "heartbeat", "payload": {"client_id": "field-a", "app": "field", "member_id": "A"}})
        ws.send_json({"type": "status.set", "payload": {"member_id": "A", "status": "Istirahat"}})
        msg = receive_until(ws, "member.status")
        assert msg["payload"]["member_id"] == "A" and msg["payload"]["status"] == "Istirahat"
        assert msg["id"].startswith("ev_") and msg["v"] == 1
        assert store.member("A")["status"] == "Istirahat"
        ws.send_json({"type": "position.ping", "payload": {"member_id": "A",
                                                           "position": {"lat": -7.154, "lon": 107.7025, "accuracy_m": 8}}})
        pos = receive_until(ws, "member.position")
        assert pos["payload"]["freshness"] == "live"
        assert store.freshness("A")[0] == "live"


def test_ws_invalid_transition_returns_allowed_list(env):
    client, store, hub, clock = env
    _, body = login(client, "a.asep")
    with client.websocket_connect(f"/ws?app=field&token={body['token']}") as ws:
        ws.send_json({"type": "status.set", "payload": {"member_id": "A", "status": "OnScene"}})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "Dispatched" in msg["payload"]["allowed"]


def test_ws_officer_cannot_set_another_member(env):
    client, store, hub, clock = env
    _, body = login(client, "a.asep")
    with client.websocket_connect(f"/ws?app=field&token={body['token']}") as ws:
        ws.send_json({"type": "status.set", "payload": {"member_id": "F", "status": "Istirahat"}})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert store.member("F")["status"] == "Standby"


def test_ws_broadcast_reaches_operator_on_dispatch(env):
    client, store, hub, clock = env
    ops, _ = login(client, "operator")
    with client.websocket_connect("/ws?app=ops") as ws:
        ws.send_json({"type": "snapshot.request", "payload": {}})
        ws.receive_json()
        iid = client.post("/incidents", json={"type": "Kebakaran ruko", "lat": -7.102, "lon": 107.77},
                          headers=ops).json()["id"]
        client.post(f"/incidents/{iid}/assignments", json={"members": ["A"], "vehicle_id": "v1"}, headers=ops)
        assert receive_until(ws, "assignment.updated")["payload"]["incident_id"] == iid
        assert receive_until(ws, "incident.updated")["payload"]["status"] == "Dispatched"
