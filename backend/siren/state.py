"""Dispatch state machine: status transitions plus incident/vehicle cascades.

Transition tables come from contracts/config.yaml (transitions.incident / .member /
.vehicle), so the allowed set is data, not code (D6). Anything not listed raises
TransitionError with the allowed list, which the API turns into HTTP 409.

Member duty status is the authoritative signal on the ground: the incident status is the
strongest stage among its active assignments, and the vehicle status follows the members
riding it. IsiUlang is a sub-state of EnRoute (the truck is still heading somewhere).
"""

from __future__ import annotations

from typing import Any

from .config import Config

INCIDENT = "incident"
MEMBER = "member"
VEHICLE = "vehicle"

TERMINAL_INCIDENT = {"Selesai", "Dibatalkan"}
ACTIVE_ASSIGNMENT = {"Dispatched", "EnRoute", "OnScene", "Returning"}
CLOSED_ASSIGNMENT = {"Completed", "Withdrawn"}

# member duty status -> incident status / assignment status
MEMBER_TO_INCIDENT = {
    "Dispatched": "Dispatched",
    "EnRoute": "EnRoute",
    "IsiUlang": "EnRoute",
    "OnScene": "OnScene",
    "Returning": "Returning",
}
MEMBER_TO_ASSIGNMENT = MEMBER_TO_INCIDENT
MEMBER_TO_VEHICLE = {
    "Dispatched": "Dispatched",
    "EnRoute": "EnRoute",
    "IsiUlang": "IsiUlang",
    "OnScene": "OnScene",
    "Returning": "Returning",
}
INCIDENT_RANK = {"Baru": 0, "Dispatched": 1, "EnRoute": 2, "OnScene": 3, "Returning": 4}
VEHICLE_RANK = {"Dispatched": 1, "EnRoute": 2, "IsiUlang": 2, "OnScene": 3, "Returning": 4}

# statuses that mean the member is no longer working a task
RELEASE_STATUSES = {"Standby", "Istirahat", "Offline"}

TIMESTAMP_FIELD = {
    "Dispatched": "dispatched_at",
    "OnScene": "arrived_at",
    "Selesai": "finished_at",
    "Dibatalkan": "cancelled_at",
}


class TransitionError(Exception):
    def __init__(self, kind: str, current: str, target: str, allowed: list[str]) -> None:
        super().__init__(f"{kind} status {current} -> {target} is not allowed")
        self.kind = kind
        self.current = current
        self.target = target
        self.allowed = allowed


def check(config: Config, kind: str, current: str, target: str) -> str:
    """Same status is a no-op; anything outside the config table is rejected."""
    config.validate(kind + "_status", current)
    config.validate(kind + "_status", target)
    if target == current:
        return target
    if not config.can_transition(kind, current, target):
        raise TransitionError(kind, current, target, config.allowed_transitions(kind, current))
    return target


def strongest(statuses: list[str], ranks: dict[str, int]) -> str | None:
    known = [s for s in statuses if s in ranks]
    if not known:
        return None
    return max(known, key=lambda s: ranks[s])


def recompute_incident(config: Config, store: Any, incident_id: str) -> dict | None:
    """Move an incident to the stage its active members actually report.

    No active assignments left -> Selesai if it already reached the scene, otherwise Baru
    (nothing is dispatched anymore). Terminal statuses are never touched here.
    """
    inc = store.incident(incident_id)
    if inc["status"] in TERMINAL_INCIDENT:
        return None
    active = [a for a in store.assignments(incident_id) if a["status"] in ACTIVE_ASSIGNMENT]
    duty = [MEMBER_TO_INCIDENT.get(store.member(a["member_id"])["status"]) for a in active]
    target = strongest(duty, INCIDENT_RANK)
    if target is None:
        target = "Selesai" if inc["status"] in {"OnScene", "Returning"} else "Baru"
    if target == inc["status"]:
        return None
    check(config, INCIDENT, inc["status"], target)
    data: dict[str, Any] = {"status": target}
    field = TIMESTAMP_FIELD.get(target)
    if field and not inc.get(field):
        data[field] = store.now_iso()
    return store.update_incident(incident_id, data)


def recompute_vehicle(config: Config, store: Any, vehicle_id: str) -> dict | None:
    """Vehicle status follows the members riding it; no active members -> Standby."""
    v = store.vehicle(vehicle_id)
    if v["status"] == "Offline":
        return None
    active = [
        a
        for a in store.assignments()
        if a["vehicle_id"] == vehicle_id and a["status"] in ACTIVE_ASSIGNMENT
    ]
    duty = [MEMBER_TO_VEHICLE.get(store.member(a["member_id"])["status"]) for a in active]
    target = strongest(duty, VEHICLE_RANK) or "Standby"
    if target == v["status"]:
        return None
    check(config, VEHICLE, v["status"], target)
    data: dict[str, Any] = {"status": target, "updated_at": store.now_iso()}
    if target == "Standby":
        data["eta_minutes"] = None
        data["lat"] = None
        data["lon"] = None
    return store.update_vehicle(vehicle_id, data)


def apply_member_status(config: Config, store: Any, member_id: str, target: str) -> dict[str, Any]:
    """Apply a duty status change and cascade assignments, vehicles, and incidents.

    Returns {"member", "assignments", "withdrawn", "vehicles", "incidents"} so the API can
    emit exactly the events contracts/events.md promises (member.status, assignment.updated,
    incident.updated).
    """
    current = store.member(member_id)
    check(config, MEMBER, current["status"], target)
    member = store.update_member(member_id, {"status": target, "updated_at": store.now_iso()})

    active = [
        a for a in store.assignments() if a["member_id"] == member_id and a["status"] in ACTIVE_ASSIGNMENT
    ]
    withdrawn: list[dict] = []
    touched_incidents: set[str] = set()
    touched_vehicles: set[str] = set()

    if target in RELEASE_STATUSES:
        for a in active:
            withdrawn.append(store.withdraw_assignment(a["id"]))
            touched_incidents.add(a["incident_id"])
            if a["vehicle_id"]:
                touched_vehicles.add(a["vehicle_id"])
    else:
        mapped = MEMBER_TO_ASSIGNMENT.get(target)
        for a in active:
            if mapped and a["status"] != mapped:
                store.update_assignment(a["id"], {"status": mapped, "updated_at": store.now_iso()})
            touched_incidents.add(a["incident_id"])
            if a["vehicle_id"]:
                touched_vehicles.add(a["vehicle_id"])

    incidents = [recompute_incident(config, store, i) for i in sorted(touched_incidents)]
    vehicles = [recompute_vehicle(config, store, v) for v in sorted(touched_vehicles)]
    assignments = [
        a for a in store.assignments() if a["member_id"] == member_id and a["status"] in ACTIVE_ASSIGNMENT
    ]
    return {
        "member": member,
        "assignments": assignments,
        "withdrawn": withdrawn,
        "incidents": [i for i in incidents if i],
        "vehicles": [v for v in vehicles if v],
    }
