"""Water load: discrete levels, manual readings, pump-based estimate (docs/siren.md §5.4).

Levels come from config (water_level.options, 0..4) and are always an estimate unless a
human confirmed them (L3): the UI shows the age of the last confirmation. Auto-estimate
only runs while the truck is actually pumping (status IsiUlang / OnScene) and is always
corrected by the next manual reading.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .timeutil import minutes_between, now_iso

PUMPING_STATUSES = {"IsiUlang", "OnScene"}


def estimated_level(config: Config, vehicle: dict, last_level: int, confirmed_at: str | None, now: str) -> int:
    """Level after pumping since the last confirmed reading, clamped to the config range.

    consumed = pump_flow_lpm * minutes_since; a discrete level step (one quarter of the
    tank) only drops once that step is actually consumed: drop = floor(consumed / tank * hi).
    So 400 L out of a 4000 L tank does not pretend a quarter is gone.
    """
    lo, hi = config.min_level(), config.max_level()
    if confirmed_at is None or not config.get("water.auto_estimate", False):
        return last_level
    minutes = minutes_between(now, confirmed_at)
    if minutes <= 0:
        return last_level
    flow = float(vehicle.get("pump_flow_lpm") or config.get("water.default_pump_flow_lpm", 800))
    tank = float(vehicle.get("tank_capacity_l") or config.get("water.tank_capacity_l", 4000))
    if tank <= 0:
        return last_level
    consumed = flow * minutes
    drop = int(consumed / tank * hi)
    return max(lo, min(last_level, last_level - drop))


def tick(config: Config, store: Any, vehicles: list[dict] | None = None, now: str | None = None) -> list[dict]:
    """Insert an 'estimate' reading when pumping has lowered a vehicle's level.

    Returns the vehicle rows whose estimate changed, so the API can emit
    vehicle.water_level with source "estimate". Manual readings always win: this never
    invents a level above the last confirmed one.
    """
    now = now or store.now_iso()
    changed: list[dict] = []
    rows = vehicles if vehicles is not None else store.vehicles()
    for v in rows:
        if v["status"] not in PUMPING_STATUSES:
            continue
        state = store.water_state(v["id"])
        est = estimated_level(config, v, state["level"], state["confirmed_at"], now)
        if est >= state["level"]:
            continue
        changed.append(store.record_water_level(v["id"], est, "estimate"))
    return changed
