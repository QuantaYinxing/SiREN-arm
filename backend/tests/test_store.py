"""Store tests: SQLite dev store, seeded data, injected clock, WS event buffer."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from siren.config import Config
from siren.geo import haversine_km
from siren.store import (
    _INCIDENT_KEYS,
    _MEMBER_KEYS,
    _VEHICLE_KEYS,
    Conflict,
    LinkError,
    NotFound,
    Store,
)
from siren.timeutil import add_minutes, now_iso

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "contracts" / "config.yaml"
SEED_DIR = ROOT / "seed"


def _seed(kind: str) -> list[dict]:
    return json.loads((SEED_DIR / f"{kind}.json").read_text())

T0 = datetime(2026, 11, 19, 7, 32, 0, tzinfo=timezone.utc)
YESTERDAY = (T0 - timedelta(days=1)).date().isoformat()


def make_store(db_path, config=None, limit=500):
    clock = {"t": T0}
    store = Store(db_path, config or Config.load(CONFIG_PATH),
                  now=lambda: clock["t"], event_buffer_limit=limit)
    store.init_schema()

    def advance(minutes: float) -> None:
        clock["t"] += timedelta(minutes=minutes)

    return store, advance


def seeded_store(db_path, limit=500):
    store, advance = make_store(db_path, limit=limit)
    store.load_seed(SEED_DIR)
    return store, advance


def team_member(store, member_id="A", team_id="t1", vehicle_id=None):
    store.create_team({"id": team_id, "name": "Regu", "color": "#ff9f68", "display_order": 0})
    if vehicle_id:
        store.create_vehicle({"id": vehicle_id, "name": "Truk", "vehicle_type": "truck",
                             "team_id": team_id, "lat": -7.15, "lon": 107.70})
    store.create_member({"id": member_id, "name": member_id, "team_id": team_id,
                         "vehicle_id": vehicle_id})
    return member_id


def _sha256(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# ---- lifecycle / seed ----

def test_init_schema_is_repeatable(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    store.init_schema()  # CREATE TABLE IF NOT EXISTS: second run must not fail
    assert store.stations() == []
    assert store.teams() == []
    with pytest.raises(NotFound):
        store.member("missing")


def test_load_seed_counts_and_is_idempotent(tmp_path):
    store, _ = seeded_store(tmp_path / "db.sqlite")
    counts = store.load_seed(SEED_DIR)
    assert counts == {"station": 0, "teams": 0, "vehicle_profiles": 0, "vehicles": 0,
                      "members": 0, "accounts": 0, "water_sources": 0, "road_restrictions": 0,
                      "incidents": 0, "assignments": 0}

    store2, _ = make_store(tmp_path / "fresh.sqlite")
    first = store2.load_seed(SEED_DIR)
    assert first == {"station": 1, "teams": 2, "vehicle_profiles": 3, "vehicles": 2,
                     "members": 6, "accounts": 7, "water_sources": len(_seed("water_sources")),
                     "road_restrictions": 2,
                     "incidents": 2, "assignments": 3}


def test_seed_content_and_status_consistency(tmp_path):
    store, _ = seeded_store(tmp_path / "db.sqlite")
    assert store.station_position() == (-7.311924330474647, 108.19915732405596)
    assert store.stations()[0]["name"] == "Pos Damkar Tasikmalaya"
    teams = store.teams()
    assert [t["id"] for t in teams] == ["t1", "t2"]
    assert teams[0]["color"] == "#ff9f68"
    assert store.vehicle_profile("vp_truck")["valhalla_costing"] == {
        "type": "truck", "use_osm_trucking_on_road": False, "max_haulage_costing": True}
    assert store.vehicle("v1")["plate"] == "Z 8123 AB"
    assert store.vehicle("v2")["team_id"] == "t2"
    members = store.members()
    assert sorted(m["id"] for m in members) == ["A", "B", "C", "D", "E", "F"]
    assert all(m["status"] == "Standby" for m in members)
    assert all(v["status"] == "Standby" for v in store.vehicles())
    assert store.authenticate("operator", "siren-dev")["role"] == "operator"
    assert store.authenticate("a.asep", "siren-dev")["member_id"] == "A"
    assert store.authenticate("b.budi", "wrong-password") is None
    sources = {w["id"]: w for w in store.water_sources()}
    assert sources, "water seed must not be empty"
    assert all(s["status"] == "active" for s in sources.values())
    assert all(s["notes"] == "OSM placeholder, needs field survey" for s in sources.values())
    for w in _seed("water_sources"):
        assert sources[w["id"]]["access_points"] == [{"lat": w["lat"], "lon": w["lon"]}]
    restrictions = {r["id"]: r for r in store.restrictions()}
    assert restrictions["r1"]["geometry"] == {
        "kind": "polygon",
        "coordinates": [[-7.336, 108.2], [-7.336, 108.222], [-7.364, 108.222], [-7.364, 108.2]]}
    assert restrictions["r2"]["status"] == "pending"
    assert restrictions["r2"]["reported_member_id"] == "B"
    assert store.active_restrictions("truck") == [restrictions["r1"]]
    inc25 = store.incident("I-25")
    assert inc25["status"] == "Selesai" and inc25["finished_at"] is not None
    assert sorted(a["member_id"] for a in inc25["assignments"]) == ["D", "E", "F"]
    assert all(a["status"] == "Completed" and a["vehicle_id"] == "v2"
               and a["water_source_id"] == "w1" for a in inc25["assignments"])
    assert store.incident("I-27")["assignments"] == []
    assert store.incidents("active") == [store.incident("I-27")]


# ---- master data CRUD ----

def test_team_crud(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    team = store.create_team({"name": "Regu 3", "color": "#00ff00", "display_order": 2})
    assert team["id"].startswith("team_")
    updated = store.update_team(team["id"], {"name": "Regu 3b", "display_order": 1})
    assert updated["name"] == "Regu 3b" and updated["display_order"] == 1
    assert store.teams() == [updated]


def test_member_crud(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    member = store.create_member({"id": "A", "name": "Asep", "role": "komandan", "team_id": "t9"})
    assert member["status"] == "Standby"
    assert member["freshness"] == "offline" and member["position_age_seconds"] is None
    updated = store.update_member("A", {"status": "Dispatched"})
    assert updated["status"] == "Dispatched"
    store.create_member({"id": "B", "name": "Budi", "team_id": "t8"})
    assert [m["id"] for m in store.members("t9")] == ["A"]
    assert len(store.members()) == 2


def test_profile_crud(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    profile = store.create_profile({"id": "vp_x", "name": "Truk X", "vehicle_type": "truck",
                                    "height_m": 3.2, "valhalla_costing": {"type": "truck"}})
    assert profile["valhalla_costing"] == {"type": "truck"}
    updated = store.update_profile("vp_x", {"height_m": 3.8})
    assert updated["height_m"] == 3.8
    assert store.vehicle_profiles() == [updated]


def test_vehicle_crud(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    store.create_team({"id": "t1", "name": "Regu"})
    vehicle = store.create_vehicle({"id": "v1", "name": "Truk 1", "vehicle_type": "truck",
                                    "team_id": "t1", "pump_flow_lpm": 800, "tank_capacity_l": 4000,
                                    "lat": -7.15, "lon": 107.70})
    assert vehicle["position"] == {"lat": -7.15, "lon": 107.7}
    assert vehicle["water_level"] == 0 and vehicle["water_level_source"] == "estimate"
    assert vehicle["estimated"] is True and vehicle["km_today"] == 0.0
    updated = store.update_vehicle("v1", {"eta_minutes": 8})
    assert updated["eta_minutes"] == 8
    assert store.set_vehicle_status("v1", "Dispatched")["status"] == "Dispatched"
    assert store.vehicle_position("v1") == (-7.15, 107.7)


def test_water_source_crud_and_soft_delete(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    ws = store.create_water_source({"id": "w9", "name": "Sumur", "type": "well",
                                    "lat": -7.2, "lon": 107.65})
    assert ws["access_points"] == [{"lat": -7.2, "lon": 107.65}]
    updated = store.update_water_source("w9", {"status": "broken", "capacity_note": "rusak",
                                               "access_points": [{"lat": -7.19, "lon": 107.66}]})
    assert updated["status"] == "broken"
    assert updated["access_points"] == [{"lat": -7.19, "lon": 107.66}]
    removed = store.remove_water_source("w9")
    assert removed["status"] == "inactive" and removed["updated_at"] is not None
    assert store.water_sources("active") == []
    assert store.water_sources("all") == [removed]


# ---- freshness / positions ----

def test_freshness_thresholds(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    team_member(store)
    store.create_member({"id": "Z", "name": "Zen", "team_id": "t1"})
    ping = store.record_ping("A", -7.15, 107.7, accuracy_m=8)
    assert ping["freshness"] == "live" and ping["position_age_seconds"] == 0
    assert ping["position"] == {"lat": -7.15, "lon": 107.7, "accuracy_m": 8}
    expected_age = 0.0
    for minutes, expected in [(0, "live"), (5, "live"), (0.5, "stale"), (9.5, "stale"), (1, "offline")]:
        advance(minutes)
        expected_age += minutes * 60
        state, age = store.freshness("A")
        assert state == expected
        assert age == pytest.approx(expected_age)
    by_id = {m["id"]: m for m in store.members()}
    assert by_id["A"]["freshness"] == "offline"
    assert by_id["A"]["position_age_seconds"] == pytest.approx(960.0)
    assert by_id["Z"]["freshness"] == "offline" and by_id["Z"]["position"] is None
    assert by_id["A"]["position"] == {"lat": -7.15, "lon": 107.7, "accuracy_m": 8}


def test_record_ping_moves_vehicle(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    team_member(store, vehicle_id="v1")
    store.record_ping("A", -7.2, 107.8, vehicle_id="v1")
    assert store.vehicle_position("v1") == (-7.2, 107.8)
    assert store.latest_ping("A")["vehicle_id"] == "v1"


def test_km_today_only_counts_pings_of_the_utc_date(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    team_member(store, vehicle_id="v1")
    p1, p2, p3 = (-7.15, 107.70), (-7.16, 107.71), (-7.17, 107.72)
    store.record_ping("A", *p1, vehicle_id="v1")
    advance(1)
    store.record_ping("A", *p2, vehicle_id="v1")
    advance(1)
    store.record_ping("A", *p3, vehicle_id="v1")
    expected = haversine_km(p1, p2) + haversine_km(p2, p3)
    assert store.km_today("v1") == pytest.approx(expected)
    # yesterday's pings do not leak into today's total
    advance(-24 * 60)
    q1, q2 = (-7.0, 107.5), (-7.01, 107.51)
    store.record_ping("A", *q1, vehicle_id="v1")
    advance(1)
    store.record_ping("A", *q2, vehicle_id="v1")
    advance(24 * 60 - 1)
    assert store.km_today("v1") == pytest.approx(expected)
    assert store.km_today("v1", YESTERDAY) == pytest.approx(haversine_km(q1, q2))


def test_prune_pings(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    team_member(store)
    advance(-2 * 24 * 60)
    store.record_ping("A", -7.15, 107.70)
    advance(2 * 24 * 60)
    store.record_ping("A", -7.16, 107.71)
    assert store.prune_pings(retention_days=1) == 1
    assert store.prune_pings(retention_days=1) == 0
    assert store.latest_ping("A")["lat"] == -7.16
    assert store.prune_pings() == 0  # default retention from config keeps today's ping


# ---- water ----

def test_water_state_default_without_reading(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    store.create_vehicle({"id": "v1", "name": "Truk", "vehicle_type": "truck"})
    state = store.water_state("v1")
    assert state == {"level": 0, "source": "estimate", "confirmed_at": None,
                     "estimated": True, "age_seconds": None}
    assert store.latest_water_reading("v1") is None


def test_water_reading_and_refill(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    store.create_vehicle({"id": "v1", "name": "Truk", "vehicle_type": "truck"})
    row = store.record_water_level("v1", 2, "operator")
    assert row["water_level"] == 2 and row["water_level_source"] == "operator"
    assert row["estimated"] is False and row["water_level_confirmed_at"] == now_iso(T0)
    state = store.water_state("v1")
    assert state["level"] == 2 and state["age_seconds"] == 0
    with pytest.raises(Conflict):
        store.refill_done("v1", confirmed=False)
    full = store.refill_done("v1")
    assert full["water_level"] == 4 and full["water_level_source"] == "operator"
    assert store.refills_today() == 1
    assert store.refills_today(YESTERDAY) == 0
    # a vehicle whose first reading is already full counts as a refill from empty
    store.create_vehicle({"id": "v2", "name": "Truk 2", "vehicle_type": "truck"})
    store.refill_done("v2")
    assert store.refills_today() == 2


def test_refills_today_date_boundary(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    store.create_vehicle({"id": "v1", "name": "Truk", "vehicle_type": "truck"})
    advance(-24 * 60)
    store.record_water_level("v1", 2, "officer")  # yesterday, not a refill
    advance(24 * 60)
    store.refill_done("v1")
    assert store.refills_today(YESTERDAY) == 0
    assert store.refills_today() == 1


# ---- incidents & dispatch ----

def test_incident_ids_continue_sequence(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    first = store.create_incident({"type": "Kebakaran rumah", "lat": -7.15, "lon": 107.7})
    assert first["id"] == "I-1"
    second = store.create_incident({"type": "Kebakaran ruko", "lat": -7.16, "lon": 107.7})
    assert second["id"] == "I-2"
    seeded, _ = seeded_store(tmp_path / "seeded.sqlite")
    assert all(v == 0 for v in seeded.load_seed(SEED_DIR).values())
    nxt = seeded.create_incident({"type": "Kebakaran gudang", "lat": -7.1, "lon": 107.7})
    assert nxt["id"] == "I-28"


def test_assignments_create_duplicate_conflict_and_withdraw(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    team_member(store, "A")
    store.create_member({"id": "B", "name": "Budi", "team_id": "t1"})
    incident = store.create_incident({"type": "Kebakaran", "lat": -7.15, "lon": 107.7})
    rows = store.create_assignments(incident["id"], ["A", "B"], vehicle_id="v1",
                                   roles={"A": "komandan"})
    assert len(rows) == 2 and rows[0]["status"] == "Dispatched"
    assert rows[0]["role"] == "komandan"
    assert rows[0]["id"].startswith("asg_")
    with pytest.raises(Conflict):
        store.create_assignments(incident["id"], ["A"])
    with pytest.raises(Conflict):
        store.create_assignments(incident["id"], ["A", "B"])

    store.update_member("A", {"status": "Dispatched"})
    withdrawn = store.withdraw_assignment(rows[0]["id"])
    assert withdrawn["status"] == "Withdrawn" and withdrawn["updated_at"] is not None
    assert store.member("A")["status"] == "Dispatched"  # withdrawal does not touch duty status
    assert store.incident(incident["id"])["assignments"] == [withdrawn, rows[1]]
    assert [m["id"] for m in store.member_for_incident(incident["id"])] == ["A", "B"]
    assert store.assignments("no-such-incident") == []


# ---- reporter links ----

def test_reporter_link_create_consume_expire(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    incident = store.create_incident({"type": "Kebakaran", "lat": -7.15, "lon": 107.7})
    link = store.create_reporter_link(incident["id"])
    assert link["status"] == "pending"
    assert link["expires_at"] == add_minutes(now_iso(T0), 30)  # config ttl
    assert link["url"].startswith("https://siren.id/r/")
    assert store.reporter_link(link["token"])["incident_id"] == incident["id"]

    consumed = store.consume_reporter_link(link["token"], -7.102, 107.77, accuracy_m=12,
                                           name="Warga via tautan")
    assert consumed["lat"] == -7.102 and consumed["lon"] == 107.77
    assert consumed["reporter_link_id"] == link["token"]
    assert consumed["reporter_name"] == "Warga via tautan"
    row = store.reporter_link(link["token"])
    assert row["status"] == "received" and row["used_at"] == now_iso(T0)
    assert row["lat"] == -7.102 and row["accuracy_m"] == 12

    with pytest.raises(LinkError):
        store.consume_reporter_link(link["token"], -7.2, 107.6)
    with pytest.raises(LinkError):
        store.consume_reporter_link("nope-token", -7.2, 107.6)

    expired = store.create_reporter_link(incident["id"], ttl_minutes=10)
    advance(11)
    with pytest.raises(LinkError):
        store.consume_reporter_link(expired["token"], -7.2, 107.6)
    assert store.reporter_link(expired["token"])["status"] == "expired"
    assert store.reporter_links("pending") == []
    assert len(store.reporter_links("all")) == 2


# ---- restrictions ----

def test_restriction_verify_transitions_and_filtering(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    narrow = store.create_restriction({"kind": "point", "reason": "narrow",
                                       "geometry": {"kind": "point", "coordinates": [[-7.13, 107.6625]]}})
    assert narrow["status"] == "pending"
    with pytest.raises(ValueError):
        store.verify_restriction(narrow["id"], "pending")
    with pytest.raises(ValueError):
        store.verify_restriction(narrow["id"], "accepted")
    rejected = store.verify_restriction(narrow["id"], "rejected", note="sudah longgar")
    assert rejected["status"] == "rejected"
    assert store.active_restrictions("truck") == []
    verified = store.verify_restriction(narrow["id"], "verified", verified_by="acct_1")
    assert verified["status"] == "verified" and verified["verified_at"] == now_iso(T0)
    assert [r["id"] for r in store.active_restrictions("truck")] == [narrow["id"]]
    assert store.active_restrictions("motorbike") == []

    moto = store.create_restriction({"kind": "point", "reason": "blocked",
                                     "affected_vehicle_type": "motorbike",
                                     "geometry": {"kind": "point", "coordinates": [[-7.14, 107.71]]},
                                     "status": "verified"})
    assert [r["id"] for r in store.active_restrictions("motorbike")] == [moto["id"]]
    assert [r["id"] for r in store.active_restrictions("truck")] == [narrow["id"]]


def test_restriction_expiry(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    flood = store.create_restriction({"kind": "polygon", "reason": "flood",
                                      "geometry": {"kind": "polygon",
                                                   "coordinates": [[-7.186, 107.7225],
                                                                   [-7.186, 107.74375],
                                                                   [-7.214, 107.74375],
                                                                   [-7.214, 107.7225]]},
                                      "status": "verified",
                                      "expires_at": add_minutes(now_iso(T0), 30)})
    stale_pending = store.create_restriction({"kind": "point", "reason": "blocked",
                                              "geometry": {"kind": "point", "coordinates": [[-7.12, 107.7]]},
                                              "expires_at": add_minutes(now_iso(T0), 5)})
    assert [r["id"] for r in store.active_restrictions("truck")] == [flood["id"]]
    advance(31)
    changed = store.expire_overdue()
    assert [r["id"] for r in changed] == [flood["id"]]
    assert store.restriction(flood["id"])["status"] == "expired"
    assert store.active_restrictions("truck") == []
    # pending rows past their expiry are left alone until verification
    assert store.restriction(stale_pending["id"])["status"] == "pending"
    assert store.expire_overdue() == []


# ---- stats ----

def test_stats_today(tmp_path):
    store, advance = make_store(tmp_path / "db.sqlite")
    team_member(store, vehicle_id="v1")
    store.record_ping("A", -7.15, 107.70, vehicle_id="v1")
    advance(1)
    store.record_ping("A", -7.16, 107.71, vehicle_id="v1")
    active = store.create_incident({"type": "Kebakaran ruko", "lat": -7.1, "lon": 107.77})
    done = store.create_incident({"type": "Kebakaran dapur", "lat": -7.15, "lon": 107.75})
    base = store.now_iso()
    store.update_incident(done["id"], {"status": "Selesai",
                                       "dispatched_at": add_minutes(base, 2),
                                       "arrived_at": add_minutes(base, 10),
                                       "finished_at": add_minutes(base, 12)})
    store.record_water_level("v1", 2, "operator")
    store.refill_done("v1")

    stats = store.stats_today()
    assert stats["date"] == "2026-11-19"
    assert stats["incidents_active"] == 1  # the finished one left 'active'
    assert stats["incidents_finished"] == 1
    assert stats["avg_response_minutes"] == pytest.approx(10.0)
    assert stats["km_per_vehicle"] == {"v1": store.km_today("v1")}
    assert stats["km_total"] == pytest.approx(stats["km_per_vehicle"]["v1"])
    assert stats["refills"] == 1
    assert stats["weather"] is None
    assert store.stats_today(YESTERDAY)["incidents_finished"] == 0
    assert store.stats_today(YESTERDAY)["avg_response_minutes"] is None

    warm = make_store(tmp_path / "warm.sqlite",
                      Config.load(CONFIG_PATH).replaced("stats.weather_enabled", True))[0]
    assert warm.stats_today()["weather"] == {"source": None}


def test_stats_avg_none_without_finished_incidents(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    assert store.stats_today()["avg_response_minutes"] is None


# ---- snapshot ----

def test_snapshot_shape(tmp_path):
    store, _ = seeded_store(tmp_path / "db.sqlite")
    snap = store.snapshot()
    assert set(snap) == {"server_ts", "last_event_id", "config", "stations", "teams", "members",
                         "vehicles", "water_sources", "incidents", "assignments",
                         "road_restrictions", "reporter_links", "event_log", "stats_today"}
    assert snap["server_ts"] == now_iso(T0) and snap["last_event_id"] is None
    assert snap["config"] == Config.load(CONFIG_PATH).dump()
    assert snap["stations"][0]["id"] == "st1"
    assert snap["teams"][0]["id"] == "t1"
    for m in snap["members"]:
        assert set(m) <= set(_MEMBER_KEYS)
        assert m["freshness"] == "offline" and m["position"] is None
    for v in snap["vehicles"]:
        assert set(v) <= set(_VEHICLE_KEYS)
        assert v["water_level"] == 0 and v["water_level_source"] == "estimate"
        assert v["estimated"] is True and v["km_today"] == 0.0
        assert v["position"] is not None and "tank_capacity_l" not in v
    for inc in snap["incidents"]:
        assert set(inc) == set(_INCIDENT_KEYS) | {"location"}
        assert inc["id"] in {"I-25", "I-27"} and "lat" not in inc
    assert {i["id"]: i for i in snap["incidents"]}["I-25"]["location"] == {"lat": -7.34, "lon": 108.235}
    assert len(snap["assignments"]) == 3
    assert all(a["status"] == "Completed" for a in snap["assignments"])
    assert all(set(a) <= {"id", "incident_id", "member_id", "vehicle_id", "water_source_id",
                          "role", "status"} for a in snap["assignments"])
    by_id = {r["id"]: r for r in snap["road_restrictions"]}
    assert by_id["r1"]["geometry"]["kind"] == "polygon"
    assert len(by_id["r1"]["geometry"]["coordinates"]) == 4
    w0 = _seed("water_sources")[0]
    assert snap["water_sources"][0]["location"] == {"lat": w0["lat"], "lon": w0["lon"]}
    assert snap["water_sources"][0]["access_points"] == [
        {"lat": w0["access_points"][0]["lat"], "lon": w0["access_points"][0]["lon"]}]
    assert snap["reporter_links"] == [] and snap["event_log"] == []
    assert snap["stats_today"]["date"] == "2026-11-19"


# ---- events & WS buffer ----

def test_record_event_monotonic_and_events_since(tmp_path):
    db = tmp_path / "db.sqlite"
    store, _ = make_store(db, limit=2)
    envelopes = [store.record_event("incident.created", {"incident": {"id": f"I-{n}"}},
                                    f"Insiden baru I-{n}.") for n in range(1, 4)]
    assert all(e["v"] == 1 and set(e) == {"id", "type", "ts", "v", "payload"} for e in envelopes)
    assert [e["id"] for e in envelopes] == ["ev_1", "ev_2", "ev_3"]
    assert store.last_event_id == "ev_3"
    assert store.events_since("ev_2") == [envelopes[2]]
    assert store.events_since("ev_1") is None  # ev_1 was evicted: gap needs a snapshot
    assert store.events_since(None) == [envelopes[1], envelopes[2]]
    assert store.event_log()[0]["type"] == "incident.created"
    assert store.event_log()[0]["payload"] == {"incident": {"id": "I-3"}}

    reopened, _ = make_store(db)
    again = reopened.record_event("config.updated", {"key": "routing.provider", "value": "estimate"},
                                  "Rute diganti ke estimate.")
    assert again["id"] == "ev_4"  # sequence continues from the persisted log
    assert reopened.events_since("ev_3") is None  # fresh process, empty buffer


# ---- config mirror & accounts ----

def test_sync_config_tables(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    store.sync_config_tables()
    values = [r["value"] for r in store._rows(
        "SELECT value FROM config_enum WHERE group_name = ? ORDER BY sort", ("member_status",))]
    assert "Standby" in values and "IsiUlang" in values and len(values) == 8
    levels = [r["value"] for r in store._rows(
        "SELECT value FROM config_enum WHERE group_name = ? ORDER BY sort", ("water_level",))]
    assert levels == ["0", "1", "2", "3", "4"]
    key = store._row("SELECT value FROM config_key WHERE key = ?", ("routing.provider",))
    assert json.loads(key["value"]) == "valhalla"
    stale = store._row("SELECT value FROM config_key WHERE key = ?", ("position.stale_after_seconds",))
    assert json.loads(stale["value"]) == 300


def test_account_duplicate_username(tmp_path):
    store, _ = make_store(tmp_path / "db.sqlite")
    account = store.create_account("op1", "operator", "secret", member_id=None)
    assert account["role"] == "operator" and account["id"].startswith("acct_")
    assert store.authenticate("op1", "secret")["username"] == "op1"
    assert store.authenticate("op1", "typo") is None
    assert store.account_by_username("nobody") is None
    with pytest.raises(Conflict):
        store.create_account("op1", "operator", "other")
    assert store.account_by_username("op1")["password_hash"] == _sha256("secret")
