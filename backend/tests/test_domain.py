"""Domain tests: transitions, cascades, restriction application, routing fallback,
water-source ranking, pump estimate. Deterministic coordinates + injected clock; no DB,
no API, no network.
"""

from __future__ import annotations

import pytest

from fake_store import FakeStore
from siren.config import Config, ConfigError
from siren.geo import buffer_line, haversine_km, point_in_ring
from siren.restrictions import crossing, selection, valhalla_exclusions
from siren.routing import build_route, costing_for, estimate_leg, water_options
from siren.state import RELEASE_STATUSES, TransitionError, apply_member_status, check, recompute_incident
from siren.timeutil import add_minutes
from siren.water import estimated_level, tick

CFG = Config.load()

V1 = (-7.154, 107.7025)
STATION = (-7.218, 107.72)
INC = (-7.102, 107.77)
W1 = (-7.13, 107.75)
W2 = (-7.182, 107.7925)
W3 = (-7.13, 107.7025)
W4 = (-7.182, 107.6625)
FLOOD = [[-7.186, 107.7225], [-7.186, 107.74375], [-7.214, 107.74375], [-7.214, 107.7225]]


def base_store() -> FakeStore:
    s = FakeStore(CFG)
    s.add_station(*STATION)
    s.add_profile("vp_truck", vehicle_type="truck", height_m=3.5, width_m=2.5, length_m=11.5,
                  gross_weight_kg=16000, axle_load_kg=10000,
                  valhalla_costing={"type": "truck", "use_osm_trucking_on_road": True})
    s.add_vehicle("v1", team_id="t1", profile_id="vp_truck", lat=V1[0], lon=V1[1],
                  pump_flow_lpm=800, tank_capacity_l=4000)
    s.add_vehicle("v2", team_id="t2", profile_id="vp_truck", lat=STATION[0], lon=STATION[1],
                  pump_flow_lpm=800, tank_capacity_l=4000)
    s.add_member("A", team_id="t1", vehicle_id="v1", name="Asep")
    s.add_member("B", team_id="t1", vehicle_id="v1", name="Budi")
    s.add_member("D", team_id="t2", vehicle_id="v2", name="Dede")
    s.add_incident("I-1", *INC)
    s.add_water_source("w1", *W1, capacity_note="± 600 L/mnt")
    s.add_water_source("w2", *W2, type="river")
    s.add_water_source("w3", *W3, type="pond")
    s.add_water_source("w4", *W4, type="tank", status="dry")
    s.add_restriction("r1", "polygon", FLOOD, reason="flood", status="verified")
    s.add_restriction("r2", "point", [[-7.13, 107.6625]], reason="narrow", status="pending",
                      reported_by="B")
    return s


# ---- transitions ----

def test_check_rejects_unknown_transition_with_allowed_list():
    with pytest.raises(TransitionError) as exc:
        check(CFG, "member", "Standby", "OnScene")
    assert exc.value.allowed == CFG.allowed_transitions("member", "Standby")
    assert exc.value.allowed == ["Dispatched", "Istirahat", "Offline"]


def test_check_same_status_is_noop_and_values_come_from_config():
    assert check(CFG, "incident", "EnRoute", "EnRoute") == "EnRoute"
    assert CFG.can_transition("incident", "Baru", "Dibatalkan")
    assert CFG.enum_values("water_source_status") == ["active", "dry", "broken", "inactive"]


def test_transitions_are_editable_data_not_code():
    cfg = CFG.replaced("transitions.member.EnRoute", ["OnScene"])
    assert cfg.can_transition("member", "EnRoute", "OnScene")
    assert not cfg.can_transition("member", "EnRoute", "Returning")
    with pytest.raises(TransitionError):
        check(cfg, "member", "EnRoute", "Returning")


def test_check_rejects_status_outside_enum():
    with pytest.raises(ConfigError) as exc:
        check(CFG, "member", "Standby", "Ngeredas")
    assert "Ngeredas" not in exc.value.allowed


# ---- cascades ----

def test_dispatch_to_scene_cascades_assignment_vehicle_and_incident():
    s = base_store()
    s.assign("I-1", "A", "v1")
    s.assign("I-1", "B", "v1")

    r1 = apply_member_status(CFG, s, "A", "Dispatched")
    assert s.member("A")["status"] == "Dispatched"
    assert all(a["status"] == "Dispatched" for a in s.assignments("I-1"))
    assert s.incident("I-1")["status"] == "Dispatched"
    assert s.incident("I-1")["dispatched_at"] == s.now_iso()
    assert s.vehicle("v1")["status"] == "Dispatched"

    apply_member_status(CFG, s, "A", "EnRoute")
    assert s.incident("I-1")["status"] == "EnRoute"
    assert s.vehicle("v1")["status"] == "EnRoute"

    apply_member_status(CFG, s, "A", "IsiUlang")
    # IsiUlang is a sub-state of EnRoute: the incident stays EnRoute, the truck shows IsiUlang
    assert s.incident("I-1")["status"] == "EnRoute"
    assert s.vehicle("v1")["status"] == "IsiUlang"

    apply_member_status(CFG, s, "A", "OnScene")
    inc = s.incident("I-1")
    assert inc["status"] == "OnScene" and inc["arrived_at"] == s.now_iso()
    assert s.vehicle("v1")["status"] == "OnScene"
    assert [a["status"] for a in s.assignments("I-1")] == ["OnScene", "Dispatched"]


def test_release_withdraws_and_recomputes_incident_and_vehicle():
    s = base_store()
    s.assign("I-1", "A", "v1")
    apply_member_status(CFG, s, "A", "Dispatched")
    apply_member_status(CFG, s, "A", "EnRoute")
    apply_member_status(CFG, s, "A", "OnScene")
    out = apply_member_status(CFG, s, "A", "Returning")
    assert s.incident("I-1")["status"] == "Returning"
    assert s.vehicle("v1")["status"] == "Returning"
    assert "A" not in RELEASE_STATUSES

    out = apply_member_status(CFG, s, "A", "Standby")
    assert [a["status"] for a in s.assignments("I-1")] == ["Withdrawn"]
    assert s.incident("I-1")["status"] == "Selesai"  # it reached the scene, then everyone left
    assert s.incident("I-1")["finished_at"] == s.now_iso()
    v1 = s.vehicle("v1")
    assert v1["status"] == "Standby" and v1["eta_minutes"] is None and v1["lat"] is None


def test_release_before_scene_returns_incident_to_baru():
    s = base_store()
    s.assign("I-1", "A", "v1")
    apply_member_status(CFG, s, "A", "Dispatched")
    apply_member_status(CFG, s, "A", "Standby")
    assert s.incident("I-1")["status"] == "Baru"
    assert recompute_incident(CFG, s, "I-1") is None


def test_rest_leaves_assignment_withdrawn_but_member_not_standby():
    s = base_store()
    s.assign("I-1", "A", "v1")
    apply_member_status(CFG, s, "A", "Dispatched")
    apply_member_status(CFG, s, "A", "Standby")  # withdrawal returns to Standby first
    apply_member_status(CFG, s, "A", "Istirahat")
    assert s.member("A")["status"] == "Istirahat"
    assert s.assignments("I-1")[0]["status"] == "Withdrawn"


def test_split_across_two_incidents_keeps_vehicles_and_incidents_separate():
    s = base_store()
    s.add_incident("I-2", *W2)
    s.assign("I-1", "A", "v1")
    s.assign("I-2", "D", "v2")
    apply_member_status(CFG, s, "A", "Dispatched")
    apply_member_status(CFG, s, "A", "EnRoute")
    apply_member_status(CFG, s, "D", "Dispatched")
    apply_member_status(CFG, s, "D", "EnRoute")
    apply_member_status(CFG, s, "D", "OnScene")
    assert s.incident("I-1")["status"] == "EnRoute"
    assert s.incident("I-2")["status"] == "OnScene"
    assert s.vehicle("v1")["status"] == "EnRoute"
    assert s.vehicle("v2")["status"] == "OnScene"


def test_terminal_incident_is_not_recomputed():
    s = base_store()
    s.assign("I-1", "A", "v1")
    s.update_incident("I-1", {"status": "Dibatalkan"})
    assert recompute_incident(CFG, s, "I-1") is None


# ---- estimate fallback ----

def test_estimate_leg_matches_fallback_config():
    leg = estimate_leg(CFG, V1, INC)
    km = haversine_km(V1, INC) * float(CFG.get("routing.fallback.detour_factor"))
    assert leg["km"] == round(km, 1)
    assert leg["minutes"] == round(km / float(CFG.get("routing.fallback.avg_speed_kmh")) * 60, 1)
    assert leg["source"] == "estimate"
    assert leg["geometry"] == [[V1[0], V1[1]], [INC[0], INC[1]]]


def test_estimate_leg_respects_minimum_minutes():
    leg = estimate_leg(CFG, V1, (V1[0] + 0.0001, V1[1] + 0.0001))
    assert leg["minutes"] >= float(CFG.get("routing.fallback.min_minutes"))


# ---- restrictions ----

def test_selection_applies_only_verified_and_affecting_type():
    s = base_store()
    picked = selection(CFG, s, "truck")
    assert [r["id"] for r in picked] == ["r1"]
    assert selection(CFG, s, "motorbike") == []
    s.verify("r2", "verified")
    assert sorted(r["id"] for r in selection(CFG, s, "truck")) == ["r1", "r2"]


def test_selection_never_forces_an_unverified_restriction():
    s = base_store()
    assert selection(CFG, s, "truck", ids=["r2"]) == []


def test_selection_expires_overdue_restriction():
    s = base_store()
    s.add_restriction("r3", "polygon", FLOOD, reason="flood", status="verified",
                      expires_at=add_minutes(s.now_iso(), 30))
    assert "r3" in [r["id"] for r in selection(CFG, s, "truck")]
    s.advance(40)
    assert "r3" not in [r["id"] for r in selection(CFG, s, "truck")]
    assert [r["status"] for r in s.restrictions() if r["id"] == "r3"] == ["expired"]


def test_valhalla_exclusions_mapping():
    ex = valhalla_exclusions([
        {"geometry": {"kind": "point", "coordinates": [[-7.13, 107.6625]]}},
        {"geometry": {"kind": "polygon", "coordinates": FLOOD}},
        {"geometry": {"kind": "line", "coordinates": [[-7.15, 107.70], [-7.16, 107.72]]}},
    ])
    assert ex["exclude_locations"] == [{"lat": -7.13, "lon": 107.6625}]
    assert len(ex["exclude_polygons"]) == 2
    assert ex["exclude_polygons"][0] == FLOOD
    buf = ex["exclude_polygons"][1]
    assert len(buf) == 4 and all(len(p) == 2 for p in buf)


def test_estimate_route_warns_when_leg_crosses_verified_restriction():
    s = base_store()
    route = build_route(CFG, s, "v2", {"lat": INC[0], "lon": INC[1]}, provider="estimate")
    assert route["applied_restrictions"] == ["r1"]
    assert any("r1" in w for w in route["warnings"])
    penalty = float(CFG.get("routing.fallback.blocked_penalty"))
    plain = estimate_leg(CFG, STATION, INC)
    assert route["legs"][0]["minutes"] == round(plain["minutes"] * penalty, 1)


def test_estimate_route_without_restriction_has_no_warning():
    s = base_store()
    route = build_route(CFG, s, "v1", {"lat": W3[0], "lon": W3[1]}, provider="estimate")
    assert route["warnings"] == []  # the leg does not cross the flood polygon
    assert route["applied_restrictions"] == ["r1"]  # still excluded from routing
    assert route["provider"] == "estimate"


def test_route_via_water_source_produces_two_legs_and_folds_refill():
    s = base_store()
    s.record_water_level("v1", 1, "officer")
    route = build_route(CFG, s, "v1", {"lat": INC[0], "lon": INC[1]},
                        via_water_source_id="w1", provider="estimate")
    assert len(route["legs"]) == 2
    assert route["legs"][0]["to"] == {"lat": W1[0], "lon": W1[1]}
    assert route["refill_minutes"] == float(CFG.get("water.refill_minutes"))
    assert route["total_minutes"] == round(
        route["legs"][0]["minutes"] + route["legs"][1]["minutes"] + route["refill_minutes"], 1
    )


def test_route_without_position_falls_back_to_station():
    s = base_store()
    s.update_vehicle("v1", {"lat": None, "lon": None})
    route = build_route(CFG, s, "v1", {"lat": INC[0], "lon": INC[1]}, provider="estimate")
    assert route["legs"][0]["from"] == {"lat": STATION[0], "lon": STATION[1]}


def test_profile_locks_costing_to_the_vehicle():
    s = base_store()
    name, options = costing_for(CFG, s.vehicle("v1"), s.vehicle_profile("vp_truck"))
    assert name == "truck"
    assert options["height"] == 3.5 and options["width"] == 2.5
    assert options["weight"] == 16000 and options["axle_load"] == 10000
    assert options["use_osm_trucking_on_road"] is True
    moto = s.add_vehicle("v3", team_id="t1", vehicle_type="motorbike", lat=V1[0], lon=V1[1])
    assert costing_for(CFG, moto, None)[0] == "motorcycle"


# ---- water-source ranking ----

def test_ranking_prefers_source_closest_to_scene_when_tank_full():
    s = base_store()
    s.record_water_level("v1", 4, "operator")
    out = water_options(CFG, s, "v1", "I-1")
    assert out["refill_required"] is False
    assert [o["water_source_id"] for o in out["options"]] == ["w1", "w3", "w2"]
    for o in out["options"]:
        assert o["score_minutes"] == o["b"]["minutes"]
        assert o["refill_minutes"] == 0.0
    assert out["options"][0]["rank"] == 1
    assert out["options"][0]["b"]["km"] <= out["options"][1]["b"]["km"]


def test_ranking_refill_first_when_tank_low():
    s = base_store()
    s.record_water_level("v1", 1, "officer")
    out = water_options(CFG, s, "v1", "I-1")
    assert out["refill_required"] is True
    refill = float(CFG.get("water.refill_minutes"))
    for o in out["options"]:
        assert o["score_minutes"] == round(o["a"]["minutes"] + refill + o["b"]["minutes"], 1)
    # low tank can flip the winner away from the source closest to the scene
    best = out["options"][0]
    assert best["refill_required"] is True


def test_ranking_limits_to_max_options_and_skips_dry_sources():
    s = base_store()
    s.add_water_source("w5", -7.12, 107.74, type="well")
    out = water_options(CFG, s, "v1", "I-1")
    assert len(out["options"]) == int(CFG.get("routing.max_options"))
    assert "w4" not in [o["water_source_id"] for o in out["options"]]


def test_ranking_reports_raw_numbers_for_transparency():
    s = base_store()
    out = water_options(CFG, s, "v1", "I-1")
    o = out["options"][0]
    assert o["a"]["km"] > 0 and o["b"]["km"] > 0 and o["name"] == "w1"
    assert o["provider"] == "estimate"


# ---- pump estimate ----

def test_pump_estimate_lowers_level_over_time():
    v = {"pump_flow_lpm": 800, "tank_capacity_l": 4000}
    # a quarter of the tank (1000 L) is 1.25 minutes of pumping; nothing drops before that
    assert estimated_level(CFG, v, 4, "2026-11-19T07:32:00Z", "2026-11-19T07:33:00Z") == 4
    assert estimated_level(CFG, v, 4, "2026-11-19T07:32:00Z", "2026-11-19T07:32:30Z") == 4
    assert estimated_level(CFG, v, 4, "2026-11-19T07:32:00Z", "2026-11-19T07:34:00Z") == 3
    assert estimated_level(CFG, v, 4, "2026-11-19T07:32:00Z", "2026-11-19T08:32:00Z") == CFG.min_level()


def test_pump_estimate_never_exceeds_last_confirmed_and_respects_toggle():
    v = {"pump_flow_lpm": 800, "tank_capacity_l": 4000}
    assert estimated_level(CFG, v, 2, "2026-11-19T07:32:00Z", "2026-11-19T07:33:00Z") <= 2
    off = CFG.replaced("water.auto_estimate", False)
    assert estimated_level(off, v, 4, "2026-11-19T07:32:00Z", "2026-11-19T09:00:00Z") == 4


def test_tick_records_estimate_only_for_pumping_vehicles():
    s = base_store()
    s.record_water_level("v1", 4, "operator")
    s.record_water_level("v2", 4, "operator")
    s.advance(2)
    s.update_vehicle("v1", {"status": "IsiUlang"})
    changed = tick(CFG, s)
    assert [c["id"] for c in changed] == ["v1"]
    assert s.water_state("v1")["level"] == 3  # 1600 L of 4000 L -> one quarter consumed
    assert s.water_state("v1")["source"] == "estimate"
    assert s.water_state("v2")["level"] == 4 and s.water_state("v2")["source"] == "operator"


# ---- geometry ----

def test_point_in_ring_and_blocks():
    assert point_in_ring(-7.20, 107.73, FLOOD)
    assert not point_in_ring(-7.15, 107.73, FLOOD)
    assert not point_in_ring(-7.20, 107.80, FLOOD)
    r = {"geometry": {"kind": "polygon", "coordinates": FLOOD}}
    from siren.geo import blocks

    assert blocks(r, -7.20, 107.73)
    assert not blocks(r, -7.15, 107.73)


def test_crossing_detects_restriction_on_a_leg_only():
    s = base_store()
    hits = crossing(s.active_restrictions("truck"), STATION, INC)
    assert [h["id"] for h in hits] == ["r1"]
    assert crossing(s.active_restrictions("truck"), V1, W3) == []


def test_buffer_line_encloses_the_line():
    buf = buffer_line([[-7.15, 107.70], [-7.16, 107.72]])
    assert point_in_ring(-7.155, 107.71, buf)
