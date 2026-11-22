#!/usr/bin/env bash
# Build the self-hosted vector basemap as a single PMTiles archive (docs/siren.md
# stack table: self-hosted vector tiles). Pipeline: protomaps daily planet build
# (https://build.protomaps.com) -> `pmtiles extract --bbox` (go-pmtiles binary in
# .sandbox/bin; range requests only, no planet download). The bbox comes from
# contracts/config.yaml geodata, never hard-coded here. Output stays in
# .sandbox/data/tiles/ and is never committed (docs/rules.md §sandboxing).
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root, wherever the script is invoked from

OUT=".sandbox/data/tiles/siren.pmtiles"
mkdir -p .sandbox/data/tiles

# Coverage bbox from contracts/config.yaml (min_lon,min_lat,max_lon,max_lat —
# pmtiles extract wants lon,lat order).
BBOX="$(.sandbox/py/bin/python - <<'EOF'
import sys
sys.path.insert(0, "backend")
from siren.config import Config
b = Config.load("contracts/config.yaml").get("geodata.bbox")
print(f"{b['min_lon']},{b['min_lat']},{b['max_lon']},{b['max_lat']}")
EOF
)"

# Newest daily build, probing back a few days for availability.
BUILD="${PROTOMAPS_BUILD:-}"
if [ -z "$BUILD" ]; then
  for i in 0 1 2 3 4 5 6; do
    d=$(date -d "-$i day" +%Y%m%d)
    if curl -sfI "https://build.protomaps.com/$d.pmtiles" >/dev/null 2>&1; then
      BUILD="https://build.protomaps.com/$d.pmtiles"
      break
    fi
  done
fi
if [ -z "$BUILD" ]; then
  echo "No recent protomaps daily build found" >&2
  exit 1
fi

echo "Extracting bbox $BBOX from $BUILD (zoom 0-14) ..."
nice -n 10 .sandbox/bin/pmtiles extract "$BUILD" "$OUT" \
  --bbox="$BBOX" --maxzoom=14 --download-threads=4

echo "Done: $OUT ($(stat -c %s "$OUT") bytes)"
