# SiREN infrastructure

Routing data, container stack, and proxy config. Contracts (`contracts/`) stay the
source of truth; this directory only wires things together.

## Dev path (no Docker needed)

On this laptop the backend runs against SQLite and the estimate routing provider:

```sh
cd backend
../.sandbox/py/bin/python -m uvicorn siren.main:app --port 8000
```

`Config.load` reads `contracts/config.yaml` by default. That file ships
`routing.provider: valhalla`; when Valhalla is not running set
`routing.provider: estimate` (the estimate fallback straight-lines with
`routing.fallback` knobs — it also kicks in automatically when Valhalla is
unreachable). No TLS, no proxy: hit `http://127.0.0.1:8000` directly. The
compose `backend` service passes `SIREN_DB` / `SIREN_CONFIG` / `VALHALLA_URL` env
vars for the container layout below.

## Production path (compose + Caddy)

```sh
docker compose -f infra/docker-compose.yml up -d
```

Services (see `infra/docker-compose.yml`, host mounts commented there):

| Service    | What it runs                                              | Port |
|-----------|---------------------------------------------------------|------|
| postgres   | PostGIS 16, named volume `pgdata`                       | 5432 |
| valhalla   | routing engine on tiles from `scripts/valhalla_build.sh` | 8081 |
| backend    | FastAPI (`uvicorn siren.main:app`), SQLite at `/data`    | 8000 |
| tiles      | TileServer GL serving `.sandbox/data/tiles/*.pmtiles`    | 8082 |
| caddy      | reverse proxy + automatic HTTPS for `siren.id`           | 80/443 |

Routing/vector tiles are built once, sandboxed, and reused:

```sh
./scripts/valhalla_build.sh   # Valhalla tiles -> .sandbox/data/valhalla
./scripts/tiles_build.sh      # PMTiles basemap -> .sandbox/data/tiles
```

Both accept `OSM_URL` (Geofabrik extract) and thread caps; see the script headers.

## Gaps on this laptop (honest list)

- **docker and podman are absent**, so the compose stack has never been executed
  here — `infra/docker-compose.yml` is written and YAML-valid, nothing more.
- **Valhalla binaries are absent**, so `scripts/valhalla_build.sh` cannot run on
  this machine; it needs a host with Valhalla installed (or the compose container).
- **npm is absent** (node exists but npm does not), so the frontend phase that
  produces `apps/ops/dist` and `apps/field/dist` cannot build here yet; the
  Caddyfile roots for those dirs are commented as future mounts.
- **uv/uvx and curl are present**, so `scripts/tiles_build.sh` is runnable here —
  just heavy (see budget below).
- `backend/Dockerfile` is not in this repo yet; the compose `backend` service
  `build:` needs it before the stack can build.

## Disk budget

`.sandbox/data/` can grow to several GB: the Geofabrik Indonesia pbf (~1 GB),
Valhalla tiles plus packed tar (several GB), and the PMTiles basemap (up to a
few GB at zoom 0–14). `/home` currently has ~28 GB free of 296 GB (91% used), so
keep one extract at a time and prefer the smaller Jawa extract while iterating
(`OSM_URL=https://download.geofabrik.de/asia/indonesia-jawa-latest.osm.pbf`).
Generated data is gitignored; delete and rebuild rather than committing.

## CPU / RAM caps

The local inference engine holds nearly all of the 31 GB RAM (≈0 free), so heavy
builds run one at a time and always capped: `nice -n 10` plus an explicit thread
count (`THREADS=4` default in `scripts/valhalla_build.sh`, never all cores), and
the Valhalla service runs with a single worker (`infra/valhalla.json`). If a
build fails on memory, switch to the Jawa extract before touching knobs.
