"""Is this route a CHAIN, or does it teleport?

Extracted from `stitch-probe.py` on 2026-09-18, where it had been living alone. That is the
defect this module exists to close: the check that caught the worst failure this project has
measured was only ever wired into an EXPERIMENT, never into the gate that decides what ships.

What it caught: two independently built masters, installed together, answered a Romania →
Hungary route with **937 km in 12.04 h at 78 km/h** — every summary figure plausible — while the
geometry contained a **294.89 km jump** from 47.1587,23.8874 to 47.2186,19.9856. Distance,
duration and average speed cannot see that. Only the shape can.

A single-pass master should never produce one. "Should never" is exactly the class of statement
this project has learned to assert rather than believe.
"""
import math

# A gap bigger than this between consecutive shape points is not a road. Five kilometres rather
# than one: on a straight motorway OSM may carry few nodes, so genuine shape points can be a
# kilometre or two apart, and a threshold that flags those would cry wolf on healthy routes. A
# seam teleport is tens of kilometres, so nothing is lost by being generous here.
GEOMETRY_GAP_KM = 5.0


def decode_polyline6(encoded: str) -> list:
    """Valhalla returns route geometry as a polyline at 1e-6 precision.

    Decoded here rather than imported, because the container has no polyline library and this is
    twenty lines of the standard algorithm.
    """
    points, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for axis in range(2):
            shift, result = 0, 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / 1e6, lon / 1e6))
    return points


def shape_of(response: dict | None) -> str:
    """Every leg's geometry, concatenated.

    A route through waypoints arrives as several legs and the whole shape is their sum. Reading
    only `legs[0]` would check the first leg and silently pass a discontinuity in the second —
    which, on a corridor probe crossing four frontiers, is most of the route.
    """
    if not response:
        return ""
    try:
        return "".join(leg.get("shape") or "" for leg in response["trip"].get("legs") or [])
    except Exception:                              # noqa: BLE001 — a malformed answer has no shape
        return ""


def largest_gap_km(points: list):
    """The biggest jump between CONSECUTIVE points of the route's own geometry.

    A real road route is a dense chain: consecutive points are metres apart. A route that
    traverses an edge whose two ends are not actually joined — which is what a mismatched
    level-0 tile can offer — leaves a gap in the geometry that nothing else explains.

    This is the difference between "the route is long" and "the route is not a route", and no
    distance or duration figure can tell them apart.
    """
    worst, where = 0.0, None
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
             * math.sin(dlon / 2) ** 2)
        km = 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))
        if km > worst:
            worst, where = km, ((lat1, lon1), (lat2, lon2))
    return worst, where


def continuity(response: dict | None):
    """(worst gap in km, where) for a route response, or (None, None) if it has no geometry."""
    shape = shape_of(response)
    if not shape:
        return None, None
    return largest_gap_km(decode_polyline6(shape))
