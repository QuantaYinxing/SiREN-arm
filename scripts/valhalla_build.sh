#!/usr/bin/env bash
# Build Valhalla routing tiles from the bbox crop (scripts/osm_crop.py; bbox comes
# from contracts/config.yaml geodata). Valhalla is already compiled by Candra into
# .sandbox/bin (commit ac46322637801a3ea7735cf1ba63f7928a9314c4); prime_server's lib
# lives in .sandbox/lib, so LD_LIBRARY_PATH must be exported before running them.
# Everything stays inside .sandbox/ (docs/rules.md §sandboxing); generated data is
# never committed.
#
# Service binary note: the .sandbox/bin/valhalla_service build links system protobuf
# 36 and SIGSEGVs on repeat /route requests (Valhalla 3.9.3 is not compatible with
# protobuf 36). The stable binary is .sandbox/bin/valhalla_service.pb321, rebuilt from
# the same source with protobuf 3.21.12 (upstream's Ubuntu 24.04 pin) installed into
# .sandbox/deps-pb and passed via -DCMAKE_PREFIX_PATH plus -DCMAKE_CXX_FLAGS
# "-I$PWD/.sandbox/deps-pb/include"; setup.sh prefers it. Details: docs/blackbox.md
# 2026-11-23.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root, wherever the script is invoked from

BIN=".sandbox/bin"
export LD_LIBRARY_PATH="$PWD/.sandbox/lib"
CROP=".sandbox/data/osm/siren-bbox.osm.pbf"
TILES_DIR=".sandbox/data/valhalla"
CONFIG=".sandbox/data/valhalla/valhalla.json"

# Explicit thread cap; never all cores (docs/rules.md heavy-build rules).
THREADS="${THREADS:-4}"

mkdir -p .sandbox/data/osm "$TILES_DIR"

if [ ! -f "$CROP" ]; then
  echo "Cropping extract (bbox from contracts/config.yaml) ..."
  nice -n 10 .sandbox/py/bin/python scripts/osm_crop.py
fi

# Config skeleton with the tile extract path and listen port (Valhalla's default
# is 8002; contracts/config.yaml routing.valhalla_url expects 8081). The routing
# section (truck/motorcycle/auto costing) comes from infra/valhalla.json.
"$BIN/valhalla_build_config" \
  --mjolnir-tile-dir "$PWD/$TILES_DIR" \
  --mjolnir-tile-extract "$PWD/$TILES_DIR/valhalla_tiles.tar" \
  --httpd-service-listen "tcp://127.0.0.1:8081" -o "$CONFIG"
ROOT="$PWD"
python3 - "$CONFIG" infra/valhalla.json "$PWD/$TILES_DIR" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
base = json.load(open(sys.argv[2]))
cfg["routing"] = base["routing"]
# Default build_config id_table_size (1.3e9) reserves ~10 GB of edge ids — sized for
# all of Indonesia; the bbox crop graph is far smaller and the reservation segfaults
# on laptops under RAM pressure.
cfg["mjolnir"]["id_table_size"] = 20000000
# Default build_config points admin/timezone DBs at /data (the container path);
# the dev build keeps them next to the tiles inside the sandbox.
cfg["mjolnir"]["admin"] = sys.argv[3] + "/admin.sqlite"
cfg["mjolnir"]["timezone"] = sys.argv[3] + "/tz_world.sqlite"
# Verified flood polygons near the HQ run ~11 km around; the default 10 km
# exclude-polygon limit would reject them.
cfg["service_limits"]["max_exclude_polygons_length"] = 30000
# routing.router ships with /valhalla/tiles paths; Thor's polygon avoids open
# the spatial index sqlite there and segfault on a missing file.
cfg["routing"]["router"]["tile_extract"] = sys.argv[3] + "/valhalla_tiles.tar"
cfg["routing"]["router"]["spatial_index"]["sqlite_file"] = sys.argv[3] + "/valhalla_tiles.spatial.idx.sqlite"
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
EOF

# Valhalla 3.9 builds the admin SQLite from the OSM extract itself (not from
# shapefiles), and routing uses it during graph build, so it must run first.
nice -n 10 "$BIN/valhalla_build_admins" -c "$CONFIG" "$CROP"

# The timezone DB is optional for this area; the tool downloads its own
# shapefile archive, so run it from the sandbox data dir to keep files out of
# the repo.
if [ ! -f "$PWD/$TILES_DIR/tz_world.sqlite" ]; then
  (cd "$TILES_DIR" && nice -n 10 "$ROOT/$BIN/valhalla_build_timezones" -c "$ROOT/$CONFIG") || true
fi

nice -n 10 "$BIN/valhalla_build_tiles" -c "$CONFIG" -j "$THREADS" "$CROP"

# Pack the tile directory into the single tar extract the Valhalla service reads.
nice -n 10 "$BIN/valhalla_build_extract" -c "$CONFIG" -O \
  -e "$TILES_DIR/valhalla_tiles.tar"

echo "Done: $TILES_DIR/valhalla_tiles.tar ($(stat -c %s "$TILES_DIR/valhalla_tiles.tar") bytes)"
echo "Serve: LD_LIBRARY_PATH=$PWD/.sandbox/lib $BIN/valhalla_service $CONFIG 1"
