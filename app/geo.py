"""
Geometry for geofences.

Polygons are lists of [lat, lon] vertices (not closed: the last vertex joins the
first). Calculations project onto a local flat plane in metres, centred on the
point being tested, which is accurate to well under 1% at geofence scale (up to
tens of km). Polygons crossing the antimeridian are not supported.
"""

from __future__ import annotations

import math

EARTH_R = 6_371_000.0
MAX_VERTICES = 200


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _project(poly: list[list[float]], lat0: float, lon0: float) -> list[tuple[float, float]]:
    """Vertices as (x, y) metres east/north of (lat0, lon0)."""
    k = math.cos(math.radians(lat0))
    return [(math.radians(lon - lon0) * EARTH_R * k, math.radians(lat - lat0) * EARTH_R) for lat, lon in poly]


def _edges(pts):
    return zip(pts, pts[1:] + pts[:1])


def point_in_polygon(lat: float, lon: float, poly: list[list[float]]) -> bool:
    """Even-odd ray cast from the point; handles concave polygons."""
    pts = _project(poly, lat, lon)  # the test point is the origin
    inside = False
    for (x1, y1), (x2, y2) in _edges(pts):
        if (y1 > 0) != (y2 > 0):
            x_cross = x1 + (0 - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > 0:
                inside = not inside
    return inside


def distance_to_boundary_m(lat: float, lon: float, poly: list[list[float]]) -> float:
    """Shortest distance from the point to any edge, whether inside or outside."""
    pts = _project(poly, lat, lon)
    best = math.inf
    for (x1, y1), (x2, y2) in _edges(pts):
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg2))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def area_m2(poly: list[list[float]]) -> float:
    pts = _project(poly, poly[0][0], poly[0][1])
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in _edges(pts))) / 2


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return (v > 1e-9) - (v < -1e-9)

    def on_segment(p, q, r):  # r collinear with p-q: is it within the segment?
        return min(p[0], q[0]) - 1e-9 <= r[0] <= max(p[0], q[0]) + 1e-9 and \
               min(p[1], q[1]) - 1e-9 <= r[1] <= max(p[1], q[1]) + 1e-9

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and on_segment(a, b, c)) or (o2 == 0 and on_segment(a, b, d)) or
            (o3 == 0 and on_segment(c, d, a)) or (o4 == 0 and on_segment(c, d, b)))


def is_simple(poly: list[list[float]]) -> bool:
    """True if no two non-adjacent edges touch or cross."""
    pts = _project(poly, poly[0][0], poly[0][1])
    edges = list(_edges(pts))
    n = len(edges)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # adjacent edges share a vertex by design
            if _segments_cross(*edges[i], *edges[j]):
                return False
    return True


def bounding_circle(poly: list[list[float]]) -> tuple[float, float, float]:
    """(lat, lon, radius_m) of the bounding-box centre and its farthest vertex.
    Used to place and zoom to the area, not for inside/outside tests."""
    lats = [p[0] for p in poly]
    lons = [p[1] for p in poly]
    clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    return clat, clon, max(haversine_m(clat, clon, lat, lon) for lat, lon in poly)
