"""Coverage bbox (contracts/config.yaml geodata) and seed loader against it."""
import json
import pathlib
import tempfile

from siren.config import Config
from siren.geo import bbox_from_station
from siren.store import Store

REPO = pathlib.Path(__file__).resolve().parents[2]
CFG = REPO / "contracts" / "config.yaml"
SEED = REPO / "seed"


def test_bbox_is_station_plus_radius():
    cfg = Config.load(CFG)
    st = cfg.get("geodata.station")
    r = float(cfg.get("geodata.radius_km"))
    lo_lat, hi_lat, lo_lon, hi_lon = bbox_from_station(st["lat"], st["lon"], r)
    b = cfg.get("geodata.bbox")
    assert abs(b["min_lat"] - lo_lat) < 1e-5
    assert abs(b["max_lat"] - hi_lat) < 1e-5
    assert abs(b["min_lon"] - lo_lon) < 1e-5
    assert abs(b["max_lon"] - hi_lon) < 1e-5


def test_bbox_contains_station():
    cfg = Config.load(CFG)
    st = cfg.get("geodata.station")
    b = cfg.get("geodata.bbox")
    assert b["min_lat"] <= st["lat"] <= b["max_lat"]
    assert b["min_lon"] <= st["lon"] <= b["max_lon"]


def test_seed_station_is_the_real_hq():
    st = json.loads((SEED / "station.json").read_text())
    cfg = Config.load(CFG)
    assert st["lat"] == cfg.get("geodata.station.lat")
    assert st["lon"] == cfg.get("geodata.station.lon")


def test_seed_positions_inside_bbox():
    cfg = Config.load(CFG)
    b = cfg.get("geodata.bbox")
    inside = lambda lat, lon: b["min_lat"] <= lat <= b["max_lat"] and b["min_lon"] <= lon <= b["max_lon"]

    for v in json.loads((SEED / "vehicles.json").read_text()):
        assert inside(v["lat"], v["lon"]), v["id"]
    for w in json.loads((SEED / "water_sources.json").read_text()):
        assert inside(w["lat"], w["lon"]), w["id"]
    for inc in json.loads((SEED / "incidents.json").read_text()):
        assert inside(inc["lat"], inc["lon"]), inc["id"]
    for r in json.loads((SEED / "road_restrictions.json").read_text()):
        assert all(inside(lat, lon) for lat, lon in r["geometry"]["coordinates"]), r["id"]


def test_seed_water_sources_are_marked_unverified():
    sources = json.loads((SEED / "water_sources.json").read_text())
    assert sources, "water seed must not be empty"
    for w in sources:
        assert w["notes"] == "OSM placeholder, needs field survey", w["id"]
        for ap in w.get("access_points") or []:
            assert ap.get("note") == "OSM placeholder, needs field survey", w["id"]


def test_seed_vehicle_dims_are_flagged_placeholders():
    profiles = json.loads((SEED / "vehicle_profiles.json").read_text())
    truck = next(p for p in profiles if p["vehicle_type"] == "truck")
    assert "ASSUMPTION" in truck["//"]
    assert (truck["width_m"], truck["height_m"], truck["length_m"]) == (2.5, 3.2, 8.0)
    assert truck["gross_weight_kg"] == 12000


def test_load_seed_stores_real_station():
    cfg = Config.load(CFG)
    with tempfile.TemporaryDirectory() as d:
        store = Store(pathlib.Path(d) / "s.sqlite", cfg)
        store.init_schema()
        counts = store.load_seed(SEED)
        assert counts["station"] == 1
        st = store.stations()[0]
        assert st["lat"] == cfg.get("geodata.station.lat")
        assert st["name"] == "Pos Damkar Tasikmalaya"
        assert counts["water_sources"] >= 1
        assert counts["vehicles"] == 2
