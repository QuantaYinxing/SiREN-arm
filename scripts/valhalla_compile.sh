#!/usr/bin/env bash
# Compile Valhalla locally (arm64 fork). The author's x86 laptop needed
# .sandbox/deps-pb (protobuf 3.21.12 rebuilt from source) because Ubuntu 24.04
# ships protobuf 36, which SIGSEGVs Valhalla 3.9.3 on repeat /route requests.
# Ubuntu 26.10 arm64 ships protobuf 3.21.12 itself, so the system libs are used
# directly and the plain .sandbox/bin/valhalla_service is stable.
#
# Pinned commits (infra/README.md): Valhalla ac46322637801a3ea7735cf1ba63f7928a9314c4,
# prime_server 0d41876997760e22396075aeb7873bffcffd8786. That Valhalla commit no longer
# vendors prime_server in third_party (CMakeLists finds it via find_package /
# pkg-config), so it is built standalone from kevinkreiser/prime_server (the
# upstream repo Valhalla consumes) into .sandbox/deps-prime and passed via
# CMAKE_PREFIX_PATH; its shared libs go to .sandbox/lib for the LD_LIBRARY_PATH
# convention used by valhalla_build.sh and valhalla_keep.sh.
# Submodule trees pin their own third_party commits; `submodule update` fetches them.
#
# Heavy build (docs/rules.md): run with free RAM, nice, capped -j (never all cores).
# Needs apt packages: g++ cmake libprotobuf-dev protobuf-compiler libspatialite-dev
# libsqlite3-dev libcurl4-openssl-dev libzmq3-dev libczmq-dev libboost-dev
# libgeotiff-dev liblz4-dev libluajit-5.1-dev libgeos-dev libssl-dev libexpat1-dev
# libreadosm-dev zlib1g-dev git.
set -euo pipefail
cd "$(dirname "$0")/.."
SRC=".sandbox/src/valhalla"
PRIME=".sandbox/src/prime_server"
THREADS="${THREADS:-3}"
VALHALLA_COMMIT="ac46322637801a3ea7735cf1ba63f7928a9314c4"
PRIME_COMMIT="0d41876997760e22396075aeb7873bffcffd8786"
mkdir -p .sandbox/bin .sandbox/lib .sandbox/src .sandbox/deps-prime

if [ ! -e "$SRC/.git" ]; then
  echo "→ fetch Valhalla $VALHALLA_COMMIT ..."
  git init -q "$SRC"
  git -C "$SRC" fetch --depth 1 https://github.com/valhalla/valhalla.git "$VALHALLA_COMMIT"
  git -C "$SRC" checkout -q FETCH_HEAD
  git -C "$SRC" submodule update --init --recursive
fi

if [ ! -e "$PRIME/.git" ]; then
  echo "→ fetch prime_server $PRIME_COMMIT ..."
  git init -q "$PRIME"
  git -C "$PRIME" fetch --depth 1 https://github.com/kevinkreiser/prime_server.git "$PRIME_COMMIT"
  git -C "$PRIME" checkout -q FETCH_HEAD
fi
# src/logging is itself a submodule; init outside the fetch guard so reruns heal it.
git -C "$PRIME" submodule update --init --recursive

echo "→ build prime_server → .sandbox/deps-prime ..."
cmake -S "$PRIME" -B "$PRIME/build" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PWD/.sandbox/deps-prime" >/dev/null
nice -n 10 cmake --build "$PRIME/build" -j "$THREADS"
cmake --install "$PRIME/build"

# Ubuntu 26.10 dropped spatialite-tools (a binary package exists in Debian as
# spatialite-bin), but Valhalla's cmake (ENABLE_DATA_TOOLS) hard-requires the
# 'spatialite' and 'spatialite_tool' executables (build_admins/build_timezones
# invoke spatialite_tool at runtime). They are built here from the upstream
# tarball in the Debian pool against system libspatialite-dev; needs apt
# libreadosm-dev (OSM loaders) plus the libspatialite runtime deps (freexl,
# minizip, rttopo, proj, geos, readosm - pulled in by libspatialite-dev).
if ! command -v spatialite >/dev/null 2>&1 && [ ! -x .sandbox/bin/spatialite ]; then
  echo "→ build spatialite tools ..."
  [ -f .sandbox/src/spatialite-tools_5.1.0a.orig.tar.gz ] || \
    curl -sL --fail -o .sandbox/src/spatialite-tools_5.1.0a.orig.tar.gz \
      https://deb.debian.org/debian/pool/main/s/spatialite-tools/spatialite-tools_5.1.0a.orig.tar.gz
  tar -xzf .sandbox/src/spatialite-tools_5.1.0a.orig.tar.gz -C .sandbox/src
  SPATIALITE_TOOLS=".sandbox/src/spatialite-tools-5.1.0a"
  (cd "$SPATIALITE_TOOLS" && ./configure --build="$(uname -m)-unknown-linux-gnu" >/dev/null 2>&1 && nice -n 10 make -j "$THREADS" >/dev/null)
  cp "$SPATIALITE_TOOLS/spatialite" "$SPATIALITE_TOOLS/spatialite_tool" .sandbox/bin/
fi
export PATH="$PWD/.sandbox/bin:$PATH"

echo "→ cmake configure + build (Release, tests off, shared libs) ..."
cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTS=OFF -DENABLE_TESTS=OFF -DINSTALL_TEST_LIB=OFF \
  -DVALHALLA_STATIC=OFF \
  -DENABLE_PYTHON_BINDINGS=OFF -DENABLE_NODE_BINDINGS=OFF \
  -DCMAKE_PREFIX_PATH="$PWD/.sandbox/deps-prime" >/dev/null
nice -n 10 cmake --build "$SRC/build" -j "$THREADS"

# Install the tools the scripts invoke; shared libs (libvalhalla, libprime_server)
# land in .sandbox/lib so the existing LD_LIBRARY_PATH convention holds.
cp "$SRC/build/valhalla_service" "$SRC/build/valhalla_build_config" \
   "$SRC/build/valhalla_build_admins" "$SRC/build/valhalla_build_timezones" \
   "$SRC/build/valhalla_build_tiles" "$SRC/build/valhalla_build_extract" .sandbox/bin/
cp -a "$PWD/.sandbox/deps-prime/lib/"*.so* .sandbox/lib/ 2>/dev/null || true
find "$SRC/build" -maxdepth 1 -name "*.so*" -exec cp -a {} .sandbox/lib/ \;

# protobuf pin: Valhalla 3.9.3 SIGSEGVs on repeat /route requests when linked
# against newer protobuf (the author's x86 Ubuntu 24.04 ships 36). Ubuntu 26.10
# arm64 ships protobuf 3.21.12 itself, so the plain build above is stable and no
# deps-pb exists. On any other system protobuf, build 3.21.12 into .sandbox/deps-pb
# and a second service binary .sandbox/bin/valhalla_service.pb321 (setup.sh and
# valhalla_keep.sh prefer pb321 when present - docs/blackbox.md 2026-11-23).
PB_VER="$(pkg-config --modversion protobuf 2>/dev/null || echo none)"
if [ "$PB_VER" != "3.21.12" ]; then
  echo "→ system protobuf $PB_VER: build protobuf 3.21.12 -> .sandbox/deps-pb + pb321 ..."
  mkdir -p .sandbox/deps-pb
  if [ ! -d .sandbox/src/protobuf-3.21.12 ]; then
    curl -sL --fail -o .sandbox/src/protobuf-cpp-3.21.12.tar.gz \
      https://github.com/protocolbuffers/protobuf/archive/refs/tags/v3.21.12.tar.gz
    tar -xzf .sandbox/src/protobuf-cpp-3.21.12.tar.gz -C .sandbox/src
  fi
  cmake -S .sandbox/src/protobuf-3.21.12 -B .sandbox/src/protobuf-3.21.12/build \
    -DCMAKE_BUILD_TYPE=Release -Dprotobuf_BUILD_TESTS=OFF \
    -DCMAKE_INSTALL_PREFIX="$PWD/.sandbox/deps-pb" >/dev/null
  nice -n 10 cmake --build .sandbox/src/protobuf-3.21.12/build -j "$THREADS"
  cmake --install .sandbox/src/protobuf-3.21.12/build
  PB_PREFIX="$PWD/.sandbox/deps-prime;$PWD/.sandbox/deps-pb"
  export PKG_CONFIG_PATH="$PWD/.sandbox/deps-pb/lib/pkgconfig:$PWD/.sandbox/deps-pb/lib64/pkgconfig:${PKG_CONFIG_PATH:-}"
  cmake -S "$SRC" -B "$SRC/build-pb321" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_TESTS=OFF -DENABLE_TESTS=OFF -DINSTALL_TEST_LIB=OFF \
    -DVALHALLA_STATIC=OFF \
    -DENABLE_PYTHON_BINDINGS=OFF -DENABLE_NODE_BINDINGS=OFF \
    -DCMAKE_PREFIX_PATH="$PB_PREFIX" >/dev/null
  nice -n 10 cmake --build "$SRC/build-pb321" -j "$THREADS"
  cp "$SRC/build-pb321/valhalla_service" .sandbox/bin/valhalla_service.pb321
  cp -a "$PWD/.sandbox/deps-pb/lib/"*.so* .sandbox/lib/ 2>/dev/null || true
  find "$SRC/build-pb321" -maxdepth 1 -name "*.so*" -exec cp -a {} .sandbox/lib/ \;
fi

echo "Done. Serve: LD_LIBRARY_PATH=$PWD/.sandbox/lib .sandbox/bin/valhalla_service .sandbox/data/valhalla/valhalla.json 1"
echo "Next: ./scripts/valhalla_build.sh (needs .sandbox/bin/osm_bbox - compile per scripts/osm_crop.cpp header)"