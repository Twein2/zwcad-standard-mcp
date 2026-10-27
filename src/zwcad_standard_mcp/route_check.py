"""Exact car-vs-boundary clearance and analysis of a driven rear-axle trace.

Used by plan_vehicle_route (exact re-check of the final poses) and verify_vehicle_path (hand-made traces such as
the SP_SLAD_JAZDY / SP_SCIEZKA output of the owner's JAZDA simulator). Pure numpy, CAD-independent.

Car frame: origin = rear axle centre, x forward. The car shape is the body rectangle plus the two mirror
rectangles (the same shape the planner and CAYENNE use).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from zwcad_standard_mcp.route_planner import Car, wrap


# --------------------------------------------------------------------------- exact geometry

def car_polygon(car: Car) -> np.ndarray:
    """Outline of body + mirrors as one simple polygon (12 vertices, counter-clockwise), car frame."""
    x0, x1, hm = car.mirror
    f, r, hw = car.front, -car.rear, car.hw
    return np.array([(r, -hw), (x0, -hw), (x0, -hm), (x1, -hm), (x1, -hw), (f, -hw),
                     (f, hw), (x1, hw), (x1, hm), (x0, hm), (x0, hw), (r, hw)], dtype=float)


def car_rects(car: Car) -> np.ndarray:
    """(x0, x1, y0, y1) of the body and of the mirror bar, car frame."""
    x0, x1, hm = car.mirror
    return np.array([(-car.rear, car.front, -car.hw, car.hw), (x0, x1, -hm, hm)], dtype=float)


def point_in_polygon(px, py, poly) -> np.ndarray:
    inside = np.zeros(np.shape(px), dtype=bool)
    a, b = poly, np.roll(poly, -1, axis=0)
    for (ax, ay), (bx, by) in zip(a, b):
        crosses = (ay > py) != (by > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = ax + (py - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (px < xint)
    return inside


def point_segment_distance(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    ll = vx * vx + vy * vy
    t = np.clip(((px - ax) * vx + (py - ay) * vy) / (ll if ll > 0 else 1.0), 0.0, 1.0)
    return np.hypot(px - ax - t * vx, py - ay - t * vy)


def exact_clearance(poses, car: Car, boundary, chunk: int = 4000) -> np.ndarray:
    """Signed clearance of every pose (x, y, heading of the rear axle): distance between the car outline and the
    boundary polygon when the car is inside, minus the penetration depth when it is not."""
    poses = np.asarray(poses, dtype=float).reshape(-1, 3)
    bnd = np.asarray(boundary, dtype=float)[:, :2]
    out = np.empty(len(poses))
    for c0 in range(0, len(poses), chunk):
        out[c0:c0 + chunk] = _exact_chunk(poses[c0:c0 + chunk], car_polygon(car), car_rects(car), bnd)
    return out


def _exact_chunk(poses, carp, rects, bnd):
    c, s = np.cos(poses[:, 2])[:, None], np.sin(poses[:, 2])[:, None]
    X = poses[:, 0][:, None] + c * carp[:, 0] - s * carp[:, 1]
    Y = poses[:, 1][:, None] + s * carp[:, 0] + c * carp[:, 1]
    a, b = bnd, np.roll(bnd, -1, axis=0)
    dmin = np.full(X.shape, np.inf)
    for (ax, ay), (bx, by) in zip(a, b):
        dmin = np.minimum(dmin, point_segment_distance(X, Y, ax, ay, bx, by))
    inside = point_in_polygon(X.ravel(), Y.ravel(), bnd).reshape(X.shape)
    pen = np.where(inside, 0.0, dmin).max(axis=1)              # car corners outside the boundary
    clr = dmin.min(axis=1)
    # boundary vertices in the car frame: inside the car = penetration; else distance to the car outline
    dx = bnd[:, 0][None, :] - poses[:, 0][:, None]
    dy = bnd[:, 1][None, :] - poses[:, 1][:, None]
    u, v = c * dx + s * dy, -s * dx + c * dy
    depth = np.zeros(u.shape)
    for x0, x1, y0, y1 in rects:
        depth = np.maximum(depth, np.minimum.reduce([u - x0, x1 - u, v - y0, y1 - v]))
    pen = np.maximum(pen, depth.max(axis=1))
    n = len(carp)
    for i in range(n):
        (ax, ay), (bx, by) = carp[i], carp[(i + 1) % n]
        clr = np.minimum(clr, point_segment_distance(u, v, ax, ay, bx, by).min(axis=1))
    # edges crossing without a vertex of either polygon inside the other (thin wall through the car)
    cross = _edges_cross(X, Y, bnd)
    pen = np.where(cross & (pen <= 0.0), 1e-6, pen)
    return np.where(pen > 0.0, -pen, clr)


def _edges_cross(X, Y, bnd) -> np.ndarray:
    n = X.shape[1]
    P1x, P1y = X, Y
    P2x, P2y = np.roll(X, -1, axis=1), np.roll(Y, -1, axis=1)
    hit = np.zeros(X.shape[0], dtype=bool)
    a, b = bnd, np.roll(bnd, -1, axis=0)
    for (ax, ay), (bx, by) in zip(a, b):
        d1 = (bx - ax) * (P1y - ay) - (by - ay) * (P1x - ax)
        d2 = (bx - ax) * (P2y - ay) - (by - ay) * (P2x - ax)
        d3 = (P2x - P1x) * (ay - P1y) - (P2y - P1y) * (ax - P1x)
        d4 = (P2x - P1x) * (by - P1y) - (P2y - P1y) * (bx - P1x)
        hit |= ((d1 * d2 < 0) & (d3 * d4 < 0)).any(axis=1)
    _ = n
    return hit


_PARTS = ("rear bumper", "right side", "right mirror", "front bumper", "left side", "left mirror")


def contact(pose, car: Car, boundary) -> dict[str, Any]:
    """Which part of the car is closest to (or deepest in) which boundary edge at one pose."""
    bnd = np.asarray(boundary, dtype=float)[:, :2]
    x, y, th = pose
    c, s = math.cos(th), math.sin(th)
    # sample the outline densely and find the point with the smallest signed distance
    poly = car_polygon(car)
    pts = []
    for i in range(len(poly)):
        p, q = poly[i], poly[(i + 1) % len(poly)]
        n = max(2, int(math.ceil(math.dist(p, q) / 2.0)) + 1)
        pts.append(np.linspace(p, q, n))
    loc = np.vstack(pts)
    X, Y = x + c * loc[:, 0] - s * loc[:, 1], y + s * loc[:, 0] + c * loc[:, 1]
    inside = point_in_polygon(X, Y, bnd)
    best_d, best_e = np.full(len(X), np.inf), np.zeros(len(X), dtype=int)
    a, b = bnd, np.roll(bnd, -1, axis=0)
    for i, ((ax, ay), (bx, by)) in enumerate(zip(a, b)):
        d = point_segment_distance(X, Y, ax, ay, bx, by)
        better = d < best_d
        best_d[better], best_e[better] = d[better], i
    sd = np.where(inside, best_d, -best_d)
    k = int(np.argmin(sd))
    lx, ly = loc[k]
    x0, x1, hm = car.mirror
    if abs(ly) > car.hw + 1e-6 and x0 - 1e-6 <= lx <= x1 + 1e-6:
        part = "left mirror" if ly > 0 else "right mirror"
    elif lx <= -car.rear + 1e-6:
        part = "rear bumper"
    elif lx >= car.front - 1e-6:
        part = "front bumper"
    elif ly > 0:
        part = "left side, " + ("front" if lx > car.L * 0.5 else "rear")
    else:
        part = "right side, " + ("front" if lx > car.L * 0.5 else "rear")
    e = int(best_e[k])
    # a boundary corner inside the car is reported too (the outline may stay outside a thin wall end)
    exact = float(exact_clearance(np.array([pose]), car, bnd)[0])
    return {"car_part": part, "edge_index": e,
            "edge": [[round(float(a[e][0]), 3), round(float(a[e][1]), 3)],
                     [round(float(b[e][0]), 3), round(float(b[e][1]), 3)]],
            "point": [round(float(X[k]), 3), round(float(Y[k]), 3)], "clearance": round(exact, 2)}


# --------------------------------------------------------------------------- trace analysis

def analyse_trace(points, car: Car, boundary, *, anchor: str = "rear_axle", lock_deg: float | None = None,
                  jump_deg_per_m: float = 60.0, ignore_start_collision: bool = True,
                  margin: float = 0.0, first_gear: str = "forward",
                  lock_tolerance_deg: float = 0.1) -> dict[str, Any]:
    """Analyse a driven trace given as points of the anchor (rear axle by default) in driving order.

    Cusps are detected from the direction reversal (consecutive chords turning back by more than 90 deg), the
    wheel angle from the curvature (rear axle: tan(delta) = L * dphi/ds, sign by gear), stops where the wheel
    angle jumps faster than ``jump_deg_per_m``. Heading at every point: the mean of the adjacent chords, which is
    exact on constant-curvature steps (simulator output) and stable against rounded coordinates. The former
    alternating reconstruction for evenly spaced traces integrated the rounding noise of the vertices and
    overstated the full lock by 0.04-0.07 deg on JAZDA traces (FINDINGS #70).
    ``lock_tolerance_deg``: steering up to lock + tolerance still counts as within the lock (rounding of drawn
    paths); places beyond it are listed in ``lock_exceedances`` (one entry per place, places closer than one
    wheelbase merged).
    """
    P = np.asarray(points, dtype=float)[:, :2]
    keep = np.concatenate([[True], np.hypot(np.diff(P[:, 0]), np.diff(P[:, 1])) > 1e-9])
    P = P[keep]
    if len(P) < 3:
        raise ValueError("The path needs at least 3 distinct points.")
    d = np.diff(P, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    ang = np.arctan2(d[:, 1], d[:, 0])
    turn = (np.diff(ang) + np.pi) % (2 * np.pi) - np.pi
    cusps = [int(i) + 1 for i in np.nonzero(np.abs(turn) > math.pi / 2)[0]]   # vertex index of each cusp
    gear = np.ones(len(ln))
    g = 1.0 if first_gear == "forward" else -1.0
    for i in range(len(ln)):
        if i in cusps:
            g = -g
        gear[i] = g
    h = ang + np.where(gear < 0, math.pi, 0.0)
    chord_h = np.unwrap(h)
    even = float(ln.std() / ln.mean()) < 0.02 if len(ln) > 2 else False
    phi = np.empty(len(P))
    phi[0], phi[-1] = chord_h[0], chord_h[-1]
    phi[1:-1] = 0.5 * (chord_h[:-1] + chord_h[1:])
    dphi = np.diff(phi)
    delta = np.arctan(car.L * dphi / ln * gear)                # wheel angle on every step (+ = left)
    lock = math.radians(lock_deg) if lock_deg is not None else math.atan2(car.L, car.rmin)
    tol = math.radians(max(0.0, lock_tolerance_deg))
    exceed = lock_exceedances(P, ln, delta, lock, tol, merge_gap=car.L)
    poses = np.column_stack([P, phi])
    clr = exact_clearance(poses, car, boundary)
    # stops: wheel angle jumps faster than the rate a moving driver can turn the wheels
    jumps = np.abs(np.diff(delta))
    thr = math.radians(jump_deg_per_m) * 0.5 * (ln[:-1] + ln[1:]) / (1000.0 * car.k)
    stop_idx = [int(i) + 1 for i in np.nonzero(jumps > thr)[0] if int(i) + 1 not in cusps]
    # merge stops on neighbouring vertices (with the mean-of-chords heading a jump spreads over two steps)
    stops = []
    for i in stop_idx:
        if stops and i - stops[-1]["last"] <= 2:
            stops[-1]["last"] = i
            stops[-1]["after_deg"] = round(math.degrees(delta[i]), 2)
            continue
        stops.append({"index": i, "last": i, "position": [round(float(P[i][0]), 3), round(float(P[i][1]), 3)],
                      "before_deg": round(math.degrees(delta[i - 1]), 2), "after_deg": round(math.degrees(delta[i]), 2)})
    for st in stops:                                     # the stop is where the car stood: middle of the jump
        a_, b_ = st["index"], st.pop("last")
        m = (a_ + b_ + 1) // 2 if b_ > a_ else a_
        st["index"], st["position"] = m, [round(float(P[m][0]), 3), round(float(P[m][1]), 3)]
    # runs
    bounds = [0] + cusps + [len(P) - 1]
    runs = []
    for r0, r1 in zip(bounds[:-1], bounds[1:]):
        seg = slice(r0, r1)
        runs.append({"gear": "forward" if gear[r0] > 0 else "reverse", "from_index": r0, "to_index": r1,
                     "length": round(float(ln[seg].sum()), 2),
                     "max_steer_deg": round(float(np.degrees(np.abs(delta[seg]).max())), 2),
                     "stops": [s_ for s_ in stops if r0 < s_["index"] < r1]})
    # start collision: poses before the car first keeps the margin
    first_ok = int(np.argmax(clr >= margin)) if (clr >= margin).any() else len(clr)
    start_part = None
    check = clr
    if ignore_start_collision and first_ok > 0:
        start_part = {"poses": first_ok, "length": round(float(ln[:first_ok].sum()), 2),
                      "max_penetration": round(float(max(0.0, -clr[:first_ok].min())), 2),
                      "min_clearance": round(float(clr[:first_ok].min()), 2)}
        check = clr[first_ok:]
    coll = np.nonzero(clr < 0)[0]
    coll_after = coll[coll >= (first_ok if ignore_start_collision else 0)]
    groups = []
    for i in coll_after:
        if groups and i - groups[-1][-1] <= 1:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    collisions = []
    for grp in groups:
        k = grp[int(np.argmin(clr[grp]))]
        collisions.append({"from_index": grp[0], "to_index": grp[-1], "max_penetration": round(float(-clr[k]), 2),
                           **contact(poses[k], car, boundary)})
    close = check[check >= 0]
    k_min = int(np.argmin(check)) + (first_ok if (ignore_start_collision and start_part) else 0) if len(check) else None
    return {"points": int(len(P)), "anchor": anchor, "gear_changes": len(cusps),
            "cusps": [{"index": c_, "position": [round(float(P[c_][0]), 3), round(float(P[c_][1]), 3)]} for c_ in cusps],
            "runs": runs, "total_length": round(float(ln.sum()), 2),
            "lock_deg": round(math.degrees(lock), 2),
            "lock_tolerance_deg": round(math.degrees(tol), 3),
            "max_steer_deg": round(float(np.degrees(np.abs(delta).max())), 2),
            "lock_exceeded": bool(exceed),
            "lock_exceedances": exceed,
            "stops": stops, "start_collision": start_part,
            "min_clearance": round(float(check.min()), 2) if len(check) else None,
            "min_clearance_index": k_min,
            "poses_closer_than_5cm": int((check < 50.0 * car.k).sum()),
            "collisions": collisions, "collision_free": not collisions,
            "heading_method": ("mean of adjacent chords (even steps: exact on constant-curvature steps)"
                               if even else "mean of adjacent chords"),
            "_poses": poses, "_delta": delta, "_gear": gear, "_clearance": clr}


def edge_violations(poses, car: Car, boundary, margin: float) -> list[dict]:
    """Boundary edges the car comes closer to than ``margin`` (or crosses), with the affected part of each edge."""
    bnd = np.asarray(boundary, dtype=float)[:, :2]
    poses = np.asarray(poses, dtype=float).reshape(-1, 3)
    loc = car.outline(spacing=max(car.L / 300.0, 1e-6))
    a, b = bnd, np.roll(bnd, -1, axis=0)
    hits: dict[int, list] = {}
    for c0 in range(0, len(poses), 500):
        P = poses[c0:c0 + 500]
        c, s = np.cos(P[:, 2])[:, None], np.sin(P[:, 2])[:, None]
        X = (P[:, 0][:, None] + c * loc[:, 0] - s * loc[:, 1]).ravel()
        Y = (P[:, 1][:, None] + s * loc[:, 0] + c * loc[:, 1]).ravel()
        best_d, best_i, best_t = np.full(X.shape, np.inf), np.zeros(X.shape, dtype=int), np.zeros(X.shape)
        for i, ((ax, ay), (bx, by)) in enumerate(zip(a, b)):
            vx, vy = bx - ax, by - ay
            ll = vx * vx + vy * vy
            t = np.clip(((X - ax) * vx + (Y - ay) * vy) / (ll if ll > 0 else 1.0), 0.0, 1.0)
            d = np.hypot(X - ax - t * vx, Y - ay - t * vy)
            better = d < best_d
            best_d[better], best_i[better], best_t[better] = d[better], i, t[better]
        inside = point_in_polygon(X, Y, bnd)
        sd = np.where(inside, best_d, -best_d)
        bad = sd < margin
        for i in np.unique(best_i[bad]):
            m = bad & (best_i == i)
            hits.setdefault(int(i), []).append((float(best_t[m].min()), float(best_t[m].max()), float(sd[m].min())))
    out = []
    for i, lst in hits.items():
        t0, t1, dmin = min(v[0] for v in lst), max(v[1] for v in lst), min(v[2] for v in lst)
        (ax, ay), (bx, by) = a[i], b[i]
        out.append({"edge_index": i,
                    "edge": [[round(float(ax), 3), round(float(ay), 3)], [round(float(bx), 3), round(float(by), 3)]],
                    "from": [round(float(ax + t0 * (bx - ax)), 3), round(float(ay + t0 * (by - ay)), 3)],
                    "to": [round(float(ax + t1 * (bx - ax)), 3), round(float(ay + t1 * (by - ay)), 3)],
                    "min_clearance": round(dmin, 2), "intrusion": round(margin - dmin, 2),
                    "crosses_boundary": dmin < 0.0})
    out.sort(key=lambda v: -v["intrusion"])
    return out


def lock_exceedances(P, ln, delta, lock: float, tol: float, merge_gap: float) -> list[dict[str, Any]]:
    """Places where |wheel angle| > lock + tol: one entry per place; places closer than ``merge_gap`` merged."""
    over = np.abs(delta) > lock + tol
    if not over.any():
        return []
    s = np.concatenate([[0.0], np.cumsum(ln)])            # station at the start of every step
    groups: list[list[int]] = []
    for i in np.nonzero(over)[0]:
        i = int(i)
        if groups and s[i] - s[groups[-1][-1] + 1] <= merge_gap:
            groups[-1].append(i)
        else:
            groups.append([i])
    out = []
    for grp in groups:
        k = grp[int(np.argmax(np.abs(delta[grp])))]
        a, b = grp[0], grp[-1] + 1
        out.append({"from_index": a, "to_index": b, "length": round(float(s[b] - s[a]), 2),
                    "from": [round(float(P[a][0]), 3), round(float(P[a][1]), 3)],
                    "to": [round(float(P[b][0]), 3), round(float(P[b][1]), 3)],
                    "max_steer_deg": round(math.degrees(abs(float(delta[k]))), 2),
                    "excess_deg": round(math.degrees(abs(float(delta[k])) - lock), 2)})
    return out
