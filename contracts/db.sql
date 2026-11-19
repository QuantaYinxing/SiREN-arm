-- SiREN database schema (source of truth).
--
-- Target: PostgreSQL 16 + PostGIS 3.4 (infra/docker-compose.yml).
-- Dev fallback: SQLite mirror in backend/siren/store.py — same table and column names,
-- geometry stored as JSON ("kind","coordinates" [[lat,lon],...]), distances computed in
-- Python. Keep both in sync; this file is authoritative.
--
-- Enum VALUES are validated at the application layer against contracts/config.yaml
-- (docs/siren.md D6), so columns are TEXT with CHECK constraints on shape only.
-- Indonesian enum values (Baru, IsiUlang, Selesai, ...) are data, not code identifiers.

-- ---------------------------------------------------------------- master data

CREATE TABLE station (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE team (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  color         TEXT NOT NULL,                  -- hex, e.g. #ff9f68
  display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE shift (
  id        TEXT PRIMARY KEY,
  team_id   TEXT NOT NULL REFERENCES team(id) ON DELETE CASCADE,
  name      TEXT NOT NULL,
  days      TEXT,                               -- e.g. "mon-fri", NULL = always
  starts_at TIME,
  ends_at   TIME
);

CREATE TABLE vehicle_profile (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  vehicle_type    TEXT NOT NULL,                 -- truck | motorbike | patrol_car
  height_m        DOUBLE PRECISION,
  width_m         DOUBLE PRECISION,
  length_m        DOUBLE PRECISION,
  gross_weight_kg DOUBLE PRECISION,
  axle_load_kg    DOUBLE PRECISION,
  valhalla_costing JSONB NOT NULL DEFAULT '{}',  -- Valhalla costing options for this profile
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE vehicle (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  plate          TEXT,
  vehicle_type   TEXT NOT NULL,
  profile_id     TEXT REFERENCES vehicle_profile(id) ON DELETE SET NULL,
  team_id        TEXT REFERENCES team(id) ON DELETE SET NULL,   -- dynamic per team (D10)
  pump_flow_lpm  DOUBLE PRECISION,
  tank_capacity_l DOUBLE PRECISION,
  status         TEXT NOT NULL DEFAULT 'Standby',
  lat            DOUBLE PRECISION,
  lon            DOUBLE PRECISION,
  eta_minutes    DOUBLE PRECISION,                -- last planned route ETA; cleared on Standby
  updated_at     TIMESTAMPTZ,
  CHECK (status <> '')
);

CREATE TABLE account (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  role          TEXT NOT NULL,                    -- operator | officer
  password_hash TEXT NOT NULL,
  member_id     TEXT REFERENCES member(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE member (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  role       TEXT,                                -- komandan | pompa_operator | pemadam
  team_id    TEXT NOT NULL REFERENCES team(id) ON DELETE RESTRICT,
  vehicle_id TEXT REFERENCES vehicle(id) ON DELETE SET NULL,
  status     TEXT NOT NULL DEFAULT 'Standby',
  updated_at TIMESTAMPTZ,
  CHECK (status <> '')
);

-- ---------------------------------------------------------------- water

CREATE TABLE water_source (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  type          TEXT NOT NULL,                    -- hydrant | river | pond | tank | well | other
  status        TEXT NOT NULL DEFAULT 'active',   -- active | dry | broken | inactive
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  capacity_note TEXT,
  notes         TEXT,
  created_by    TEXT REFERENCES account(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ
);

-- Points a truck can actually reach; may be several per source.
CREATE TABLE water_access_point (
  id              TEXT PRIMARY KEY,
  water_source_id TEXT NOT NULL REFERENCES water_source(id) ON DELETE CASCADE,
  lat             DOUBLE PRECISION NOT NULL,
  lon             DOUBLE PRECISION NOT NULL,
  note            TEXT
);

CREATE TABLE water_level_reading (
  id          TEXT PRIMARY KEY,
  vehicle_id  TEXT NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
  level       INTEGER NOT NULL CHECK (level BETWEEN 0 AND 4),
  source      TEXT NOT NULL CHECK (source IN ('operator', 'officer', 'estimate')),
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- roads

CREATE TABLE road_restriction (
  id                   TEXT PRIMARY KEY,
  kind                 TEXT NOT NULL CHECK (kind IN ('point', 'line', 'polygon')),
  geometry             JSONB NOT NULL,            -- {"kind":..,"coordinates":[[lat,lon],...]}
  reason               TEXT NOT NULL,             -- narrow | low_bridge | flood | weight_limit | blocked | other
  affected_vehicle_type TEXT NOT NULL DEFAULT 'truck'
    CHECK (affected_vehicle_type IN ('truck', 'motorbike', 'patrol_car', 'all')),
  status               TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'verified', 'rejected', 'expired')),
  reported_by          TEXT REFERENCES account(id),
  reported_member_id   TEXT REFERENCES member(id),
  reported_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_by          TEXT REFERENCES account(id),
  verified_at          TIMESTAMPTZ,
  expires_at           TIMESTAMPTZ,               -- temporary restrictions (flood)
  note                 TEXT,
  photo_url            TEXT
);

-- ---------------------------------------------------------------- incidents & dispatch

CREATE TABLE incident (
  id            TEXT PRIMARY KEY,
  type          TEXT NOT NULL,
  address       TEXT,
  description   TEXT,
  reporter_name TEXT,
  reporter_account_id TEXT REFERENCES account(id),
  reporter_link_id    TEXT,
  status        TEXT NOT NULL DEFAULT 'Baru',
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  dispatched_at TIMESTAMPTZ,
  arrived_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  cancelled_at  TIMESTAMPTZ,
  CHECK (status <> '')
);

-- One row per member per incident: dispatch is custom per member (D9), so a team can
-- be split across incidents and one incident can draw members from two teams.
CREATE TABLE assignment (
  id              TEXT PRIMARY KEY,
  incident_id     TEXT NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  member_id       TEXT NOT NULL REFERENCES member(id) ON DELETE RESTRICT,
  vehicle_id      TEXT REFERENCES vehicle(id) ON DELETE SET NULL,   -- optional
  water_source_id TEXT REFERENCES water_source(id) ON DELETE SET NULL,
  role            TEXT,
  status          TEXT NOT NULL DEFAULT 'Dispatched',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ,
  CHECK (status <> ''),
  UNIQUE (incident_id, member_id)
);

-- ---------------------------------------------------------------- live data & audit

CREATE TABLE position_ping (
  id           TEXT PRIMARY KEY,
  member_id    TEXT NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  vehicle_id   TEXT REFERENCES vehicle(id) ON DELETE SET NULL,
  lat          DOUBLE PRECISION NOT NULL,
  lon          DOUBLE PRECISION NOT NULL,
  accuracy_m   DOUBLE PRECISION,
  status_at    TEXT,                              -- duty status the member reported with
  received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_log (
  id           TEXT PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  type         TEXT NOT NULL,                     -- same names as WS event types + admin kinds
  actor_account_id TEXT REFERENCES account(id),
  actor_member_id  TEXT REFERENCES member(id),
  incident_id  TEXT REFERENCES incident(id) ON DELETE SET NULL,
  payload      JSONB NOT NULL DEFAULT '{}',
  text         TEXT NOT NULL                      -- Indonesian UI sentence
);

CREATE TABLE reporter_link (
  token       TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  created_by  TEXT REFERENCES account(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  status      TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'received', 'expired', 'used')),
  lat         DOUBLE PRECISION,
  lon         DOUBLE PRECISION,
  accuracy_m  DOUBLE PRECISION
);

-- ---------------------------------------------------------------- config (D6)

CREATE TABLE config_enum (
  group_name TEXT NOT NULL,                       -- member_status, water_level, restriction_reason, ...
  value      TEXT NOT NULL,
  label      TEXT NOT NULL,                       -- Indonesian UI string
  gloss      TEXT NOT NULL,                       -- English explanation
  sort       INTEGER NOT NULL DEFAULT 0,
  enabled    BOOLEAN NOT NULL DEFAULT true,
  PRIMARY KEY (group_name, value)
);

CREATE TABLE config_key (
  key        TEXT PRIMARY KEY,                    -- position.stale_after_seconds, routing.provider, ...
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- indexes

CREATE INDEX vehicle_status_idx ON vehicle(status);
CREATE INDEX member_status_idx ON member(status);
CREATE INDEX incident_status_idx ON incident(status);
CREATE INDEX incident_created_idx ON incident(created_at DESC);
CREATE INDEX assignment_incident_idx ON assignment(incident_id);
CREATE INDEX restriction_status_idx ON road_restriction(status);
CREATE INDEX water_source_status_idx ON water_source(status);
CREATE INDEX water_reading_vehicle_idx ON water_level_reading(vehicle_id, recorded_at DESC);
CREATE INDEX ping_member_idx ON position_ping(member_id, received_at DESC);
CREATE INDEX log_ts_idx ON event_log(ts DESC);
CREATE INDEX log_incident_idx ON event_log(incident_id, ts DESC);

-- PostGIS geography columns (dev SQLite mirror keeps lat/lon doubles instead):
-- ALTER TABLE station ADD COLUMN location geography(Point);
-- ALTER TABLE incident ADD COLUMN location geography(Point);
-- ALTER TABLE water_source ADD COLUMN location geography(Point);
-- ALTER TABLE vehicle ADD COLUMN location geography(Point);
-- ALTER TABLE road_restriction ADD COLUMN geom geography(Polygon);
-- CREATE INDEX restriction_geom_idx ON road_restriction USING gist (geom);
