import math
from typing import Dict, List, Optional, Sequence, Tuple

EARTH_RADIUS_KM = 6371.0088


def point_in_polygon(lat: float, lng: float, vertices: Sequence[Tuple[float, float]]) -> bool:
    """
    Ray casting: counts how many edges a ray to the east crosses. Odd means in.

    Coordinates are treated as a flat plane. Over a delivery area — tens of
    kilometres at most — the curvature of the earth moves a boundary by less
    than the width of the street it runs down, so projecting properly would buy
    nothing and cost a dependency.

    A point exactly on an edge may land either way. That is inherent to the
    method and harmless here: the boundary of a delivery zone is a commercial
    decision drawn by hand, not a surveyed line.
    """
    if len(vertices) < 3:
        return False

    inside = False
    count = len(vertices)
    j = count - 1

    for i in range(count):
        lat_i, lng_i = vertices[i]
        lat_j, lng_j = vertices[j]

        # The inequality test also guarantees lat_j != lat_i, so the division
        # below cannot divide by zero.
        if (lat_i > lat) != (lat_j > lat):
            crossing_lng = (lng_j - lng_i) * (lat - lat_i) / (lat_j - lat_i) + lng_i
            if lng < crossing_lng:
                inside = not inside
        j = i

    return inside


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance, used to pick the nearest store."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _within_bounds(lat: float, lng: float, bounds: Dict) -> bool:
    """Cheap rejection before walking a polygon's vertices."""
    if not bounds:
        return True
    try:
        return (
            bounds["lat_min"] <= lat <= bounds["lat_max"]
            and bounds["lng_min"] <= lng <= bounds["lng_max"]
        )
    except (KeyError, TypeError):
        return True


def resolve_zone(lat: float, lng: float, zones: List[Dict]) -> Optional[Dict]:
    """
    Finds the store that serves a point.

    When zones overlap the nearest store wins, measured from the store itself.
    A store with no coordinates cannot be measured, so those fall back to the
    zone priority set in the backoffice — which is why the warning about
    missing coordinates exists on the store form.
    """
    matches = []

    for zone in zones:
        if not _within_bounds(lat, lng, zone.get("limites") or {}):
            continue

        vertices = [
            (float(v["lat"]), float(v["lng"]))
            for v in zone.get("poligono", [])
            if isinstance(v, dict) and "lat" in v and "lng" in v
        ]
        if not point_in_polygon(lat, lng, vertices):
            continue

        store_lat = zone.get("tienda_lat") or 0.0
        store_lng = zone.get("tienda_lng") or 0.0
        distance = (
            haversine_km(lat, lng, store_lat, store_lng)
            if (store_lat and store_lng) else None
        )
        matches.append((distance, zone.get("prioridad", 10), zone))

    if not matches:
        return None

    measurable = [m for m in matches if m[0] is not None]
    if measurable:
        distance, _priority, zone = min(measurable, key=lambda m: m[0])
        return {"zona": zone, "distancia_km": round(distance, 2)}

    _distance, _priority, zone = min(matches, key=lambda m: m[1])
    return {"zona": zone, "distancia_km": None}
