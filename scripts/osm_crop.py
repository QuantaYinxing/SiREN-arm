#!/usr/bin/env python
"""Crop a Geofabrik extract to the coverage bbox from contracts/config.yaml.

Runs the compiled libosmium crop tool (scripts/osm_crop.cpp ->
.sandbox/bin/osm_bbox; compile command in that file's header). Same semantics
as the old pyosmium crop: keep ways that touch the bbox (any node inside),
relations whose member ways are kept, and every node referenced by kept
objects plus every node inside the bbox - self-consistent for Valhalla and
protomaps. The bbox lives in config (geodata.bbox), never in scripts.

Usage: .sandbox/py/bin/python scripts/osm_crop.py [in.pbf] [out.pbf]
Defaults: .sandbox/data/osm/indonesia-latest.osm.pbf -> .sandbox/data/osm/siren-bbox.osm.pbf
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from siren.config import Config


def bbox_from_config() -> tuple[float, float, float, float]:
    cfg = Config.load(REPO / "contracts" / "config.yaml")
    b = cfg.get("geodata.bbox")
    return (float(b["min_lat"]), float(b["max_lat"]), float(b["min_lon"]), float(b["max_lon"]))


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else REPO / ".sandbox/data/osm/indonesia-latest.osm.pbf")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else REPO / ".sandbox/data/osm/siren-bbox.osm.pbf")
    bbox = bbox_from_config()
    print(f"bbox lat [{bbox[0]}, {bbox[1]}] lon [{bbox[2]}, {bbox[3]}]")
    res = subprocess.run(
        [str(REPO / ".sandbox/bin/osm_bbox"), str(src), str(dst),
         str(bbox[0]), str(bbox[1]), str(bbox[2]), str(bbox[3])],
    )
    if res.returncode != 0:
        return res.returncode
    print(f"out: {dst} ({dst.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
