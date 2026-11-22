"""Dev/test store: SQLite mirror of contracts/db.sql.

Production target is PostgreSQL + PostGIS (contracts/db.sql header explains the mapping);
dev uses SQLite so nothing outside the sandbox is required (docs/rules.md §3). Same table
and column names, geometry as JSON text, distances computed in Python (siren.geo).

All row-returning methods give plain dicts with the contract's snake_case keys. Timestamps
are ISO-8601 UTC strings. Enum values are validated by the caller (siren.state /
siren.config) before hitting the DB; columns only constrain shape.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .geo import affects, haversine_km, normalize_geometry
from .restrictions import VALID_VERIFY_STATUSES
from .timeutil import add_minutes, age_seconds, now_iso, parse_iso, today

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS station (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#ff9f68',
  display_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shift (
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, name TEXT NOT NULL,
  days TEXT, starts_at TEXT, ends_at TEXT
);
CREATE TABLE IF NOT EXISTS vehicle_profile (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, vehicle_type TEXT NOT NULL,
  height_m REAL, width_m REAL, length_m REAL, gross_weight_kg REAL, axle_load_kg REAL,
  valhalla_costing TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicle (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, plate TEXT, vehicle_type TEXT NOT NULL,
  profile_id TEXT, team_id TEXT, pump_flow_lpm REAL, tank_capacity_l REAL,
  status TEXT NOT NULL DEFAULT 'Standby', lat REAL, lon REAL, eta_minutes REAL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS account (
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
  password_hash TEXT NOT NULL, member_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT, team_id TEXT NOT NULL,
  vehicle_id TEXT, status TEXT NOT NULL DEFAULT 'Standby', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS water_source (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', lat REAL NOT NULL, lon REAL NOT NULL,
  capacity_note TEXT, notes TEXT, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS water_access_point (
  id TEXT PRIMARY KEY, water_source_id TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS water_level_reading (
  id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, level INTEGER NOT NULL,
  source TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS road_restriction (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, geometry TEXT NOT NULL, reason TEXT NOT NULL,
  affected_vehicle_type TEXT NOT NULL DEFAULT 'truck',
  status TEXT NOT NULL DEFAULT 'pending', reported_by TEXT, reported_member_id TEXT,
  reported_at TEXT NOT NULL, verified_by TEXT, verified_at TEXT, expires_at TEXT,
  note TEXT, photo_url TEXT
);
CREATE TABLE IF NOT EXISTS incident (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, address TEXT, description TEXT,
  reporter_name TEXT, reporter_account_id TEXT, reporter_link_id TEXT,
  status TEXT NOT NULL DEFAULT 'Baru', lat REAL NOT NULL, lon REAL NOT NULL,
  created_at TEXT NOT NULL, dispatched_at TEXT, arrived_at TEXT, finished_at TEXT,
  cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS assignment (
  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, member_id TEXT NOT NULL,
  vehicle_id TEXT, water_source_id TEXT, role TEXT,
  status TEXT NOT NULL DEFAULT 'Dispatched', created_at TEXT NOT NULL, updated_at TEXT,
  UNIQUE (incident_id, member_id)
);
CREATE TABLE IF NOT EXISTS position_ping (
  id TEXT PRIMARY KEY, member_id TEXT NOT NULL, vehicle_id TEXT, lat REAL NOT NULL,
  lon REAL NOT NULL, accuracy_m REAL, status_at TEXT, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_log (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, type TEXT NOT NULL, actor_account_id TEXT,
  actor_member_id TEXT, incident_id TEXT, payload TEXT NOT NULL DEFAULT '{}',
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reporter_link (
  token TEXT PRIMARY KEY, incident_id TEXT NOT NULL, created_by TEXT,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending', lat REAL, lon REAL, accuracy_m REAL
);
CREATE TABLE IF NOT EXISTS config_enum (
  group_name TEXT NOT NULL, value TEXT NOT NULL, label TEXT NOT NULL, gloss TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (group_name, value)
);
CREATE TABLE IF NOT EXISTS config_key (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vehicle_status_idx ON vehicle(status);
CREATE INDEX IF NOT EXISTS member_status_idx ON member(status);
CREATE INDEX IF NOT EXISTS incident_status_idx ON incident(status);
CREATE INDEX IF NOT EXISTS incident_created_idx ON incident(created_at DESC);
CREATE INDEX IF NOT EXISTS assignment_incident_idx ON assignment(incident_id);
CREATE INDEX IF NOT EXISTS restriction_status_idx ON road_restriction(status);
CREATE INDEX IF NOT EXISTS water_source_status_idx ON water_source(status);
CREATE INDEX IF NOT EXISTS water_reading_vehicle_idx ON water_level_reading(vehicle_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS ping_member_idx ON position_ping(member_id, received_at DESC);
CREATE INDEX IF NOT EXISTS log_ts_idx ON event_log(ts DESC);
CREATE INDEX IF NOT EXISTS log_incident_idx ON event_log(incident_id, ts DESC);
"""

ACTIVE_INCIDENT_STATUSES = ("Baru", "Dispatched", "EnRoute", "OnScene", "Returning")

# Keys allowed on each entity by contracts/events.json $defs (additionalProperties false).
_MEMBER_KEYS = ("id", "name", "role", "team_id", "vehicle_id", "status", "freshness",
                "position", "position_age_seconds", "updated_at")
_VEHICLE_KEYS = ("id", "name", "plate", "vehicle_type", "profile_id", "team_id", "status",
                 "position", "eta_minutes", "water_level", "water_level_source",
                 "water_level_confirmed_at", "estimated", "km_today", "pump_flow_lpm")
_INCIDENT_KEYS = ("id", "type", "address", "description", "reporter_name", "status",
                  "created_at", "dispatched_at", "arrived_at", "finished_at", "cancelled_at")
_WATER_KEYS = ("id", "name", "type", "status", "capacity_note", "notes", "updated_at")
_ASSIGNMENT_KEYS = ("id", "incident_id", "member_id", "vehicle_id", "water_source_id", "role", "status")
_RESTRICTION_KEYS = ("id", "kind", "geometry", "reason", "affected_vehicle_type", "status",
                     "reported_by", "reported_at", "verified_at", "expires_at", "note", "photo_url")


def _loads(text: str) -> Any:
    return json.loads(text)


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {key: row.get(key) for key in keys}


def _sha256(password: str) -> str:
    # Dev placeholder for password hashing; PBKDF2 lands with the real auth endpoints.
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _link_url(config: Config, token: str) -> str:
    base = config.get("reporter_link.base_url") or ""
    return f"{base.rstrip('/')}/{token}" if base else token


def _ping_position(ping: dict | None) -> dict | None:
    if ping is None:
        return None
    pos: dict[str, Any] = {"lat": ping["lat"], "lon": ping["lon"]}
    if ping.get("accuracy_m") is not None:
        pos["accuracy_m"] = ping["accuracy_m"]
    return pos


def _member_view(store: "Store", row: dict) -> dict:
    ping = store.latest_ping(row["id"])
    age = age_seconds(store.now_iso(), ping["received_at"] if ping else None)
    row["freshness"] = store.config.freshness(age)
    row["position_age_seconds"] = age
    row["position"] = _ping_position(ping)
    return row


def _vehicle_view(store: "Store", row: dict) -> dict:
    state = store.water_state(row["id"])
    row["water_level"] = state["level"]
    row["water_level_source"] = state["source"]
    row["water_level_confirmed_at"] = state["confirmed_at"]
    row["estimated"] = state["estimated"]
    row["position"] = ({"lat": row["lat"], "lon": row["lon"]}
                       if row.get("lat") is not None and row.get("lon") is not None else None)
    row["km_today"] = store.km_today(row["id"])
    return row


def _incident_view(store: "Store", row: dict) -> dict:
    row["assignments"] = store.assignments(row["id"])
    return row


def _water_view(store: "Store", row: dict) -> dict:
    points = store._rows("SELECT lat, lon FROM water_access_point WHERE water_source_id = ? ORDER BY id",
                         (row["id"],))
    row["access_points"] = [{"lat": p["lat"], "lon": p["lon"]} for p in points] or [
        {"lat": row["lat"], "lon": row["lon"]}]
    return row


def _restriction_view(store: "Store", row: dict) -> dict:
    row["geometry"] = _loads(row["geometry"])
    return row


def _log_entry(store: "Store", row: dict) -> dict:
    actor: str | None = None
    if row.get("actor_account_id"):
        account = store._row("SELECT username FROM account WHERE id = ?", (row["actor_account_id"],))
        actor = account["username"] if account else None
    elif row.get("actor_member_id"):
        member = store._row("SELECT name FROM member WHERE id = ?", (row["actor_member_id"],))
        actor = member["name"] if member else None
    return {"id": row["id"], "ts": row["ts"], "type": row["type"],
            "incident_id": row["incident_id"], "actor": actor, "text": row["text"]}


class NotFound(Exception):
    """Row does not exist."""


class Conflict(Exception):
    """State conflict: duplicate member on an incident, refill without confirmation."""


class LinkError(Exception):
    """Reporter link missing, expired, or already used (HTTP 410)."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Store:
    """SQLite-backed state. Owns rows, freshness math, and the WS event buffer."""

    def __init__(
        self,
        db_path: str | Path,
        config: Config,
        now: Callable[[], datetime] | None = None,
        event_buffer_limit: int = 500,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # uvicorn runs sync endpoints in worker threads and app.mock drives the
        # simulator from the event loop: one shared connection needs a lock.
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.event_buffer_limit = event_buffer_limit
        self.event_buffer: list[dict[str, Any]] = []
        self.last_event_id: str | None = None

    # ---- lifecycle ----

    def init_schema(self) -> None:
        """Create tables/indexes from SCHEMA_SQL."""
        with self._lock:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.commit()

    def load_seed(self, seed_dir: Path) -> dict[str, int]:
        """Load seed/*.json (station, teams, accounts, vehicle profiles, vehicles,
        members, water sources, restrictions). Idempotent: existing ids are skipped.
        Returns counts per entity kind."""
        seed_dir = Path(seed_dir)
        counts = {"station": 0, "teams": 0, "vehicle_profiles": 0, "vehicles": 0,
                  "members": 0, "accounts": 0, "water_sources": 0, "road_restrictions": 0,
                  "incidents": 0, "assignments": 0}
        now = self.now_iso()

        def seed(kind: str):
            p = seed_dir / f"{kind}.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

        def present(table: str, id_: str) -> bool:
            return self._row(f'SELECT id FROM "{table}" WHERE id = ?', (id_,)) is not None

        st = seed("station")
        if st and not present("station", st["id"]):
            self._insert("station", {"id": st["id"], "name": st["name"], "lat": st["lat"],
                                     "lon": st["lon"], "created_at": now})
            counts["station"] += 1

        for t in seed("teams") or []:
            if present("team", t["id"]):
                continue
            self._insert("team", {"id": t["id"], "name": t["name"], "color": t.get("color", "#ff9f68"),
                                  "display_order": t.get("display_order", 0)})
            counts["teams"] += 1

        for p in seed("vehicle_profiles") or []:
            if present("vehicle_profile", p["id"]):
                continue
            self._insert("vehicle_profile", {
                "id": p["id"], "name": p["name"], "vehicle_type": p["vehicle_type"],
                "height_m": p.get("height_m"), "width_m": p.get("width_m"),
                "length_m": p.get("length_m"), "gross_weight_kg": p.get("gross_weight_kg"),
                "axle_load_kg": p.get("axle_load_kg"),
                "valhalla_costing": json.dumps(p.get("valhalla_costing") or {}),
                "created_at": now})
            counts["vehicle_profiles"] += 1

        for v in seed("vehicles") or []:
            if present("vehicle", v["id"]):
                continue
            self._insert("vehicle", {
                "id": v["id"], "name": v["name"], "plate": v.get("plate"),
                "vehicle_type": v["vehicle_type"], "profile_id": v.get("profile_id"),
                "team_id": v.get("team_id"), "pump_flow_lpm": v.get("pump_flow_lpm"),
                "tank_capacity_l": v.get("tank_capacity_l"), "status": v.get("status", "Standby"),
                "lat": v.get("lat"), "lon": v.get("lon"), "eta_minutes": None, "updated_at": None})
            counts["vehicles"] += 1

        for m in seed("members") or []:
            if present("member", m["id"]):
                continue
            self._insert("member", {"id": m["id"], "name": m["name"], "role": m.get("role"),
                                    "team_id": m["team_id"], "vehicle_id": m.get("vehicle_id"),
                                    "status": m.get("status", "Standby"), "updated_at": None})
            counts["members"] += 1

        for a in seed("accounts") or []:
            if self.account_by_username(a["username"]) is not None:
                continue
            self.create_account(a["username"], a["role"], a["password"], a.get("member_id"))
            counts["accounts"] += 1

        for w in seed("water_sources") or []:
            if present("water_source", w["id"]):
                continue
            self._insert("water_source", {
                "id": w["id"], "name": w["name"], "type": w["type"], "status": w.get("status", "active"),
                "lat": w["lat"], "lon": w["lon"], "capacity_note": w.get("capacity_note"),
                "notes": w.get("notes"), "created_by": w.get("created_by"), "created_at": now,
                "updated_at": None})
            for pt in w.get("access_points") or [{"lat": w["lat"], "lon": w["lon"]}]:
                self._insert("water_access_point", {"id": new_id("wap"), "water_source_id": w["id"],
                                                    "lat": pt["lat"], "lon": pt["lon"], "note": pt.get("note")})
            counts["water_sources"] += 1

        for r in seed("road_restrictions") or []:
            if present("road_restriction", r["id"]):
                continue
            geom = normalize_geometry(r["geometry"])
            status = r.get("status", "pending")
            self._insert("road_restriction", {
                "id": r["id"], "kind": r["kind"], "geometry": json.dumps(geom), "reason": r["reason"],
                "affected_vehicle_type": r.get("affected_vehicle_type", "truck"), "status": status,
                "reported_by": r.get("reported_by"), "reported_member_id": r.get("reported_member_id"),
                "reported_at": r.get("reported_at") or now, "verified_by": r.get("verified_by"),
                "verified_at": r.get("verified_at") or (now if status == "verified" else None),
                "expires_at": r.get("expires_at"), "note": r.get("note"), "photo_url": r.get("photo_url")})
            counts["road_restrictions"] += 1

        for inc in seed("incidents") or []:
            if present("incident", inc["id"]):
                continue
            self._insert("incident", {
                "id": inc["id"], "type": inc["type"], "address": inc.get("address"),
                "description": inc.get("description"), "reporter_name": inc.get("reporter_name"),
                "reporter_account_id": inc.get("reporter_account_id"),
                "reporter_link_id": inc.get("reporter_link_id"), "status": inc.get("status", "Baru"),
                "lat": inc["lat"], "lon": inc["lon"], "created_at": inc.get("created_at") or now,
                "dispatched_at": inc.get("dispatched_at"), "arrived_at": inc.get("arrived_at"),
                "finished_at": inc.get("finished_at") or (now if inc.get("status") == "Selesai" else None),
                "cancelled_at": inc.get("cancelled_at") or (now if inc.get("status") == "Dibatalkan" else None)})
            for a in inc.get("assignments") or []:
                if self._row("SELECT id FROM assignment WHERE incident_id = ? AND member_id = ?",
                             (inc["id"], a["member_id"])) is not None:
                    continue
                self._insert("assignment", {
                    "id": new_id("asg"), "incident_id": inc["id"], "member_id": a["member_id"],
                    "vehicle_id": a.get("vehicle_id"), "water_source_id": a.get("water_source_id"),
                    "role": a.get("role"), "status": a.get("status", "Dispatched"),
                    "created_at": now, "updated_at": None})
                counts["assignments"] += 1
            counts["incidents"] += 1

        return counts

    def sync_config_tables(self) -> None:
        """Mirror config.yaml enum groups into config_enum / config_key."""
        now = self.now_iso()
        enum_rows: list[tuple] = []
        for group, node in self.config.data.items():
            if isinstance(node, dict):
                if not node or not all(isinstance(v, dict) and "label" in v for v in node.values()):
                    continue  # not an enum group (e.g. water_level, transitions)
                for sort, value in enumerate(node):
                    item = node[value]
                    enum_rows.append((group, str(value), str(item.get("label", value)),
                                      str(item.get("gloss", value)), sort, 1))
            elif isinstance(node, list):
                for sort, value in enumerate(node):
                    enum_rows.append((group, str(value), str(value), str(value), sort, 1))
        for sort, opt in enumerate(self.config.get("water_level.options") or []):
            enum_rows.append(("water_level", str(opt["value"]), str(opt["label"]),
                              str(opt.get("gloss", "")), sort, 1))
        for sort, value in enumerate(self.config.get("water_level.reading_sources") or []):
            enum_rows.append(("water_level.reading_sources", str(value), str(value), str(value), sort, 1))
        with self._lock:
            for group, value, label, gloss, sort, enabled in enum_rows:
                self.conn.execute(
                    "INSERT INTO config_enum (group_name, value, label, gloss, sort, enabled)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (group_name, value) DO UPDATE SET label = excluded.label,"
                    " gloss = excluded.gloss, sort = excluded.sort, enabled = excluded.enabled",
                    (group, value, label, gloss, sort, enabled))

        key_rows: list[tuple[str, Any]] = []

        def walk(node: dict, prefix: str) -> None:
            for key, value in node.items():
                if isinstance(value, dict):
                    walk(value, f"{prefix}{key}.")
                else:
                    key_rows.append((f"{prefix}{key}", value))

        walk(self.config.data, "")
        with self._lock:
            for key, value in key_rows:
                self.conn.execute(
                    "INSERT INTO config_key (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (key, json.dumps(value), now))
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def now_seconds(self) -> float:
        """Current time as epoch seconds (virtual clock in game mode)."""
        now = self._now()
        return now.timestamp() if isinstance(now, datetime) else float(now)

    def now_iso(self) -> str:
        """Current UTC time as ISO-8601 with 'Z'."""
        return now_iso(self._now())

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _row(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _insert(self, table: str, data: dict) -> None:
        cols = ", ".join(f'"{k}"' for k in data)
        placeholders = ", ".join("?" for _ in data)
        with self._lock:
            self.conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders})', tuple(data.values()))
            self.conn.commit()

    def _update(self, table: str, id_: str, data: dict) -> dict:
        """Update given columns and return the new row. Raises NotFound if absent."""
        with self._lock:
            if data:
                sets = ", ".join(f'"{k}" = ?' for k in data)
                self.conn.execute(f'UPDATE "{table}" SET {sets} WHERE id = ?', (*data.values(), id_))
                self.conn.commit()
            row = self._row(f'SELECT * FROM "{table}" WHERE id = ?', (id_,))
        if row is None:
            raise NotFound(f"{table} {id_} not found")
        return row

    def stations(self) -> list[dict]:
        return self._rows("SELECT * FROM station ORDER BY created_at, id")

    def station_position(self) -> tuple[float, float] | None:
        """First station's (lat, lon) — fallback when a vehicle has no known position."""
        row = self._row("SELECT lat, lon FROM station ORDER BY created_at, id LIMIT 1")
        return (float(row["lat"]), float(row["lon"])) if row else None

    # ---- master data ----

    def teams(self) -> list[dict]:
        return self._rows("SELECT * FROM team ORDER BY display_order, id")

    def create_team(self, data: dict) -> dict:
        id_ = data.get("id") or new_id("team")
        self._insert("team", {"id": id_, "name": data["name"], "color": data.get("color", "#ff9f68"),
                              "display_order": data.get("display_order", 0)})
        return self._row("SELECT * FROM team WHERE id = ?", (id_,))

    def update_team(self, id_: str, data: dict) -> dict:
        return self._update("team", id_, data)

    def members(self, team_id: str | None = None) -> list[dict]:
        """Rows plus server-computed `freshness` and `position_age_seconds`."""
        sql = "SELECT * FROM member"
        params: tuple = ()
        if team_id is not None:
            sql += " WHERE team_id = ?"
            params = (team_id,)
        sql += " ORDER BY team_id, id"
        return [_member_view(self, row) for row in self._rows(sql, params)]

    def member(self, id_: str) -> dict:
        row = self._row("SELECT * FROM member WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"member {id_} not found")
        return _member_view(self, row)

    def create_member(self, data: dict) -> dict:
        id_ = data.get("id") or new_id("mbr")
        self._insert("member", {"id": id_, "name": data["name"], "role": data.get("role"),
                                "team_id": data["team_id"], "vehicle_id": data.get("vehicle_id"),
                                "status": data.get("status", "Standby"), "updated_at": data.get("updated_at")})
        return self.member(id_)

    def update_member(self, id_: str, data: dict) -> dict:
        row = self._update("member", id_, data)
        return _member_view(self, row)

    def vehicle_profiles(self) -> list[dict]:
        """Rows with `valhalla_costing` parsed back to a dict."""
        rows = self._rows("SELECT * FROM vehicle_profile ORDER BY id")
        for row in rows:
            row["valhalla_costing"] = _loads(row["valhalla_costing"])
        return rows

    def vehicle_profile(self, id_: str) -> dict:
        row = self._row("SELECT * FROM vehicle_profile WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"vehicle profile {id_} not found")
        row["valhalla_costing"] = _loads(row["valhalla_costing"])
        return row

    def create_profile(self, data: dict) -> dict:
        id_ = data.get("id") or new_id("vp")
        self._insert("vehicle_profile", {
            "id": id_, "name": data["name"], "vehicle_type": data["vehicle_type"],
            "height_m": data.get("height_m"), "width_m": data.get("width_m"),
            "length_m": data.get("length_m"), "gross_weight_kg": data.get("gross_weight_kg"),
            "axle_load_kg": data.get("axle_load_kg"),
            "valhalla_costing": json.dumps(data.get("valhalla_costing") or {}),
            "created_at": data.get("created_at") or self.now_iso()})
        return self.vehicle_profile(id_)

    def update_profile(self, id_: str, data: dict) -> dict:
        self._update("vehicle_profile", id_, data)
        return self.vehicle_profile(id_)

    def vehicles(self) -> list[dict]:
        """Rows plus `position`, `water_level`, `water_level_source`,
        `water_level_confirmed_at`, `estimated`, `km_today`, `eta_minutes`."""
        return [_vehicle_view(self, row) for row in self._rows("SELECT * FROM vehicle ORDER BY id")]

    def vehicle(self, id_: str) -> dict:
        row = self._row("SELECT * FROM vehicle WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"vehicle {id_} not found")
        return _vehicle_view(self, row)

    def create_vehicle(self, data: dict) -> dict:
        id_ = data.get("id") or new_id("veh")
        self._insert("vehicle", {
            "id": id_, "name": data["name"], "plate": data.get("plate"),
            "vehicle_type": data["vehicle_type"], "profile_id": data.get("profile_id"),
            "team_id": data.get("team_id"), "pump_flow_lpm": data.get("pump_flow_lpm"),
            "tank_capacity_l": data.get("tank_capacity_l"), "status": data.get("status", "Standby"),
            "lat": data.get("lat"), "lon": data.get("lon"), "eta_minutes": data.get("eta_minutes"),
            "updated_at": data.get("updated_at")})
        return self.vehicle(id_)

    def update_vehicle(self, id_: str, data: dict) -> dict:
        row = self._update("vehicle", id_, data)
        return _vehicle_view(self, row)

    def set_vehicle_status(self, vehicle_id: str, status: str) -> dict:
        """Store-level write; the transition is validated by the caller."""
        return self.update_vehicle(vehicle_id, {"status": status, "updated_at": self.now_iso()})

    def water_sources(self, status: str = "all") -> list[dict]:
        """Rows with `access_points` merged in."""
        sql = "SELECT * FROM water_source"
        params: tuple = ()
        if status != "all":
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id"
        return [_water_view(self, row) for row in self._rows(sql, params)]

    def water_source(self, id_: str) -> dict:
        row = self._row("SELECT * FROM water_source WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"water source {id_} not found")
        return _water_view(self, row)

    def create_water_source(self, data: dict) -> dict:
        id_ = data.get("id") or new_id("ws")
        self._insert("water_source", {
            "id": id_, "name": data["name"], "type": data["type"],
            "status": data.get("status", "active"), "lat": data["lat"], "lon": data["lon"],
            "capacity_note": data.get("capacity_note"), "notes": data.get("notes"),
            "created_by": data.get("created_by"), "created_at": data.get("created_at") or self.now_iso(),
            "updated_at": None})
        for pt in data.get("access_points") or [{"lat": data["lat"], "lon": data["lon"]}]:
            self._insert("water_access_point", {"id": new_id("wap"), "water_source_id": id_,
                                                "lat": pt["lat"], "lon": pt["lon"], "note": pt.get("note")})
        return _water_view(self, self._row("SELECT * FROM water_source WHERE id = ?", (id_,)))

    def update_water_source(self, id_: str, data: dict) -> dict:
        row = self._row("SELECT * FROM water_source WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"water source {id_} not found")
        data = dict(data)
        points = data.pop("access_points", None)
        data.setdefault("updated_at", self.now_iso())
        self._update("water_source", id_, data)
        if points is not None:
            with self._lock:
                self.conn.execute("DELETE FROM water_access_point WHERE water_source_id = ?", (id_,))
                for pt in points:
                    self._insert("water_access_point", {"id": new_id("wap"), "water_source_id": id_,
                                                        "lat": pt["lat"], "lon": pt["lon"], "note": pt.get("note")})
        return _water_view(self, self._row("SELECT * FROM water_source WHERE id = ?", (id_,)))

    def remove_water_source(self, id_: str) -> dict:
        """Soft delete: status becomes 'inactive'. Returns the row."""
        row = self._update("water_source", id_, {"status": "inactive", "updated_at": self.now_iso()})
        return _water_view(self, row)

    # ---- water ----

    def latest_water_reading(self, vehicle_id: str) -> dict | None:
        return self._row("SELECT * FROM water_level_reading WHERE vehicle_id = ?"
                         " ORDER BY recorded_at DESC, rowid DESC LIMIT 1", (vehicle_id,))

    def water_state(self, vehicle_id: str) -> dict:
        """{"level", "source", "confirmed_at", "estimated", "age_seconds"} — newest
        reading, or the config minimum level with source 'estimate' if none exists."""
        rec = self.latest_water_reading(vehicle_id)
        if rec is None:
            return {"level": self.config.min_level(), "source": "estimate",
                    "confirmed_at": None, "estimated": True, "age_seconds": None}
        return {"level": rec["level"], "source": rec["source"], "confirmed_at": rec["recorded_at"],
                "estimated": rec["source"] == "estimate",
                "age_seconds": age_seconds(self.now_iso(), rec["recorded_at"])}

    def record_water_level(self, vehicle_id: str, level: int, source: str) -> dict:
        """Insert a water_level_reading and return the updated vehicle row."""
        level = self.config.validate_level(level)
        row = self._row("SELECT * FROM vehicle WHERE id = ?", (vehicle_id,))
        if row is None:
            raise NotFound(f"vehicle {vehicle_id} not found")
        self._insert("water_level_reading", {"id": new_id("wl"), "vehicle_id": vehicle_id,
                                             "level": level, "source": source, "recorded_at": self.now_iso()})
        return _vehicle_view(self, row)

    def refill_done(self, vehicle_id: str, confirmed: bool = True) -> dict:
        """Refill finished -> level becomes max (Penuh). Raises Conflict if not confirmed."""
        if not confirmed:
            raise Conflict("refill needs confirmation (water.refill_confirm)")
        return self.record_water_level(vehicle_id, self.config.max_level(), "operator")

    def refills_today(self, date: str | None = None) -> int:
        """Count readings that raised a vehicle to the max level from a lower one."""
        date = today(date or self.now_iso().split("T")[0])
        hi = self.config.max_level()
        rows = self._rows("SELECT * FROM water_level_reading WHERE level = ? AND substr(recorded_at, 1, 10) = ?"
                          " ORDER BY recorded_at, rowid", (hi, date))
        count = 0
        for row in rows:
            prev = self._row("SELECT level FROM water_level_reading WHERE vehicle_id = ?"
                             " AND rowid < (SELECT rowid FROM water_level_reading WHERE id = ?)"
                             " ORDER BY rowid DESC LIMIT 1", (row["vehicle_id"], row["id"]))
            prev_level = prev["level"] if prev else self.config.min_level()
            if prev_level < hi:
                count += 1
        return count

    # ---- incidents & dispatch ----

    def incidents(self, status: str = "active") -> list[dict]:
        """status: 'active' (not Selesai/Dibatalkan), 'all', or one exact value.
        Rows include `assignments`."""
        sql = "SELECT * FROM incident"
        params: tuple = ()
        if status == "active":
            marks = ", ".join("?" for _ in ACTIVE_INCIDENT_STATUSES)
            sql += f" WHERE status IN ({marks})"
            params = ACTIVE_INCIDENT_STATUSES
        elif status != "all":
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC, id"
        return [_incident_view(self, row) for row in self._rows(sql, params)]

    def incident(self, id_: str) -> dict:
        row = self._row("SELECT * FROM incident WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"incident {id_} not found")
        return _incident_view(self, row)

    def create_incident(self, data: dict) -> dict:
        """Ids are sequential 'I-<n>' continuing from the highest existing number."""
        ids = [row["id"] for row in self._rows("SELECT id FROM incident")]
        numbers = [int(m.group(1)) for m in (re.fullmatch(r"I-(\d+)", i) for i in ids) if m]
        id_ = f"I-{max(numbers) + 1 if numbers else 1}"
        self._insert("incident", {
            "id": id_, "type": data["type"], "address": data.get("address"),
            "description": data.get("description"), "reporter_name": data.get("reporter_name"),
            "reporter_account_id": data.get("reporter_account_id"),
            "reporter_link_id": data.get("reporter_link_id"), "status": data.get("status", "Baru"),
            "lat": data["lat"], "lon": data["lon"], "created_at": data.get("created_at") or self.now_iso(),
            "dispatched_at": None, "arrived_at": None, "finished_at": None, "cancelled_at": None})
        return self.incident(id_)

    def update_incident(self, id_: str, data: dict) -> dict:
        row = self._update("incident", id_, data)
        return _incident_view(self, row)

    def assignments(self, incident_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM assignment"
        params: tuple = ()
        if incident_id is not None:
            sql += " WHERE incident_id = ?"
            params = (incident_id,)
        sql += " ORDER BY created_at, rowid"
        return self._rows(sql, params)

    def assignment(self, id_: str) -> dict:
        row = self._row("SELECT * FROM assignment WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"assignment {id_} not found")
        return row

    def create_assignments(
        self,
        incident_id: str,
        member_ids: list[str],
        vehicle_id: str | None = None,
        water_source_id: str | None = None,
        roles: dict[str, str] | None = None,
    ) -> list[dict]:
        """One row per member (siren.md §5.5). Duplicate (incident, member) raises Conflict."""
        if self._row("SELECT id FROM incident WHERE id = ?", (incident_id,)) is None:
            raise NotFound(f"incident {incident_id} not found")
        existing = {a["member_id"] for a in self.assignments(incident_id)}
        seen: set[str] = set()
        for member_id in member_ids:
            if member_id in existing or member_id in seen:
                raise Conflict(f"member {member_id} already assigned to incident {incident_id}")
            seen.add(member_id)
            if self._row("SELECT id FROM member WHERE id = ?", (member_id,)) is None:
                raise NotFound(f"member {member_id} not found")
        now = self.now_iso()
        ids = [new_id("asg") for _ in member_ids]
        for id_, member_id in zip(ids, member_ids):
            self._insert("assignment", {
                "id": id_, "incident_id": incident_id, "member_id": member_id, "vehicle_id": vehicle_id,
                "water_source_id": water_source_id, "role": (roles or {}).get(member_id),
                "status": "Dispatched", "created_at": now, "updated_at": None})
        return [self.assignment(id_) for id_ in ids]

    def update_assignment(self, id_: str, data: dict) -> dict:
        self._update("assignment", id_, data)
        return self.assignment(id_)

    def withdraw_assignment(self, id_: str) -> dict:
        """status -> 'Withdrawn'. Member duty status is set by the caller (siren.state)."""
        return self.update_assignment(id_, {"status": "Withdrawn", "updated_at": self.now_iso()})

    def member_for_incident(self, incident_id: str) -> list[dict]:
        rows = self._rows("SELECT m.* FROM assignment a JOIN member m ON m.id = a.member_id"
                          " WHERE a.incident_id = ? ORDER BY a.created_at, a.rowid", (incident_id,))
        out: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            out.append(_member_view(self, row))
        return out

    # ---- positions ----

    def record_ping(
        self,
        member_id: str,
        lat: float,
        lon: float,
        accuracy_m: float | None = None,
        vehicle_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        """Insert a ping, move the member's last known position (and the vehicle's when
        vehicle_id is given), and return {"member_id", "position", "freshness",
        "position_age_seconds"}."""
        if self._row("SELECT id FROM member WHERE id = ?", (member_id,)) is None:
            raise NotFound(f"member {member_id} not found")
        if vehicle_id is not None and self._row("SELECT id FROM vehicle WHERE id = ?", (vehicle_id,)) is None:
            raise NotFound(f"vehicle {vehicle_id} not found")
        received = self.now_iso()
        self._insert("position_ping", {"id": new_id("ping"), "member_id": member_id,
                                       "vehicle_id": vehicle_id, "lat": lat, "lon": lon,
                                       "accuracy_m": accuracy_m, "status_at": status, "received_at": received})
        if vehicle_id is not None:
            self._update("vehicle", vehicle_id, {"lat": lat, "lon": lon, "updated_at": received})
        fresh, age = self.freshness(member_id)
        position: dict[str, Any] = {"lat": lat, "lon": lon}
        if accuracy_m is not None:
            position["accuracy_m"] = accuracy_m
        return {"member_id": member_id, "position": position, "freshness": fresh,
                "position_age_seconds": age}

    def latest_ping(self, member_id: str) -> dict | None:
        return self._row("SELECT * FROM position_ping WHERE member_id = ?"
                         " ORDER BY received_at DESC, rowid DESC LIMIT 1", (member_id,))

    def freshness(self, member_id: str) -> tuple[str, float | None]:
        """(live|stale|offline, age_seconds) from the newest ping; offline if none."""
        ping = self.latest_ping(member_id)
        age = age_seconds(self.now_iso(), ping["received_at"] if ping else None)
        return self.config.freshness(age), age

    def vehicle_position(self, vehicle_id: str) -> tuple[float, float] | None:
        row = self._row("SELECT lat, lon FROM vehicle WHERE id = ?", (vehicle_id,))
        if row is None or row["lat"] is None or row["lon"] is None:
            return None
        return (float(row["lat"]), float(row["lon"]))

    def km_today(self, vehicle_id: str, date: str | None = None) -> float:
        """Sum of haversine distances between that vehicle's pings within the UTC date."""
        date = today(date or self.now_iso().split("T")[0])
        rows = self._rows("SELECT * FROM position_ping WHERE vehicle_id = ? ORDER BY received_at, rowid",
                          (vehicle_id,))
        pts = [(float(r["lat"]), float(r["lon"]))
               for r in rows if parse_iso(r["received_at"]).date().isoformat() == date]
        return sum(haversine_km(p, q) for p, q in zip(pts, pts[1:]))

    def prune_pings(self, retention_days: int | None = None) -> int:
        """Delete pings older than config.position.retention_days; returns rows removed."""
        days = float(retention_days) if retention_days is not None else self.config.seconds("position.retention_days")
        cutoff = now_iso(self._now() - timedelta(days=days))
        with self._lock:
            cur = self.conn.execute("DELETE FROM position_ping WHERE received_at < ?", (cutoff,))
            self.conn.commit()
        return cur.rowcount

    # ---- restrictions ----

    def restrictions(self, status: str = "all") -> list[dict]:
        """Rows with `geometry` parsed back to {"kind","coordinates"}."""
        sql = "SELECT * FROM road_restriction"
        params: tuple = ()
        if status != "all":
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY reported_at DESC, id"
        return [_restriction_view(self, row) for row in self._rows(sql, params)]

    def restriction(self, id_: str) -> dict:
        row = self._row("SELECT * FROM road_restriction WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"road restriction {id_} not found")
        return _restriction_view(self, row)

    def create_restriction(self, data: dict) -> dict:
        id_ = data.get("id") or new_id("rr")
        geom = normalize_geometry(data["geometry"])
        now = self.now_iso()
        status = data.get("status", "pending")
        self._insert("road_restriction", {
            "id": id_, "kind": data.get("kind") or geom["kind"], "geometry": json.dumps(geom),
            "reason": data["reason"], "affected_vehicle_type": data.get("affected_vehicle_type", "truck"),
            "status": status, "reported_by": data.get("reported_by"),
            "reported_member_id": data.get("reported_member_id"),
            "reported_at": data.get("reported_at") or now, "verified_by": data.get("verified_by"),
            "verified_at": data.get("verified_at") or (now if status == "verified" else None),
            "expires_at": data.get("expires_at"), "note": data.get("note"), "photo_url": data.get("photo_url")})
        return self.restriction(id_)

    def verify_restriction(self, id_: str, status: str, note: str | None = None, verified_by: str | None = None) -> dict:
        """Operator decision. verified/rejected/expired only; pending is the initial state."""
        if status not in VALID_VERIFY_STATUSES:
            raise ValueError(f"invalid verification status {status!r}")
        data = {"status": status, "verified_at": self.now_iso()}
        if note is not None:
            data["note"] = note
        if verified_by is not None:
            data["verified_by"] = verified_by
        self._update("road_restriction", id_, data)
        return self.restriction(id_)

    def expire_overdue(self) -> list[dict]:
        """Flip past-expiry restrictions to 'expired'; returns the changed rows."""
        now = self.now_iso()
        rows = self._rows("SELECT * FROM road_restriction WHERE status = 'verified' AND expires_at IS NOT NULL"
                          " AND expires_at <= ? ORDER BY reported_at, id", (now,))
        return [_restriction_view(self, self._update("road_restriction", row["id"], {"status": "expired"}))
                for row in rows]

    def active_restrictions(self, vehicle_type: str) -> list[dict]:
        """Verified, not expired, affecting this vehicle type — applied to routing."""
        now = self.now_iso()
        rows = self._rows("SELECT * FROM road_restriction WHERE status = 'verified' ORDER BY reported_at, id")
        out: list[dict] = []
        for row in rows:
            if row["expires_at"] is not None and row["expires_at"] <= now:
                continue
            if affects(row, vehicle_type):
                out.append(_restriction_view(self, row))
        return out

    # ---- reporter links ----

    def create_reporter_link(self, incident_id: str, ttl_minutes: int | None = None, created_by: str | None = None) -> dict:
        """{"token", "url", "incident_id", "expires_at", "status": "pending"}"""
        if self._row("SELECT id FROM incident WHERE id = ?", (incident_id,)) is None:
            raise NotFound(f"incident {incident_id} not found")
        ttl = float(ttl_minutes) if ttl_minutes is not None else float(self.config.get("reporter_link.ttl_minutes", 30))
        token = secrets.token_urlsafe(16)
        created = self.now_iso()
        expires_at = add_minutes(created, ttl)
        self._insert("reporter_link", {"token": token, "incident_id": incident_id,
                                       "created_by": created_by, "created_at": created,
                                       "expires_at": expires_at, "used_at": None, "status": "pending",
                                       "lat": None, "lon": None, "accuracy_m": None})
        return {"token": token, "url": _link_url(self.config, token), "incident_id": incident_id,
                "expires_at": expires_at, "status": "pending"}

    def reporter_link(self, token: str) -> dict:
        row = self._row("SELECT * FROM reporter_link WHERE token = ?", (token,))
        if row is None:
            return None
        row["url"] = _link_url(self.config, row["token"])
        return row

    def consume_reporter_link(self, token: str, lat: float, lon: float, accuracy_m: float | None = None, name: str | None = None) -> dict:
        """Single-use: writes coordinates onto the incident and returns the incident.
        Raises LinkError when the token is missing, expired, or already used."""
        row = self._row("SELECT * FROM reporter_link WHERE token = ?", (token,))
        if row is None:
            raise LinkError(f"unknown reporter link {token!r}")
        now = self.now_iso()
        if row["expires_at"] <= now:
            with self._lock:
                self.conn.execute("UPDATE reporter_link SET status = 'expired' WHERE token = ?", (token,))
                self.conn.commit()
            raise LinkError("reporter link expired")
        if row["status"] != "pending":
            raise LinkError(f"reporter link already {row['status']}")
        with self._lock:
            self.conn.execute(
                "UPDATE reporter_link SET status = 'received', used_at = ?, lat = ?, lon = ?, accuracy_m = ?"
                " WHERE token = ?", (now, lat, lon, accuracy_m, token))
            self.conn.commit()
        data: dict[str, Any] = {"lat": lat, "lon": lon, "reporter_link_id": token}
        if name is not None:
            data["reporter_name"] = name
        self._update("incident", row["incident_id"], data)
        return self.incident(row["incident_id"])

    def reporter_links(self, status: str = "all") -> list[dict]:
        sql = "SELECT * FROM reporter_link"
        params: tuple = ()
        if status != "all":
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC, token"
        rows = self._rows(sql, params)
        for row in rows:
            row["url"] = _link_url(self.config, row["token"])
        return rows

    # ---- accounts ----

    def create_account(self, username: str, role: str, password: str, member_id: str | None = None) -> dict:
        id_ = new_id("acct")
        try:
            self._insert("account", {"id": id_, "username": username, "role": role,
                                     "password_hash": _sha256(password), "member_id": member_id,
                                     "created_at": self.now_iso()})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"username {username!r} already exists") from exc
        return self.account(id_)

    def account_by_username(self, username: str) -> dict | None:
        return self._row("SELECT * FROM account WHERE username = ?", (username,))

    def authenticate(self, username: str, password: str) -> dict | None:
        row = self.account_by_username(username)
        if row is None or row["password_hash"] != _sha256(password):
            return None
        return row

    # ---- account management (DB manager GUI) ----

    def accounts(self) -> list[dict]:
        """Login rows without the password hash."""
        return self._rows("SELECT id, username, role, member_id, created_at FROM account ORDER BY username")

    def account(self, id_: str) -> dict:
        row = self._row("SELECT id, username, role, member_id, created_at FROM account WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"account {id_} not found")
        return row

    def update_account(self, id_: str, data: dict) -> dict:
        """Accepts password (stored hashed), role, member_id."""
        fields: dict[str, object] = {}
        if "password" in data:
            fields["password_hash"] = _sha256(str(data["password"]))
        if "role" in data:
            fields["role"] = data["role"]
        if "member_id" in data:
            fields["member_id"] = data["member_id"]
        if fields:
            self._update("account", id_, fields)
        return self.account(id_)

    def delete_account(self, id_: str) -> dict:
        row = self.account(id_)
        with self._lock:
            self.conn.execute("DELETE FROM account WHERE id = ?", (id_,))
            self.conn.commit()
        return row

    def delete_member(self, id_: str) -> dict:
        row = self._row("SELECT * FROM member WHERE id = ?", (id_,))
        if row is None:
            raise NotFound(f"member {id_} not found")
        with self._lock:
            self.conn.execute("DELETE FROM member WHERE id = ?", (id_,))
            self.conn.commit()
        return row

    # ---- log & WS buffer ----

    def record_event(
        self,
        event_type: str,
        payload: dict,
        text: str,
        actor_account_id: str | None = None,
        actor_member_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict:
        """Append to event_log and to the bounded in-memory WS buffer; returns the
        envelope {"id","type","ts","v","payload"} (contracts/events.md)."""
        seq = getattr(self, "_event_seq", None)
        if seq is None:
            numbers = [int(m.group(1)) for m in
                       (re.fullmatch(r"ev_(\d+)", row["id"]) for row in self._rows("SELECT id FROM event_log"))
                       if m]
            seq = max(numbers) if numbers else 0
        seq += 1
        self._event_seq = seq
        id_ = f"ev_{seq}"
        ts = self.now_iso()
        envelope = {"id": id_, "type": event_type, "ts": ts, "v": 1, "payload": payload}
        self._insert("event_log", {"id": id_, "ts": ts, "type": event_type,
                                   "actor_account_id": actor_account_id, "actor_member_id": actor_member_id,
                                   "incident_id": incident_id, "payload": json.dumps(payload), "text": text})
        self.event_buffer.append(envelope)
        if len(self.event_buffer) > self.event_buffer_limit:
            del self.event_buffer[:len(self.event_buffer) - self.event_buffer_limit]
        self.last_event_id = id_
        return envelope

    def event_log(self, limit: int = 50, incident_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM event_log"
        params: list = []
        if incident_id is not None:
            sql += " WHERE incident_id = ?"
            params.append(incident_id)
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._rows(sql, tuple(params))
        for row in rows:
            row["payload"] = _loads(row["payload"])
        return rows

    def events_since(self, last_event_id: str | None) -> list[dict] | None:
        """Envelopes after `last_event_id`, or None when the buffer no longer covers the
        gap (caller must send a full snapshot)."""
        if last_event_id is None:
            return [dict(env) for env in self.event_buffer]
        for i, env in enumerate(self.event_buffer):
            if env["id"] == last_event_id:
                return [dict(e) for e in self.event_buffer[i + 1:]]
        return None

    # ---- aggregates ----

    def stats_today(self, date: str | None = None) -> dict:
        """{date, incidents_active, incidents_finished, km_total, km_per_vehicle,
        refills, avg_response_minutes, weather} — weather is None unless
        config.stats.weather_enabled."""
        date = today(date or self.now_iso().split("T")[0])
        marks = ", ".join("?" for _ in ACTIVE_INCIDENT_STATUSES)
        active = self._row(f"SELECT COUNT(*) AS n FROM incident WHERE status IN ({marks})",
                           ACTIVE_INCIDENT_STATUSES)["n"]
        finished = self._rows("SELECT * FROM incident WHERE finished_at IS NOT NULL"
                              " AND substr(finished_at, 1, 10) = ? ORDER BY finished_at, rowid", (date,))
        vehicles = self._rows("SELECT id FROM vehicle ORDER BY id")
        km_per_vehicle = {v["id"]: self.km_today(v["id"], date) for v in vehicles}
        minutes = [age_seconds(r["arrived_at"], r["created_at"]) / 60.0
                   for r in finished if r["arrived_at"]]
        avg = sum(minutes) / len(minutes) if minutes else None
        weather = ({"source": self.config.get("stats.weather_source")}
                   if self.config.get("stats.weather_enabled") else None)
        return {"date": date, "incidents_active": active, "incidents_finished": len(finished),
                "km_total": sum(km_per_vehicle.values()), "km_per_vehicle": km_per_vehicle,
                "refills": self.refills_today(date), "avg_response_minutes": avg, "weather": weather}

    def snapshot(self) -> dict:
        """Full snapshot per contracts/events.json $defs.snapshot: config dump, stations,
        teams, members, vehicles, water_sources, incidents, assignments, road_restrictions,
        reporter_links, event_log (recent), stats_today, server_ts, last_event_id."""
        incidents = []
        for row in self.incidents("all"):
            view = _pick(row, _INCIDENT_KEYS)
            view["location"] = {"lat": row["lat"], "lon": row["lon"]}
            incidents.append(view)
        water_sources = []
        for row in self.water_sources("all"):
            view = _pick(row, _WATER_KEYS)
            view["location"] = {"lat": row["lat"], "lon": row["lon"]}
            view["access_points"] = [{"lat": p["lat"], "lon": p["lon"]} for p in row["access_points"]]
            water_sources.append(view)
        return {
            "server_ts": self.now_iso(),
            "last_event_id": self.last_event_id,
            "config": self.config.dump(),
            "stations": self.stations(),
            "teams": self.teams(),
            "members": [_pick(m, _MEMBER_KEYS) for m in self.members()],
            "vehicles": [_pick(v, _VEHICLE_KEYS) for v in self.vehicles()],
            "water_sources": water_sources,
            "incidents": incidents,
            "assignments": [_pick(a, _ASSIGNMENT_KEYS) for a in self.assignments()],
            "road_restrictions": [_pick(r, _RESTRICTION_KEYS) for r in self.restrictions("all")],
            "reporter_links": self.reporter_links(),
            "event_log": [_log_entry(self, row) for row in self.event_log(50)],
            "stats_today": self.stats_today(),
        }


