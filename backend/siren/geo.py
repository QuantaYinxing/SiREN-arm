"""Geometry helpers: haversine distance and polygon/line math.

Positions are (lat, lon). Route distances come from Valhalla; haversine is only used for
the straight-line estimate fallback, freshness-independent sanity checks, and polygon
containment for road restrictions.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Iterable, Sequence

EARTH_RADIUS_KM = 6371.0088

Point = tuple[float, float]  # (lat, lon)
Ring = Sequence[Sequence[float]]  # [[lat, lon], ...]


def position(obj: dict) -> Point:
    """Normalize a {"lat":..,"lon":..} payload to a (lat, lon) tuple."""
    return (float(obj["lat"]), float(obj["lon"]))


def haversine_km(a: Point, b: Point) -> float:
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def polyline_km(points: Sequence[Point]) -> float:
    return sum(haversine_km(p, q) for p, q in zip(points, points[1:]))


def bbox_from_station(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Coverage bbox (min_lat, max_lat, min_lon, max_lon) = station ± radius.

    Latitude degrees ~ 110.574 km; longitude degrees shrink with cos(latitude)
    (equirectangular approximation, good enough at city scale)."""
    dlat = radius_km / 110.574
    dlon = radius_km / (111.320 * cos(radians(lat)))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def bbox(ring: Ring) -> tuple[float, float, float, float]:
    lats = [float(p[0]) for p in ring]
    lons = [float(p[1]) for p in ring]
    return min(lats), max(lats), min(lons), max(lons)


def point_in_ring(lat: float, lon: float, ring: Ring) -> bool:
    """Ray casting on a closed ring. Returns False for degenerate rings."""
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) < 3:
        return False
    minlat, maxlat, minlon, maxlon = bbox(pts)
    if not (minlat <= lat <= maxlat and minlon <= lon <= maxlon):
        return False
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if (yi > lat) != (yj > lat):
            x_cross = xj + (lat - yj) * (xi - xj) / (yi - yj)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_to_segment_km(lat: float, lon: float, a: Point, b: Point) -> float:
    """Distance from a point to a segment, in km (equirectangular approximation)."""
    x0, y0 = lon, lat
    x1, y1 = a[1], a[0]
    x2, y2 = b[1], b[0]
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return haversine_km((lat, lon), a)
    t = ((x0 - x1) * dx + (y0 - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    px, py = x1 + t * dx, y1 + t * dy
    return haversine_km((lat, lon), (py, px))


def distance_to_line_km(lat: float, lon: float, line: Ring) -> float:
    pts = [(float(p[0]), float(p[1])) for p in line]
    if len(pts) < 2:
        return haversine_km((lat, lon), (float(pts[0][0]), float(pts[0][1])))
    return min(point_to_segment_km(lat, lon, p, q) for p, q in zip(pts, pts[1:]))


def normalize_geometry(obj: dict) -> dict:
    """{"kind": "point|line|polygon", "coordinates": [[lat, lon], ...]}"""
    kind = obj["kind"]
    coords = [[float(p[0]), float(p[1])] for p in obj["coordinates"]]
    if kind == "point" and len(coords) != 1:
        coords = coords[:1]
    if kind == "line" and len(coords) < 2:
        raise ValueError("line geometry needs at least two coordinates")
    if kind == "polygon" and len(coords) < 3:
        raise ValueError("polygon geometry needs at least three coordinates")
    return {"kind": kind, "coordinates": coords}


def affects(restriction: dict, vehicle_type: str) -> bool:
    """Does this restriction apply to a vehicle type? `all` covers everything."""
    affected = restriction.get("affected_vehicle_type", "truck")
    return affected == "all" or affected == vehicle_type


def is_expired(restriction: dict, now_iso: str) -> bool:
    expires = restriction.get("expires_at")
    return bool(expires) and expires <= now_iso


def blocks(restriction: dict, lat: float, lon: float, line_width_km: float = 0.4) -> bool:
    """Does this restriction geometry cover a point (used by the estimate fallback)?"""
    geom = restriction["geometry"]
    if geom["kind"] == "point":
        return haversine_km((lat, lon), (geom["coordinates"][0][0], geom["coordinates"][0][1])) <= line_width_km
    if geom["kind"] == "polygon":
        return point_in_ring(lat, lon, geom["coordinates"])
    return distance_to_line_km(lat, lon, geom["coordinates"]) <= line_width_km
