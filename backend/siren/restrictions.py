"""Road restrictions (docs/siren.md §5.2): verification queue -> routing exclusions.

Only `verified` and not-expired restrictions affect routing. Field reports arrive as
`pending` and wait for the operator (who may verify is open question §12.6). Verified
geometry is sent to Valhalla as costing_options exclude_polygons (§5.2); the estimate
fallback cannot honour excludes, so it flags legs that cross a restriction instead.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .geo import blocks, buffer_line

VALID_VERIFY_STATUSES = {"verified", "rejected", "expired"}


def selection(
    config: Config,
    store: Any,
    vehicle_type: str,
    ids: list[str] | None = None,
) -> list[dict]:
    """Restrictions that must be applied: verified, not expired, affecting this vehicle type.

    `ids=None` means "all applicable"; an explicit list narrows to those ids (still only
    verified ones — a caller cannot force an unverified restriction into a route).
    """
    store.expire_overdue()
    rows = store.active_restrictions(vehicle_type)
    if ids is None:
        return rows
    wanted = set(ids)
    return [r for r in rows if r["id"] in wanted]


def valhalla_exclusions(restrictions: list[dict]) -> dict[str, Any]:
    """Map restriction geometry onto Valhalla's costing_options.exclude_polygons.

    point -> small square, polygon -> ring, line -> a buffered polygon (OSM rarely
    carries road width in Indonesia, so the buffer is the honest approximation).
    exclude_locations is not used: this Valhalla build segfaults on it, and the
    squares block edges during routing, which is the point-restriction semantic.
    """
    polygons: list[list[list[float]]] = []
    for r in restrictions:
        geom = r["geometry"]
        coords = geom["coordinates"]
        if geom["kind"] == "point":
            # Valhalla crashes (SIGSEGV in this build) on exclude_locations, so
            # a point restriction is sent as a small square blocking the roads
            # around it (~220 m, matching blocks()'s 0.4 km corridor check).
            lat, lon = coords[0]
            d = 0.002
            polygons.append([[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
                             [lon - d, lat + d], [lon - d, lat - d]])
        else:
            # Valhalla's exclude_polygons is an array of rings, and each ring is
            # GeoJSON [lon, lat] pairs (worker.cc parse_ring); our stored coords
            # are [lat, lon]. Close the ring for safety.
            raw = coords if geom["kind"] == "polygon" else buffer_line(coords)
            ring = [[lon, lat] for lat, lon in raw]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            polygons.append(ring)
    return {"exclude_polygons": polygons}


def sample_leg(a: tuple[float, float], b: tuple[float, float], samples: int = 9) -> list[tuple[float, float]]:
    """Points along a straight segment, including both ends."""
    n = max(2, samples)
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


def crossing(restrictions: list[dict], a: tuple[float, float], b: tuple[float, float], width_km: float = 0.4) -> list[dict]:
    """Restrictions whose geometry sits on the straight line from a to b (estimate mode)."""
    pts = sample_leg(a, b)
    hits: list[dict] = []
    for r in restrictions:
        if any(blocks(r, p[0], p[1], width_km) for p in pts):
            hits.append(r)
    return hits
