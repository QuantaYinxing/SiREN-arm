#!/usr/bin/env python
"""Re-seed seed/water_sources.json from OSM inside the coverage bbox.

Scans the cropped extract (scripts/osm_crop.py output) for fire hydrants, water
bodies, rivers, and tanks/towers, and maps them onto the config enum
water_source_types. Every entry is marked unverified: OSM data is a placeholder
until the brigade surveys the point (docs/next-real-maps_done.md step 2).

Usage: .sandbox/py/bin/python scripts/water_seed.py [crop.pbf] [out.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import osmium

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from siren.config import Config

NOTE = "OSM placeholder, needs field survey"

POND_NAMES = {"lake": "Danau", "basin": "Kolam", "pond": "Kolam", "reservoir": "Waduk"}


def bbox_from_config() -> tuple[float, float, float, float]:
    cfg = Config.load(REPO / "contracts" / "config.yaml")
    b = cfg.get("geodata.bbox")
    return (float(b["min_lat"]), float(b["max_lat"]), float(b["min_lon"]), float(b["max_lon"]))


class WaterScan(osmium.SimpleHandler):
    """One pass over the crop. pyosmium 4.x does not populate NodeRef
    locations, so way centroids are computed after the pass from the node
    positions collected during it (the crop is self-consistent: every way
    reference has its node in the file)."""

    def __init__(self, bbox: tuple[float, float, float, float]):
        super().__init__()
        self.lo_lat, self.hi_lat, self.lo_lon, self.hi_lon = bbox
        self.nodes: dict[int, tuple[float, float]] = {}
        self.found: list[dict] = []
        self.pending: list[tuple[str, str, list[int]]] = []

    def _inside(self, lat: float, lon: float) -> bool:
        return self.lo_lat <= lat <= self.hi_lat and self.lo_lon <= lon <= self.hi_lon

    def node(self, n):
        loc = (n.location.lat, n.location.lon)
        self.nodes[n.id] = loc
        if n.tags.get("fire_hydrant") == "yes" and self._inside(*loc):
            self.found.append({"type": "hydrant", "name": n.tags.get("name") or "Hidran",
                               "lat": loc[0], "lon": loc[1]})

    def way(self, w):
        t = w.tags
        kind = None
        if t.get("fire_hydrant") == "yes":
            kind = ("hydrant", t.get("name") or "Hidran")
        elif t.get("natural") == "water":
            water = t.get("water", "pond")
            kind = ("pond", t.get("name") or POND_NAMES.get(water, "Kolam"))
        elif t.get("waterway") in ("river", "riverbank", "stream"):
            kind = ("river", t.get("name") or "Sungai")
        elif t.get("man_made") == "water_tower" or t.get("reservoir") == "yes":
            kind = ("tank", t.get("name") or "Tandon")
        elif t.get("amenity") == "drinking_water":
            kind = ("well", t.get("name") or "Sumur")
        if kind is None:
            return
        self.pending.append((kind[0], kind[1], [int(n.ref) for n in w.nodes]))

    def centroids(self) -> None:
        for kind, name, refs in self.pending:
            pts = [self.nodes[r] for r in refs if r in self.nodes]
            if not pts:
                continue
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            if not self._inside(lat, lon):
                continue
            self.found.append({"type": kind, "name": name, "lat": lat, "lon": lon})


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else REPO / ".sandbox/data/osm/siren-bbox.osm.pbf")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else REPO / "seed" / "water_sources.json")

    scan = WaterScan(bbox_from_config())
    scan.apply_file(str(src))
    scan.centroids()

    # Deduplicate near-identical points (< ~100 m apart keep the first), cap rivers.
    kept: list[dict] = []
    for f in scan.found:
        if any(abs(f["lat"] - k["lat"]) < 0.001 and abs(f["lon"] - k["lon"]) < 0.001 for k in kept):
            continue
        kept.append(f)
    rivers = [f for f in kept if f["type"] == "river"]
    if len(rivers) > 6:
        kept = [f for f in kept if f["type"] != "river"] + rivers[:6]

    out = []
    for i, f in enumerate(kept, start=1):
        out.append({
            "id": f"w{i}",
            "name": f["name"],
            "type": f["type"],
            "status": "active",
            "lat": round(f["lat"], 6),
            "lon": round(f["lon"], 6),
            "capacity_note": None,
            "notes": NOTE,
            "access_points": [{"lat": round(f["lat"], 6), "lon": round(f["lon"], 6), "note": NOTE}],
        })
    dst.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"found {len(scan.found)} water features, wrote {len(out)} sources -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
