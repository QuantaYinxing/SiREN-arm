#!/usr/bin/env bash
# Build self-hosted vector tiles as a single PMTiles archive (docs/siren.md stack
# table: self-hosted vector tiles). Uses the same Geofabrik input file as
# scripts/valhalla_build.sh; output lives in .sandbox/data/tiles/ and is never
# committed (docs/rules.md §sandboxing).
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root, wherever the script is invoked from

OSM_URL="${OSM_URL:-https://download.geofabrik.de/asia/indonesia-latest.osm.pbf}"
OSM_FILE=".sandbox/data/osm/$(basename "$OSM_URL")"
OUT=".sandbox/data/tiles/siren.pmtiles"

mkdir -p .sandbox/data/osm .sandbox/data/tiles

if [ ! -f "$OSM_FILE" ]; then
  echo "Downloading $OSM_URL ..."
  curl -L --fail -o "$OSM_FILE.part" "$OSM_URL"
  # Rename only on success, so an interrupted download is never treated as done.
  mv "$OSM_FILE.part" "$OSM_FILE"
fi

# Tile server choice: TileServer GL (the `tiles` service in docker-compose.yml)
# serves this directory of PMTiles. PMTiles is a single-file archive with range
# requests, so Caddy could serve it statically too; TileServer GL is kept because
# it also ships a demo map for eyeballing the basemap.
# `uvx` fetches the CLI from the protomaps-cli package on demand (uv is installed
# in .sandbox; nothing is installed system-wide). Zoom 0-14 gives district-level
# detail; raising it grows the file by several GB — tune only if needed.
nice -n 10 uvx --from protomaps-cli protomaps build "$OSM_FILE" \
  --output "$OUT" --max-zoom 14 --edge-buffer 8000

echo "Done: $OUT"
