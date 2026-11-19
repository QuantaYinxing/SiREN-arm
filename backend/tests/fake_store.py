"""In-memory duck-typed Store for domain tests.

Implements exactly the methods siren.state / siren.routing / siren.water call, so domain
logic can be tested without SQLite or the API layer. Same semantics as the real Store:
withdrawal does not touch member status, timestamps are ISO strings from the injected clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from siren.config import Config
from siren.timeutil import age_seconds, now_iso


class FakeStore:
    def __init__(self, config: Config, now: datetime | None = None) -> None:
        self.config = config
        self._now = now or datetime(2026, 11, 19, 7, 32, 0, tzinfo=timezone.utc)
        self.members: dict[str, dict] = {}
        self._vehicles: dict[str, dict] = {}
        self.profiles: dict[str, dict] = {}
        self.incidents: dict[str, dict] = {}
        self._assignments: list[dict] = []
        self._water_sources: dict[str, dict] = {}
        self._restrictions: list[dict] = []
        self._stations: list[dict] = []
        self.readings: dict[str, tuple[int, str, str]] = {}  # vehicle_id -> (level, source, iso)
        self.pings: dict[str, list[dict]] = {}
        self._seq = 0

    # ---- clock ----

    def now_iso(self) -> str:
        return now_iso(self._now)

    def advance(self, minutes: float) -> str:
        self._now = self._now + timedelta(minutes=minutes)
        return self.now_iso()

    def _nid(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    # ---- setup helpers used by tests ----

    def add_member(self, id_: str, **kw) -> dict:
        row = {"id": id_, "name": kw.get("name", id_), "role": kw.get("role"), "team_id": kw["team_id"],
               "vehicle_id": kw.get("vehicle_id"), "status": kw.get("status", "Standby"), "updated_at": None}
        self.members[id_] = row
        return row

    def add_vehicle(self, id_: str, **kw) -> dict:
        row = {"id": id_, "name": kw.get("name", id_), "plate": kw.get("plate"), "vehicle_type": kw.get("vehicle_type", "truck"),
               "profile_id": kw.get("profile_id"), "team_id": kw.get("team_id"), "status": kw.get("status", "Standby"),
               "lat": kw.get("lat"), "lon": kw.get("lon"), "eta_minutes": kw.get("eta_minutes"),
               "pump_flow_lpm": kw.get("pump_flow_lpm"), "tank_capacity_l": kw.get("tank_capacity_l"), "updated_at": None}
        self._vehicles[id_] = row
        return row

    def add_profile(self, id_: str, **kw) -> dict:
        self.profiles[id_] = {"id": id_, "name": kw.get("name", id_), "vehicle_type": kw.get("vehicle_type", "truck"),
                              "height_m": kw.get("height_m"), "width_m": kw.get("width_m"), "length_m": kw.get("length_m"),
                              "gross_weight_kg": kw.get("gross_weight_kg"), "axle_load_kg": kw.get("axle_load_kg"),
                              "valhalla_costing": kw.get("valhalla_costing", {})}
        return self.profiles[id_]

    def add_incident(self, id_: str, lat: float, lon: float, **kw) -> dict:
        row = {"id": id_, "type": kw.get("type", "Kebakaran ruko"), "address": kw.get("address"), "description": None,
               "reporter_name": kw.get("reporter_name"), "reporter_link_id": kw.get("reporter_link_id"),
               "status": kw.get("status", "Baru"), "lat": lat, "lon": lon,
               "created_at": kw.get("created_at", self.now_iso()), "dispatched_at": kw.get("dispatched_at"),
               "arrived_at": kw.get("arrived_at"), "finished_at": kw.get("finished_at"), "cancelled_at": kw.get("cancelled_at")}
        self.incidents[id_] = row
        return row

    def add_water_source(self, id_: str, lat: float, lon: float, **kw) -> dict:
        self._water_sources[id_] = {"id": id_, "name": kw.get("name", id_), "type": kw.get("type", "hydrant"),
                                   "status": kw.get("status", "active"), "lat": lat, "lon": lon,
                                   "access_points": kw.get("access_points", [{"lat": lat, "lon": lon}]),
                                   "capacity_note": kw.get("capacity_note"), "notes": None}
        return self._water_sources[id_]

    def add_restriction(self, id_: str, kind: str, coordinates: list, **kw) -> dict:
        row = {"id": id_, "kind": kind, "geometry": {"kind": kind, "coordinates": coordinates},
               "reason": kw.get("reason", "flood"), "affected_vehicle_type": kw.get("affected_vehicle_type", "truck"),
               "status": kw.get("status", "verified"), "reported_by": kw.get("reported_by"), "reported_at": self.now_iso(),
               "verified_at": kw.get("verified_at", self.now_iso()), "expires_at": kw.get("expires_at"), "note": kw.get("note")}
        self._restrictions.append(row)
        return row

    def add_station(self, lat: float, lon: float, name: str = "Pos Damkar") -> dict:
        row = {"id": "st1", "name": name, "lat": lat, "lon": lon}
        self._stations.append(row)
        return row

    def assign(self, incident_id: str, member_id: str, vehicle_id: str | None = None,
               water_source_id: str | None = None, status: str = "Dispatched") -> dict:
        row = {"id": self._nid("asg"), "incident_id": incident_id, "member_id": member_id,
               "vehicle_id": vehicle_id, "water_source_id": water_source_id, "role": None,
               "status": status, "created_at": self.now_iso(), "updated_at": None}
        self._assignments.append(row)
        return row

    # ---- store interface ----

    def member(self, id_: str) -> dict:
        return dict(self.members[id_])

    def update_member(self, id_: str, data: dict) -> dict:
        self.members[id_].update(data)
        return dict(self.members[id_])

    def vehicle(self, id_: str) -> dict:
        row = dict(self._vehicles[id_])
        state = self.water_state(id_)
        row.update({"water_level": state["level"], "water_level_source": state["source"],
                    "water_level_confirmed_at": state["confirmed_at"], "estimated": state["estimated"],
                    "km_today": 0.0})
        return row

    def vehicles(self) -> list[dict]:
        return [self.vehicle(v) for v in self._vehicles]

    def update_vehicle(self, id_: str, data: dict) -> dict:
        self._vehicles[id_].update(data)
        return self.vehicle(id_)

    def vehicle_profile(self, id_: str) -> dict:
        return dict(self.profiles[id_])

    def vehicle_position(self, id_: str) -> tuple[float, float] | None:
        v = self._vehicles.get(id_)
        if not v or v.get("lat") is None or v.get("lon") is None:
            return None
        return (float(v["lat"]), float(v["lon"]))

    def station_position(self) -> tuple[float, float] | None:
        if not self._stations:
            return None
        return (float(self._stations[0]["lat"]), float(self._stations[0]["lon"]))

    def stations(self) -> list[dict]:
        return [dict(s) for s in self._stations]

    def incident(self, id_: str) -> dict:
        row = dict(self.incidents[id_])
        row["assignments"] = [dict(a) for a in self._assignments if a["incident_id"] == id_]
        return row

    def update_incident(self, id_: str, data: dict) -> dict:
        self.incidents[id_].update(data)
        return self.incident(id_)

    def assignments(self, incident_id: str | None = None) -> list[dict]:
        rows = [dict(a) for a in self._assignments if incident_id is None or a["incident_id"] == incident_id]
        return rows

    def update_assignment(self, id_: str, data: dict) -> dict:
        for a in self._assignments:
            if a["id"] == id_:
                a.update(data)
                return dict(a)
        raise KeyError(id_)

    def withdraw_assignment(self, id_: str) -> dict:
        return self.update_assignment(id_, {"status": "Withdrawn", "updated_at": self.now_iso()})

    def water_sources(self, status: str = "all") -> list[dict]:
        return [dict(w) for w in self._water_sources.values() if status == "all" or w["status"] == status]

    def water_source(self, id_: str) -> dict:
        return dict(self._water_sources[id_])

    def water_state(self, vehicle_id: str) -> dict:
        rec = self.readings.get(vehicle_id)
        if rec is None:
            return {"level": self.config.min_level(), "source": "estimate",
                    "confirmed_at": None, "estimated": True, "age_seconds": None}
        level, source, iso = rec
        return {"level": level, "source": source, "confirmed_at": iso,
                "estimated": source == "estimate", "age_seconds": age_seconds(self.now_iso(), iso)}

    def record_water_level(self, vehicle_id: str, level: int, source: str) -> dict:
        self.readings[vehicle_id] = (int(level), source, self.now_iso())
        return self.vehicle(vehicle_id)

    def restrictions(self, status: str = "all") -> list[dict]:
        return [dict(r) for r in self._restrictions if status == "all" or r["status"] == status]

    def active_restrictions(self, vehicle_type: str) -> list[dict]:
        from siren.geo import affects

        now = self.now_iso()
        return [
            dict(r)
            for r in self._restrictions
            if r["status"] == "verified"
            and not (r.get("expires_at") and r["expires_at"] <= now)
            and affects(r, vehicle_type)
        ]

    def expire_overdue(self) -> list[dict]:
        now = self.now_iso()
        changed = []
        for r in self._restrictions:
            if r["status"] == "verified" and r.get("expires_at") and r["expires_at"] <= now:
                r["status"] = "expired"
                changed.append(dict(r))
        return changed

    def verify(self, id_: str, status: str) -> dict:
        for r in self._restrictions:
            if r["id"] == id_:
                r["status"] = status
                return dict(r)
        raise KeyError(id_)
