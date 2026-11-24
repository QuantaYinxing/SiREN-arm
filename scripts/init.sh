#!/usr/bin/env bash
# One-shot installer: clone -> ./setup.sh -init -> ./setup.sh gamemode.
# Idempotent: every step skips when its artifact already exists.
# Works on x86_64 and aarch64: cloudflared/pmtiles download per-arch, Valhalla +
# osm_bbox compile locally for the machine's arch (docs/rules.md: all installs and
# generated data stay in .sandbox/, heavy builds run nice with capped -j).
#
#   ./setup.sh -init [--dry-run] [--skip-osm] [--no-tests]
#   --dry-run  print the plan (download URLs, build commands), touch nothing
#   --skip-osm skip the Geofabrik extract download (~1.7 GB) — needed for tiles
#   --no-tests skip the pytest smoke run at the end
#   env: THREADS (build cap, default 3), SIREN_ARCH (arch override for mocking)
set -euo pipefail
cd "$(dirname "$0")/.."
ARCH="${SIREN_ARCH:-$(uname -m)}"
PY=".sandbox/py/bin/python"
THREADS="${THREADS:-3}"
DRY=0 SKIP_OSM=0 NOTESTS=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --skip-osm) SKIP_OSM=1 ;;
    --no-tests) NOTESTS=1 ;;
    *) echo "flag tidak dikenal: $a (--dry-run|--skip-osm|--no-tests)" >&2; exit 1 ;;
  esac
done

say() { echo "→ $1"; }
skip() { echo "  sudah ada: $1 — lewati"; }
run() {
  if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; return; fi
  "$@"
}

# 1. python venv + backend deps (editable install with dev extras)
if [ ! -x "$PY" ]; then
  say "venv .sandbox/py + deps backend[dev] ..."
  if command -v uv >/dev/null 2>&1; then
    run uv venv .sandbox/py
    run uv pip install --python "$PY" -e "backend[dev]"
  else
    if [ "$DRY" = 0 ]; then
      python3 -m venv .sandbox/py 2>/dev/null \
        || { python3 -m venv --without-pip .sandbox/py \
             && curl -sL --fail https://bootstrap.pypa.io/get-pip.py | "$PY"; }
      "$PY" -m pip install -e "backend[dev]"
    else
      echo "  [dry-run] python3 -m venv .sandbox/py || (--without-pip + get-pip)"
      echo "  [dry-run] $PY -m pip install -e backend[dev]"
    fi
  fi
else
  skip ".sandbox/py"
  if [ "$DRY" = 0 ] && ! "$PY" -c "import fastapi, uvicorn, httpx, pytest" >/dev/null 2>&1; then
    say "deps backend belum lengkap — reinstall ..."
    run "$PY" -m pip install -e "backend[dev]"
  else
    skip "backend deps"
  fi
fi

# 2. apt dev packages for the native builds (never auto-sudo; just report)
NEED_CC=0
[ -x .sandbox/bin/valhalla_service ] || NEED_CC=1
[ -x .sandbox/bin/osm_bbox ] || NEED_CC=1
if [ "$NEED_CC" = 1 ]; then
  MISS=""
  for c in g++ cmake git; do
    command -v "$c" >/dev/null 2>&1 || MISS+=" $c"
  done
  for pkg in libprotobuf-dev protobuf-compiler libspatialite-dev libsqlite3-dev \
             libcurl4-openssl-dev libzmq3-dev libczmq-dev libboost-dev \
             libgeotiff-dev liblz4-dev libluajit-5.1-dev libgeos-dev \
             libssl-dev libexpat1-dev libreadosm-dev zlib1g-dev; do
    dpkg -l "$pkg" 2>/dev/null | grep -q '^ii ' || MISS+=" $pkg"
  done
  if [ -n "$MISS" ]; then
    say "paket apt belum ada:$MISS"
    echo "  jalankan: sudo apt install$MISS"
  fi
fi

# 3. Valhalla service + build tools (arch-local; pb321 variant picked by
#    valhalla_compile.sh when system protobuf != 3.21.12 — the x86 workaround)
if [ ! -x .sandbox/bin/valhalla_service ]; then
  say "compile Valhalla + prime_server + spatialite tools (jam-an, RAM bebas) ..."
  THREADS="$THREADS" run ./scripts/valhalla_compile.sh
else
  skip ".sandbox/bin/valhalla_service"
fi

# 4. bbox crop tool (libosmium/protozero headers vendored from the PyPI osmium sdist)
if [ ! -x .sandbox/bin/osm_bbox ]; then
  if [ ! -d .sandbox/include/libosmium ]; then
    say "vendor header libosmium/protozero dari sdist osmium 4.3.2 ..."
    run "$PY" - <<'EOF'
import json, shutil, tarfile, urllib.request
meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/osmium/4.3.2/json"))
sdist = next(u["url"] for u in meta["urls"] if u["filename"].endswith(".tar.gz"))
urllib.request.urlretrieve(sdist, ".sandbox/src/osmium-4.3.2.tar.gz")
with tarfile.open(".sandbox/src/osmium-4.3.2.tar.gz") as t:
    t.extractall(".sandbox/src")
src = ".sandbox/src/osmium-4.3.2/contrib"
shutil.copytree(f"{src}/libosmium/include", ".sandbox/include/libosmium")
shutil.copytree(f"{src}/protozero/include", ".sandbox/include/protozero")
EOF
  fi
  say "compile osm_bbox ..."
  run g++ -O2 -std=c++20 -DHAVE_PROTOZERO -DHAVE_ZLIB \
    -I .sandbox/include/libosmium -I .sandbox/include/protozero \
    scripts/osm_crop.cpp -o .sandbox/bin/osm_bbox -lz
else
  skip ".sandbox/bin/osm_bbox"
fi

# 5. OSM extract for the bbox crop (Geofabrik, ~1.7 GB)
if [ "$SKIP_OSM" = 0 ] && [ ! -f ".sandbox/data/osm/indonesia-latest.osm.pbf" ]; then
  say "download Geofabrik indonesia-latest.osm.pbf (~1.7 GB) ..."
  run bash -c 'mkdir -p .sandbox/data/osm
    curl -L --fail -o .sandbox/data/osm/indonesia-latest.osm.pbf.part \
      https://download.geofabrik.de/asia/indonesia-latest.osm.pbf \
    && mv .sandbox/data/osm/indonesia-latest.osm.pbf.part \
      .sandbox/data/osm/indonesia-latest.osm.pbf'
else
  skip ".sandbox/data/osm/indonesia-latest.osm.pbf"
fi

# 6. routing tiles (crop -> admins -> tz -> tiles -> tar extract on :8081)
if [ ! -f ".sandbox/data/valhalla/valhalla_tiles.tar" ]; then
  say "build routing tiles (RAM bebas) ..."
  THREADS="$THREADS" run ./scripts/valhalla_build.sh
else
  skip ".sandbox/data/valhalla/valhalla_tiles.tar"
fi

# 7. PMTiles basemap (range-request extract from a protomaps daily build)
if [ ! -f ".sandbox/data/tiles/siren.pmtiles" ]; then
  say "build PMTiles basemap ..."
  run ./scripts/tiles_build.sh
else
  skip ".sandbox/data/tiles/siren.pmtiles"
fi

# 8. cloudflared for realmode tunnels (arch-specific release asset)
if [ ! -x ".sandbox/bin/cloudflared" ]; then
  case "$ARCH" in
    x86_64) CFARCH="linux-amd64" ;;
    aarch64) CFARCH="linux-arm64" ;;
    *) CFARCH="" ;;
  esac
  if [ -n "$CFARCH" ]; then
    say "download cloudflared-$CFARCH ..."
    run bash -c "curl -L --fail -o .sandbox/bin/cloudflared.part \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-$CFARCH \
      && mv .sandbox/bin/cloudflared.part .sandbox/bin/cloudflared && chmod +x .sandbox/bin/cloudflared"
  else
    echo "  arch $ARCH tidak punya release cloudflared — realmode butuh manual" >&2
  fi
else
  skip ".sandbox/bin/cloudflared"
fi

# 9. smoke
if [ "$NOTESTS" = 0 ]; then
  say "smoke pytest ..."
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] $PY -m pytest backend/tests -q"
  else
    "$PY" -m pytest backend/tests -q
  fi
fi

echo "init selesai — jalankan ./setup.sh gamemode | realmode | database"
