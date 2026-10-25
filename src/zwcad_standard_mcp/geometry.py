"""Pure 2D geometry helpers (no COM imports) for site-plan checks."""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]


def closed_edges(points: Sequence[Point]) -> list[tuple[Point, Point]]:
    pts = list(points)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _closest_on_segment(p: Point, a: Point, b: Point) -> Point:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return (ax + t * dx, ay + t * dy)


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def segment_distance(a: Point, b: Point, c: Point, d: Point) -> tuple[float, Point, Point]:
    """Minimum distance between segments AB and CD with the closest point pair (p on AB, q on CD)."""
    if _segments_intersect(a, b, c, d):
        return 0.0, a, a
    best: tuple[float, Point, Point] | None = None
    for p, (s1, s2), from_first in ((a, (c, d), True), (b, (c, d), True), (c, (a, b), False), (d, (a, b), False)):
        q = _closest_on_segment(p, s1, s2)
        dist = math.dist(p, q)
        pair = (p, q) if from_first else (q, p)
        if best is None or dist < best[0]:
            best = (dist, pair[0], pair[1])
    assert best is not None
    return best


def distances_to_boundaries(building: Sequence[Point], plot: Sequence[Point]) -> list[dict]:
    """For every plot boundary segment, the closest distance from the building outline to that segment."""
    building_edges = closed_edges(building)
    out = []
    for index, (c, d) in enumerate(closed_edges(plot)):
        best = min((segment_distance(a, b, c, d) for a, b in building_edges), key=lambda item: item[0])
        out.append(
            {
                "boundary_index": index,
                "boundary": [list(c), list(d)],
                "boundary_length": math.dist(c, d),
                "distance": best[0],
                "building_point": list(best[1]),
                "boundary_point": list(best[2]),
            }
        )
    return out


def point_in_polygon(p: Point, polygon: Sequence[Point]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in closed_edges(polygon):
        if (y1 > p[1]) != (y2 > p[1]):
            x = x1 + (p[1] - y1) * (x2 - x1) / (y2 - y1)
            if x > p[0]:
                inside = not inside
    return inside
