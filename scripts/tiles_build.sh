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
mkdir -p .sandbox/data/tiles .sandbox/bin

# go-pmtiles binary is not committed (.sandbox is sandboxed); fetch the release
# asset for the local arch (x86_64/aarch64) if absent. Asset names carry the
# version: go-pmtiles_<ver>_Linux_<arch>.tar.gz.
if [ ! -x .sandbox/bin/pmtiles ]; then
  echo "→ download pmtiles ke .sandbox/bin ..."
  .sandbox/py/bin/python - <<'PYEOF'
import json, os, platform, tarfile, urllib.request
arch = {"x86_64": "Linux_x86_64", "aarch64": "Linux_arm64"}[platform.machine()]
rel = json.load(urllib.request.urlopen("https://api.github.com/repos/protomaps/go-pmtiles/releases/latest"))
url = next(a["browser_download_url"] for a in rel["assets"]
           if a["name"] == f"go-pmtiles_{rel['tag_name'][1:]}_{arch}.tar.gz")
tmp = ".sandbox/bin/pmtiles.tgz"
urllib.request.urlretrieve(url, tmp)
with tarfile.open(tmp) as t:
    m = next(m for m in t.getmembers() if os.path.basename(m.name) == "pmtiles")
    with t.extractfile(m) as src, open(".sandbox/bin/pmtiles", "wb") as dst:
        dst.write(src.read())
os.chmod(".sandbox/bin/pmtiles", 0o755)
os.remove(tmp)
PYEOF
fi

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
