"""Synthetic scenario driver — game mode (docs/siren.md §9, local prototype).

Game mode runs the same domain code as real mode (state.py transitions, routing estimate,
store pings, texts for the event log) with seeded randomness and a virtual clock scaled by
`game.speed`, so the simulator doubles as a deterministic pytest scenario: same seed +
injected clock replays identically without any internet connection.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any, Callable

from siren import geo
from siren import routing as routing_mod
from siren import state as state_mod
from siren import texts
from siren import timeutil

INCIDENT_TIMESTAMPS = {"Dispatched": "dispatched_at", "OnScene": "arrived_at",
                       "Selesai": "finished_at", "Dibatalkan": "cancelled_at"}


class GameClock:
    """Monotonic virtual clock: real seconds scaled by game.speed (1/2/4/8), UTC datetimes."""

    def __init__(self, base: float | None = None, speed: Callable[[], float] | None = None) -> None:
        self._base = base if base is not None else time.time()
        self._real = time.time()
        self._elapsed = 0.0
        self._speed = speed or (lambda: 1.0)

    def __call__(self) -> datetime:
        real = time.time()
        self._elapsed += (real - self._real) * max(0.1, float(self._speed()))
        self._real = real
        return datetime.fromtimestamp(self._base + self._elapsed, timezone.utc)


def _interp(a: tuple[float, float], b: tuple[float, float], f: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


class Simulator:
    """Drives synthetic reporters, officer movement, water consumption and restriction reports."""

    def __init__(self, store: Any, config: Any, hub: Any = None, seed: int | None = None) -> None:
        self.store = store
        self.config = config
        self.hub = hub
        self.rng = random.Random(seed if seed is not None else int(config.get("game.seed", 1)))
        self._next_incident: float | None = None
        self._next_report: float | None = None
        self.ticks = 0
        self._pings: dict[str, float] = {}
        self._crew: dict[str, dict] = {}
        self._station: tuple[float, float] | None = None

    # ---- helpers ----

    def _emit(self, etype: str, payload: dict, text: str, incident_id: str | None = None,
              actor_member_id: str | None = None) -> None:
        envelope = self.store.record_event(etype, payload, text, incident_id=incident_id,
                                           actor_member_id=actor_member_id)
        if self.hub is not None:
            self.hub.broadcast(envelope)

    def _home(self) -> tuple[float, float]:
        if self._station is None:
            st = self.store.stations()[0]
            self._station = (st["lat"], st["lon"])
        return self._station

    def _leg(self, a: tuple[float, float], b: tuple[float, float]) -> dict:
        return routing_mod.estimate_leg(self.store.config, a, b)

    def _advance(self, member_ids: list[str], target: str) -> None:
        """Move members through the state machine; hub mirrors REST-style broadcasts."""
        for mid in member_ids:
            try:
                result = state_mod.apply_member_status(self.store.config, self.store, mid, target)
            except state_mod.TransitionError:
                continue
            if self.hub is not None:
                self.hub._emit_status(result)
            else:
                self.store.record_event("member.status",
                                       {"member_id": mid, "status": target},
                                       texts.member_status(self.store.config, result["member"]),
                                       actor_member_id=mid)

    def _set_incident(self, incident_id: str, status: str) -> None:
        inc = self.store.incident(incident_id)
        state_mod.check(self.store.config, "incident", inc["status"], status)
        data = {"status": status}
        field = INCIDENT_TIMESTAMPS.get(status)
        if field and not inc.get(field):
            data[field] = self.store.now_iso()
        row = self.store.update_incident(incident_id, data)
        self._emit("incident.updated", {"incident_id": incident_id, "status": status, "incident": row},
                   texts.incident_updated(self.store.config, row), incident_id)

    def _plan(self, asg: dict) -> dict | None:
        """Route legs for one assignment: vehicle -> (water source) -> incident, then back home."""
        veh = self.store.vehicle(asg["vehicle_id"]) if asg["vehicle_id"] else None
        inc = self.store.incident(asg["incident_id"])
        home = self._home()
        start = (veh["position"]["lat"], veh["position"]["lon"]) if veh and veh.get("position") else home
        legs: list[tuple[tuple[float, float], tuple[float, float], float]] = []
        if asg["water_source_id"]:
            src = self.store.water_source(asg["water_source_id"])
            ap = routing_mod.access_point(src, start)
            l1 = self._leg(start, ap)
            l2 = self._leg(ap, (inc["lat"], inc["lon"]))
            legs.append((start, ap, l1["minutes"]))
            legs.append((ap, (inc["lat"], inc["lon"]), l2["minutes"]))
        else:
            legs.append((start, (inc["lat"], inc["lon"]), self._leg(start, (inc["lat"], inc["lon"]))["minutes"]))
        now = self.store.now_seconds()
        created = asg.get("created_at")
        depart_at = (timeutil.parse_iso(created).timestamp() if created else now) \
            + float(self.store.config.get("game.departure_delay_minutes", 1)) * 60
        return {
            "stage": "dispatched",
            "depart_at": depart_at,
            "legs": legs,
            "total_min": sum(l[2] for l in legs),
            "pos": start,
            "departed": None,
            "scene_at": None,
            "refill_at": None,
            "drain": 0.0,
            "last_ping": None,
        }

    # ---- ticking ----

    async def run(self) -> None:
        import asyncio

        while True:
            tick = max(0.2, float(self.store.config.get("game.tick_seconds", 1)))
            speed = max(1.0, float(self.store.config.get("game.speed", 1)))
            await asyncio.sleep(tick / speed)
            self.tick(self.store.now_seconds())

    def tick(self, now: float) -> None:
        self.ticks += 1
        cfg = self.store.config
        iv = float(cfg.get("game.incident_interval_minutes", 8)) * 60
        if self._next_incident is None:
            self._next_incident = now + self.rng.uniform(0.3, 1.2) * iv
        if now >= self._next_incident:
            self._next_incident = now + self.rng.uniform(0.3, 1.2) * iv
            self.spawn_now()
        self._standby(cfg, now)
        self._crews(cfg, now)
        self._reports(cfg, now)

    def spawn_now(self) -> dict:
        cfg = self.store.config
        home = self._home()
        lat = home[0] + self.rng.uniform(-0.045, 0.045)
        lon = home[1] + self.rng.uniform(-0.045, 0.045)
        types = cfg.get("game.incident_types") or ["Kebakaran ruko"]
        streets = cfg.get("game.street_names") or ["Jl. Pasar Cikurubuk"]
        reporters = cfg.get("game.reporter_names") or ["Warga"]
        row = self.store.create_incident({
            "type": self.rng.choice(types),
            "address": f"{self.rng.choice(streets)} No. {self.rng.randint(1, 90)}",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "reporter_name": self.rng.choice(reporters),
        })
        self._emit("incident.created", {"incident": row},
                   texts.incident_created(self.store.config, row), row["id"])
        return row

    def _standby(self, cfg: Any, now: float) -> None:
        """Standby officers keep pinging at standby rate; some pings are skipped so freshness
        goes stale/offline on its own (the GPS limitation from docs/siren.md §6.1, simulated)."""
        skip = float(cfg.get("game.stale_skip_probability", 0.06))
        interval = float(cfg.get("position.ping_standby_seconds", 300))
        jitter = float(cfg.get("game.standby_jitter_deg", 0.004))
        home = self._home()
        for m in self.store.members():
            if m["status"] not in ("Standby", "Istirahat"):
                continue
            nxt = self._pings.get(m["id"])
            if nxt is None:
                self._pings[m["id"]] = now + self.rng.uniform(0.6, 1.4) * interval
                continue
            if now < nxt:
                continue
            self._pings[m["id"]] = now + self.rng.uniform(0.6, 1.4) * interval
            if self.rng.random() < skip:
                continue
            lat = home[0] + self.rng.uniform(-jitter, jitter)
            lon = home[1] + self.rng.uniform(-jitter, jitter)
            result = self.store.record_ping(m["id"], lat, lon, 8, None, None)
            if self.hub is not None:
                self.hub.broadcast(self.hub._envelope("member.position", {
                    "member_id": m["id"], "vehicle_id": None, "position": result["position"],
                    "freshness": result["freshness"],
                    "position_age_seconds": result["position_age_seconds"],
                }))

    def _crews(self, cfg: Any, now: float) -> None:
        for a in self.store.assignments():
            if a["status"] not in state_mod.ACTIVE_ASSIGNMENT:
                continue
            crew = self._crew.get(a["id"])
            if crew is None:
                crew = self._plan(a)
                if crew is None:
                    continue
                self._crew[a["id"]] = crew
            stage = crew["stage"]
            if stage == "dispatched":
                if now >= crew["depart_at"]:
                    self._advance([a["member_id"]], "EnRoute")
                    crew["stage"] = "enroute"
                    crew["departed"] = now
                    crew["pos"] = crew["legs"][0][0]
                    self._ping_crew(a, crew, crew["pos"], now)
            elif stage == "enroute":
                self._move(a, crew, now)
            elif stage == "onscene":
                self._scene(a, crew, now)
            elif stage == "refill":
                self._refill(a, crew, now)
            elif stage == "returning":
                self._move(a, crew, now)
            elif stage == "done":
                self._crew.pop(a["id"], None)

    def _move(self, asg: dict, crew: dict, now: float) -> None:
        frac = min(1.0, (now - crew["departed"]) / (crew["total_min"] * 60))
        rem = frac * crew["total_min"]
        pos = crew["legs"][-1][1]
        for leg_a, leg_b, minutes in crew["legs"]:
            if rem >= minutes:
                rem -= minutes
                pos = leg_b
            else:
                pos = _interp(leg_a, leg_b, rem / minutes if minutes else 0.0)
                break
        crew["pos"] = pos
        if asg["vehicle_id"]:
            self.store.update_vehicle(asg["vehicle_id"], {"lat": pos[0], "lon": pos[1]})
        self._ping_crew(asg, crew, pos, now)
        if frac >= 1.0:
            if crew["stage"] == "enroute":
                self._advance(self._crew_members(asg), "OnScene")
                self._set_incident(asg["incident_id"], "OnScene")
                crew["stage"] = "onscene"
                crew["scene_at"] = now
                crew["drain"] = 0.0
            else:
                self._finish(asg, crew)

    def _ping_crew(self, asg: dict, crew: dict, pos: tuple[float, float], now: float) -> None:
        """Crew pings drive freshness and the vehicle odometer (km_today sums pings)."""
        live = float(self.store.config.get("position.ping_live_seconds", 5))
        if crew["last_ping"] is not None and now - crew["last_ping"] < live:
            return
        crew["last_ping"] = now
        for mid in self._crew_members(asg):
            result = self.store.record_ping(mid, pos[0], pos[1], 8, asg["vehicle_id"], None)
            if self.hub is not None:
                self.hub.broadcast(self.hub._envelope("member.position", {
                    "member_id": mid, "vehicle_id": asg["vehicle_id"], "position": result["position"],
                    "freshness": result["freshness"],
                    "position_age_seconds": result["position_age_seconds"],
                }))

    def _crew_members(self, asg: dict) -> list[str]:
        """All members on the same incident share the truck; movement is one crew."""
        return [a["member_id"] for a in self.store.assignments(asg["incident_id"])
                if a["status"] in state_mod.ACTIVE_ASSIGNMENT]

    def _scene(self, asg: dict, crew: dict, now: float) -> None:
        cfg = self.store.config
        elapsed_min = (now - crew["scene_at"]) / 60
        drain = crew["drain"] + elapsed_min * float(cfg.get("game.consume_per_minute", 0.3))
        veh = self.store.vehicle(asg["vehicle_id"]) if asg["vehicle_id"] else None
        if veh:
            low = int(cfg.get("water.low_level_threshold", 1))
            new_level = max(0, int(veh["water_level"]) - int(drain))
            if new_level != veh["water_level"]:
                crew["drain"] = 0.0
                row = self.store.record_water_level(asg["vehicle_id"], new_level, "estimate")
                self._emit("vehicle.water_level",
                           {"vehicle_id": asg["vehicle_id"], "level": new_level, "source": "estimate"},
                           texts.water_level(cfg, row["name"], new_level, "estimate"), asg["incident_id"])
            level_now = new_level
        else:
            level_now = 0
        if elapsed_min >= float(cfg.get("game.scene_minutes", 12)):
            if veh and level_now <= int(cfg.get("water.low_level_threshold", 1)) and asg["water_source_id"]:
                self._advance(self._crew_members(asg), "IsiUlang")
                src = self.store.water_source(asg["water_source_id"])
                ap = routing_mod.access_point(src, crew["pos"])
                leg = self._leg(crew["pos"], ap)
                crew["legs"] = [(crew["pos"], ap, leg["minutes"])]
                crew["total_min"] = leg["minutes"]
                crew["departed"] = now
                crew["refill_at"] = now + float(cfg.get("water.refill_minutes", 3)) * 60
                crew["stage"] = "refill"
            else:
                self._start_return(asg, crew, now)

    def _refill(self, asg: dict, crew: dict, now: float) -> None:
        if now < crew["refill_at"]:
            return
        if asg["vehicle_id"]:
            row = self.store.refill_done(asg["vehicle_id"], True)
            self._emit("vehicle.water_level",
                       {"vehicle_id": asg["vehicle_id"], "level": row["water_level"],
                        "source": "officer", "confirmed_at": self.store.now_iso()},
                       texts.refill_done(self.store.config, row["name"]), asg["incident_id"])
        self._start_return(asg, crew, now)

    def _start_return(self, asg: dict, crew: dict, now: float) -> None:
        self._advance(self._crew_members(asg), "Returning")
        self._set_incident(asg["incident_id"], "Returning")
        home = self._home()
        leg = self._leg(crew["pos"], home)
        crew["legs"] = [(crew["pos"], home, leg["minutes"])]
        crew["total_min"] = leg["minutes"]
        crew["departed"] = now
        crew["stage"] = "returning"

    def _finish(self, asg: dict, crew: dict) -> None:
        self._advance(self._crew_members(asg), "Standby")
        self._set_incident(asg["incident_id"], "Selesai")
        if asg["vehicle_id"]:
            state_mod.recompute_vehicle(self.store.config, self.store, asg["vehicle_id"])
            self.store.update_vehicle(asg["vehicle_id"], {"eta_minutes": None})
        crew["stage"] = "done"

    def _reports(self, cfg: Any, now: float) -> None:
        """Officers occasionally report a road that the truck cannot use (pending, needs verify)."""
        p = float(cfg.get("game.restriction_report_probability", 0.02))
        crew = next((c for c in self._crew.values() if c["stage"] == "enroute"), None)
        if crew is None or self.rng.random() > p:
            return
        asg = next((a for a in self.store.assignments()
                    if a["id"] in self._crew and self._crew[a["id"]] is crew), None)
        if asg is None:
            return
        member = self.store.member(asg["member_id"])
        row = self.store.create_restriction({
            "kind": "point",
            "geometry": geo.normalize_geometry({"kind": "point",
                                                "coordinates": [[crew["pos"][0], crew["pos"][1]]]}),
            "reason": "narrow",
            "affected_vehicle_type": "truck",
            "status": "pending",
            "reported_by": member["name"],
            "reported_member_id": member["id"],
            "note": "laporan sintetis game mode",
        })
        self._emit("restriction.reported", {"restriction": row},
                   texts.restriction_reported(cfg, member["name"], row["reason"]))
