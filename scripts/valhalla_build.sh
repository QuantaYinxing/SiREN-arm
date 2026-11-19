#!/usr/bin/env bash
# Build Valhalla routing tiles from a Geofabrik OSM extract (docs/siren.md D1).
# Everything stays inside .sandbox/ (docs/rules.md §sandboxing); generated data is
# never committed.
#
# Requires the Valhalla binaries (valhalla_build_tiles, valhalla_convert) on PATH.
# docker/podman are absent on this laptop, so run it on a machine with Valhalla
# installed or inside the valhalla compose container (infra/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root, wherever the script is invoked from

# Geofabrik Indonesia extract (~1 GB pbf). For a first laptop-sized build prefer
# the Jawa extract instead — roughly 10x smaller and it covers the Jawa region
# SiREN needs first:
#   OSM_URL=https://download.geofabrik.de/asia/indonesia-jawa-latest.osm.pbf ./scripts/valhalla_build.sh
OSM_URL="${OSM_URL:-https://download.geofabrik.de/asia/indonesia-latest.osm.pbf}"
OSM_FILE=".sandbox/data/osm/$(basename "$OSM_URL")"
TILES_DIR=".sandbox/data/valhalla"

# Explicit thread cap; never all cores (docs/rules.md heavy-build rules).
THREADS="${THREADS:-4}"

mkdir -p .sandbox/data/osm "$TILES_DIR"

if [ ! -f "$OSM_FILE" ]; then
  echo "Downloading $OSM_URL ..."
  curl -L --fail -o "$OSM_FILE.part" "$OSM_URL"
  # Rename only on success, so an interrupted download is never treated as done.
  mv "$OSM_FILE.part" "$OSM_FILE"
fi

if ! command -v valhalla_build_tiles >/dev/null 2>&1; then
  echo "valhalla_build_tiles not found on PATH; see infra/README.md" >&2
  exit 1
fi

# Tile extraction is CPU-heavy: nice -n 10 plus the thread cap keep the local
# inference engine usable (its model holds nearly all RAM on this laptop).
nice -n 10 valhalla_build_tiles --threads "$THREADS" -o "$TILES_DIR" "$OSM_FILE"

# Pack the tile directory into the single tar extract the Valhalla service reads;
# infra/valhalla.json points at /valhalla/tiles/valhalla_tiles.tar in the container.
nice -n 10 valhalla_convert --build_tiles --threads "$THREADS" \
  --input "$TILES_DIR" --output "$TILES_DIR/valhalla_tiles.tar"

echo "Done: $TILES_DIR/valhalla_tiles.tar"
