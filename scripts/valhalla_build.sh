#!/usr/bin/env bash
# Build Valhalla routing tiles from the bbox crop (scripts/osm_crop.py; bbox comes
# from contracts/config.yaml geodata). The binaries come from .sandbox/bin - build
# them for the local arch with scripts/valhalla_compile.sh (the author's x86 builds
# were never committed; nothing runs on a fresh clone until you compile). prime_server's
# lib lives in .sandbox/lib, so LD_LIBRARY_PATH must be exported before running them.
# Everything stays inside .sandbox/ (docs/rules.md §sandboxing); generated data is
# never committed.
#
# Service binary note: on the author's x86 laptop the system protobuf 36 SIGSEGVs
# Valhalla 3.9.3 on repeat /route requests, so .sandbox/bin/valhalla_service.pb321
# (protobuf 3.21.12 via .sandbox/deps-pb) was rebuilt there. On arm64 (Ubuntu 26.10)
# the system protobuf IS 3.21.12, so the plain binary compiled by valhalla_compile.sh
# is stable and no pb321 exists. setup.sh prefers pb321 and falls back to plain.
# Details: docs/blackbox.md 2026-11-23.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root, wherever the script is invoked from

BIN=".sandbox/bin"
export LD_LIBRARY_PATH="$PWD/.sandbox/lib"
# build_admins/build_timezones invoke 'spatialite_tool' via the shell (Ubuntu 26.10
# ships no spatialite-tools; valhalla_compile.sh builds it into .sandbox/bin).
export PATH="$PWD/.sandbox/bin:$PATH"
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
# Paths stay RELATIVE to the repo root: setup.sh and scripts/valhalla_keep.sh
# cd there before starting valhalla_service, so the config survives a repo move
# (absolute paths broke routing with "Tile extract could not be loaded" after
# the tree moved from ~/SiREN to ~/Projects/SiREN).
"$BIN/valhalla_build_config" \
  --mjolnir-tile-dir "$TILES_DIR" \
  --mjolnir-tile-extract "$TILES_DIR/valhalla_tiles.tar" \
  --httpd-service-listen "tcp://127.0.0.1:8081" -o "$CONFIG"
ROOT="$PWD"
python3 - "$CONFIG" infra/valhalla.json "$TILES_DIR" "$PWD/$TILES_DIR" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
base = json.load(open(sys.argv[2]))
cfg["routing"] = base["routing"]
# Default build_config id_table_size (1.3e9) reserves ~10 GB of edge ids — sized for
# all of Indonesia; the bbox crop graph is far smaller and the reservation segfaults
# on laptops under RAM pressure.
cfg["mjolnir"]["id_table_size"] = 20000000
# Default build_config points admin/timezone DBs at /data (the container path);
# the dev build keeps them next to the tiles inside the sandbox. admin stays
# repo-relative (read from the repo root); timezone stays absolute because
# valhalla_build_timezones runs from the tiles dir, where relative paths would
# resolve against the wrong cwd. tz is optional for this area — a moved repo
# just loses it with a startup warning, routing does not care.
cfg["mjolnir"]["admin"] = sys.argv[3] + "/admin.sqlite"
cfg["mjolnir"]["timezone"] = sys.argv[4] + "/tz_world.sqlite"
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
# Drop any stale tar first and skip -e: with -e pointing at mjolnir.tile_extract,
# the resolver reads THAT tar as its tile source (not tile_dir) and writes into
# the same file it is reading — a rebuild repacked the old graph and died on a
# truncated read instead of shipping the freshly built tiles.
rm -f "$TILES_DIR/valhalla_tiles.tar"
nice -n 10 "$BIN/valhalla_build_extract" -c "$CONFIG" -O

echo "Done: $TILES_DIR/valhalla_tiles.tar ($(stat -c %s "$TILES_DIR/valhalla_tiles.tar") bytes)"
echo "Serve: LD_LIBRARY_PATH=$PWD/.sandbox/lib $BIN/valhalla_service $CONFIG 1"
