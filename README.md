# SiREN — Fire-Brigade Response & Dispatch

Operator console (`/ops`) and crew phone app (`/field`), one backend. Concept and
architecture: `docs/siren.md` (local, not tracked). Binding rules for every session:
`docs/rules.md`. Session work log: `docs/blackbox.md` (append-only, mandatory).

## What is in this repo

| Path | Contents |
| --- | --- |
| `contracts/` | Source of truth: `openapi.yaml`, `events.json` + `events.md`, `db.sql`, `config.yaml` |
| `backend/` | FastAPI backend: config/enum loader, dispatch state machine, Valhalla routing + estimate fallback, water readings and pump estimate, road restrictions, SQLite dev store, WebSocket hub |
| `seed/` | Initial data (station, teams, accounts, vehicle profiles, vehicles, members, water sources, restrictions, incidents) — synthetic Garut-area data, real fire-brigade data still pending |
| `infra/` | docker-compose (Postgres/PostGIS, Valhalla, tile server, Caddy), Valhalla config, Caddyfile, build scripts |
| `apps/ops`, `apps/field` | Frontend PWAs (later phase; not built yet) |
| `.sandbox/` | Sandboxed installs and generated data — never global, never committed |

## Wire conventions

- JSON keys are snake_case; positions are `{lat, lon}` (+ optional `accuracy_m`).
- Enum values are Indonesian operational names (`Baru`, `Dispatched`, `EnRoute`, `OnScene`,
  `Returning`, `IsiUlang`, `Istirahat`, `Offline`, `Selesai`, `Dibatalkan`; water levels
  0-4). Labels and glosses come from `contracts/config.yaml` — nothing is hard-coded in
  components.
- WebSocket envelope: `{id, type, ts, v, payload}`; reconnect sends `snapshot.request`.
- Status transitions come from `config.yaml`; illegal transitions return HTTP 409 (or a WS
  `error` envelope) with the allowed list.

## Dev run (no Docker on this machine)

```bash
nice -n 10 .sandbox/py/bin/python -m uvicorn siren.main:app --port 8000 \
  --app-dir backend
# SQLite at .sandbox/data/siren.sqlite, seed/ loaded, routing.provider=estimate
SIREN_ROUTING_PROVIDER=estimate .sandbox/py/bin/python backend/siren/main.py
```

Tests (sandboxed, deterministic, injected clock):

```bash
nice -n 10 .sandbox/py/bin/python -m pytest
```

## Production (later)

`infra/docker-compose.yml` + `infra/Caddyfile` (automatic HTTPS), PostgreSQL/PostGIS,
self-hosted Valhalla with the Java/Indonesia OSM extract, self-hosted vector tiles. See
`infra/README.md` for the disk and CPU/RAM budget — the laptop runs local inference, so
heavy builds are capped with `nice -n 10` and explicit thread limits.
