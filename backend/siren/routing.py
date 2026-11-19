"""Route building: Valhalla truck costing (D1, D4) plus a straight-line estimate fallback.

The vehicle profile comes from the assigned vehicle, never from a user choice (D4).
Verified road restrictions are sent as exclude_locations / exclude_polygons (§5.2).
When Valhalla is not reachable (dev laptop without Docker), `estimate` mode computes
haversine x detour_factor at an average speed and flags restrictions it cannot avoid —
the numbers are honest estimates, not real truck routing.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config
from .geo import haversine_km, position
from .restrictions import crossing, selection, valhalla_exclusions
from .store import NotFound

# vehicle type -> Valhalla costing name
COSTING_BY_TYPE = {"truck": "truck", "motorbike": "motorcycle", "patrol_car": "auto"}


class RoutingError(Exception):
    """Routing failed (unknown vehicle, Valhalla error, no position known)."""


def _leg(a: tuple[float, float], b: tuple[float, float], km: float, minutes: float, geometry: list[list[float]] | None = None, source: str = "estimate") -> dict:
    return {
        "from": {"lat": round(a[0], 6), "lon": round(a[1], 6)},
        "to": {"lat": round(b[0], 6), "lon": round(b[1], 6)},
        "km": round(km, 1),
        "minutes": round(minutes, 1),
        "geometry": geometry or [[round(a[0], 6), round(a[1], 6)], [round(b[0], 6), round(b[1], 6)]],
        "source": source,
    }


def estimate_leg(config: Config, a: tuple[float, float], b: tuple[float, float]) -> dict:
    km = haversine_km(a, b) * float(config.get("routing.fallback.detour_factor", 1.3))
    speed = float(config.get("routing.fallback.avg_speed_kmh", 25)) or 25.0
    minutes = max(float(config.get("routing.fallback.min_minutes", 1)), km / speed * 60.0)
    return _leg(a, b, km, minutes)


def costing_for(config: Config, vehicle: dict, profile: dict | None) -> tuple[str, dict]:
    """(costing name, costing options) mapped from VehicleProfile — the D4 lock."""
    name = COSTING_BY_TYPE.get(vehicle.get("vehicle_type", "truck"), str(config.get("routing.costing", "truck")))
    options: dict[str, Any] = {}
    if profile:
        options.update(profile.get("valhalla_costing") or {})
        for key, col in (
            ("height", "height_m"),
            ("width", "width_m"),
            ("length", "length_m"),
            ("weight", "gross_weight_kg"),
            ("axle_load", "axle_load_kg"),
        ):
            if profile.get(col) is not None:
                options.setdefault(key, profile[col])
    options.setdefault("type", vehicle.get("vehicle_type", "truck"))
    return name, options


def access_point(source: dict, from_pt: tuple[float, float]) -> tuple[float, float]:
    """The reachable access point closest to `from_pt` (a source may have several)."""
    pts = source.get("access_points") or []
    if not pts:
        return (float(source["lat"]), float(source["lon"]))
    return min((position(p) for p in pts), key=lambda q: haversine_km(from_pt, q))


def _polyline(trip: dict) -> list[tuple[float, float]]:
    """Concatenate maneuver shapes (Valhalla shape strings are 'lat,lon,lat,lon,...')."""
    pts: list[tuple[float, float]] = []
    for leg in trip.get("legs") or []:
        for mv in leg.get("maneuvers") or []:
            shape = mv.get("shape")
            if not shape:
                continue
            nums = [float(x) for x in shape.split(",")]
            for i in range(0, len(nums) - 1, 2):
                pts.append((nums[i], nums[i + 1]))
    return pts


def _slice_geometry(poly: list[tuple[float, float]], start_km: float, end_km: float) -> list[list[float]]:
    """Sub-polyline covering [start_km, end_km] along `poly`, endpoints always included."""
    if len(poly) < 2:
        return [[poly[0][0], poly[0][1]], [poly[-1][0], poly[-1][1]]] if poly else []
    cum = [0.0]
    for p, q in zip(poly, poly[1:]):
        cum.append(cum[-1] + haversine_km(p, q))
    out = [[poly[0][0], poly[0][1]]]
    for i, (lat, lon) in enumerate(poly[1:], start=1):
        if start_km <= cum[i] <= end_km:
            out.append([lat, lon])
    out.append([poly[-1][0], poly[-1][1]])
    return out


class ValhallaClient:
    """POST /route on a self-hosted Valhalla. One call per route (waypoints in order)."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def route(self, points: list[tuple[float, float]], costing: str, options: dict, exclusions: dict) -> list[dict]:
        body: dict[str, Any] = {
            "locations": [{"lat": p[0], "lon": p[1]} for p in points],
            "costing": costing,
            "costing_options": {costing: options},
            "format": "json",
        }
        if exclusions.get("exclude_locations"):
            body["exclude_locations"] = exclusions["exclude_locations"]
        if exclusions.get("exclude_polygons"):
            body["exclude_polygons"] = exclusions["exclude_polygons"]
        try:
            resp = httpx.post(f"{self.url}/route", json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise RoutingError(f"Valhalla tidak dapat dihubungi ({exc})") from exc
        if resp.status_code != 200:
            raise RoutingError(f"Valhalla HTTP {resp.status_code}: {resp.text[:200]}")
        trip = resp.json().get("trip") or {}
        summary = trip.get("summary") or {}
        legs = summary.get("legs") or []
        if not legs:
            km = float(summary.get("distance", 0)) / 1000.0
            minutes = float(summary.get("time", 0)) / 60.0
            if km <= 0:
                raise RoutingError("Valhalla returned an empty route")
            poly = _polyline(trip)
            geom = _slice_geometry(poly, 0.0, sum(haversine_km(p, q) for p, q in zip(poly, poly[1:]))) if poly else None
            return [_leg(points[0], points[-1], km, minutes, geom, "valhalla")]

        poly = _polyline(trip)
        dists = [float(l.get("distance", 0)) / 1000.0 for l in legs]
        times = [float(l.get("time", 0)) / 60.0 for l in legs]
        poly_len = sum(haversine_km(p, q) for p, q in zip(poly, poly[1:])) or 1.0
        route_len = sum(dists) or 1.0
        out: list[dict] = []
        acc = 0.0
        for i, (d, t) in enumerate(zip(dists, times)):
            start = acc / route_len * poly_len
            end = (acc + d) / route_len * poly_len
            acc += d
            geom = _slice_geometry(poly, start, end) if poly else None
            out.append(_leg(points[i], points[i + 1], d, t, geom, "valhalla"))
        return out


def build_route(
    config: Config,
    store: Any,
    vehicle_id: str,
    to: dict,
    via_water_source_id: str | None = None,
    restriction_ids: list[str] | None = None,
    provider: str | None = None,
) -> dict:
    """Route for a vehicle, optionally via a water source, with restrictions applied.

    Returns the Route shape from contracts/openapi.yaml (legs, totals, applied restrictions,
    provider, warnings). ETA is in minutes; refill time is folded into total_minutes when the
    tank is low and the route goes through a source (siren.md §5.3).
    """
    veh = store.vehicle(vehicle_id)
    profile = None
    if veh.get("profile_id"):
        try:
            profile = store.vehicle_profile(veh["profile_id"])
        except NotFound:
            profile = None
    start = store.vehicle_position(vehicle_id) or store.station_position()
    if start is None:
        raise RoutingError("posisi kendaraan dan pos damkar tidak diketahui")
    dest = position(to)
    via_pt = access_point(store.water_source(via_water_source_id), start) if via_water_source_id else None

    restrictions = selection(config, store, veh.get("vehicle_type", "truck"), restriction_ids)
    exclusions = valhalla_exclusions(restrictions)
    points = [start] + ([via_pt] if via_pt else []) + [dest]

    mode = provider or str(config.get("routing.provider", "estimate"))
    warnings: list[str] = []
    legs: list[dict] = []

    if mode == "valhalla":
        costing, options = costing_for(config, veh, profile)
        try:
            legs = ValhallaClient(str(config.get("routing.valhalla_url", "http://127.0.0.1:8081"))).route(
                points, costing, options, exclusions
            )
        except RoutingError as exc:
            mode = "estimate"
            warnings.append(f"Valhalla belum tersedia — pakai estimasi: {exc}")

    if mode != "valhalla":
        legs = []
        penalty = float(config.get("routing.fallback.blocked_penalty", 1.6))
        for a, b in zip(points, points[1:]):
            leg = estimate_leg(config, a, b)
            hits = crossing(restrictions, a, b)
            if hits:
                leg["minutes"] = round(leg["minutes"] * penalty, 1)
                warnings.append(
                    "estimasi melewati pembatasan jalan (" + ", ".join(h["id"] for h in hits) + ")"
                )
            legs.append(leg)

    km = sum(l["km"] for l in legs)
    minutes = sum(l["minutes"] for l in legs)
    refill = 0.0
    if via_pt:
        state = store.water_state(vehicle_id)
        if state["level"] <= float(config.get("water.low_level_threshold", 1)):
            refill = float(config.get("water.refill_minutes", 3))

    return {
        "vehicle_id": vehicle_id,
        "profile_id": veh.get("profile_id"),
        "legs": legs,
        "total_km": round(km, 1),
        "total_minutes": round(minutes + refill, 1),
        "refill_minutes": refill,
        "applied_restrictions": [r["id"] for r in restrictions],
        "provider": mode if legs else "estimate",
        "warnings": warnings,
        "waypoints": [{"lat": round(p[0], 6), "lon": round(p[1], 6)} for p in points],
    }


def water_options(config: Config, store: Any, vehicle_id: str, incident_id: str) -> dict:
    """Top-N water-source recommendation via a routing matrix (siren.md §5.3).

    A = vehicle -> access point, B = access point -> incident. Tank sufficient: score = B.
    Tank low: score = A + refill_minutes + B and refill-first is recommended. Raw km and
    minutes are always returned so the operator can see why an option won.
    """
    veh = store.vehicle(vehicle_id)
    inc = store.incident(incident_id)
    state = store.water_state(vehicle_id)
    refill_minutes = float(config.get("water.refill_minutes", 3))
    refill_required = state["level"] <= float(config.get("water.low_level_threshold", 1))

    options: list[dict] = []
    for ws in store.water_sources(status="active"):
        route = build_route(config, store, vehicle_id, {"lat": inc["lat"], "lon": inc["lon"]}, ws["id"])
        if len(route["legs"]) < 2:
            continue
        a, b = route["legs"][0], route["legs"][1]
        score = b["minutes"] if not refill_required else a["minutes"] + refill_minutes + b["minutes"]
        options.append(
            {
                "water_source_id": ws["id"],
                "name": ws["name"],
                "type": ws["type"],
                "status": ws["status"],
                "a": a,
                "b": b,
                "score_minutes": round(score, 1),
                "refill_required": refill_required,
                "refill_minutes": refill_minutes if refill_required else 0.0,
                "provider": route["provider"],
                "warnings": route["warnings"],
            }
        )

    options.sort(key=lambda o: (o["score_minutes"], o["b"]["km"]))
    limit = int(config.get("routing.max_options", 3))
    ranked = [{**o, "rank": i + 1} for i, o in enumerate(options[:limit])]
    return {
        "vehicle_id": vehicle_id,
        "incident_id": incident_id,
        "water_level": state["level"],
        "refill_required": refill_required,
        "refill_minutes": refill_minutes if refill_required else 0.0,
        "options": ranked,
    }
