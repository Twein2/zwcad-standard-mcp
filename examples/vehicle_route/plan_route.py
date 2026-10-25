"""Plan a collision-free car route inside a closed boundary (example for zwcad-standard-mcp).

Input: JSON with the boundary polygon (drawing units), a start vector and an end vector
(tail -> head = driving heading; the vector midpoint = centre of the car body).
Output: JSON with route segments per gear, ready for ``create_entities_batch`` (LWPOLYLINE with
bulges) and ``vehicle_swept_path`` (forward segment = front axle centre path, reverse segment =
rear axle centre path, as the bundled CAYENNE port expects).

Method: hybrid A* over (x, y, heading) of the rear axle centre with circular-arc motion primitives
(kinematic bicycle model), an obstacle-aware 2D distance heuristic, and a collision check of the
whole body *with mirrors* against the signed distance field of the boundary (clearance margin).
Gear changes are allowed only between straight primitives, so every drawn segment starts tangent
to the car axis (this is what the LISP simulation assumes at the start of a path).

Only numpy, scipy and matplotlib (point-in-polygon) are needed; no CAD access.
Usage: python plan_route.py input.json output.json [--margin 10] [--step 20]
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
from dataclasses import dataclass

import numpy as np
from matplotlib.path import Path as MplPath
from scipy import ndimage

# Porsche Cayenne E3, mm (same defaults as archtools.CAYENNE_E3 / mo_tools.lsp)
CAYENNE_E3 = {"L": 2895.0, "FO": 990.0, "RO": 1045.0, "W": 1983.0, "WM": 2194.0, "MX": 1100.0,
              "MD": 230.0, "TRF": 1676.0, "TC": 12100.0}
UNIT_PER_MM = {"mm": 1.0, "cm": 0.1, "m": 0.001}


@dataclass
class Car:
    L: float
    front: float      # rear axle -> front bumper
    rear: float       # rear axle -> rear bumper (positive)
    hw: float         # half body width
    mirror: tuple     # (x0, x1, half width with mirrors) in rear-axle frame
    rmin: float       # minimum radius of the rear axle centre

    @classmethod
    def from_vehicle(cls, v: dict, units: str) -> "Car":
        k = UNIT_PER_MM[units]
        L = v["L"] * k
        rr = math.sqrt((v["TC"] * 0.5) ** 2 - v["L"] ** 2) - v["TRF"] * 0.5
        mx1 = (v["L"] - v["MX"]) * k
        return cls(L=L, front=(v["L"] + v["FO"]) * k, rear=v["RO"] * k, hw=v["W"] * 0.5 * k,
                   mirror=(mx1 - v["MD"] * k, mx1, v["WM"] * 0.5 * k), rmin=rr * k)

    def outline(self, spacing: float) -> np.ndarray:
        """Points on the body outline (with mirrors) in the rear-axle frame."""
        def seg(a, b):
            n = max(2, int(math.ceil(math.dist(a, b) / spacing)) + 1)
            return np.linspace(a, b, n)
        x0, x1, hm = self.mirror
        f, r, h = self.front, -self.rear, self.hw
        poly = [(r, -h), (f, -h), (f, h), (r, h), (r, -h)]
        pts = [seg(poly[i], poly[i + 1]) for i in range(4)]
        for s in (1, -1):
            pts += [seg((x0, s * h), (x0, s * hm)), seg((x0, s * hm), (x1, s * hm)), seg((x1, s * hm), (x1, s * h))]
        return np.vstack(pts)


class FreeSpace:
    """Signed distance field of the boundary (positive inside)."""

    def __init__(self, poly: np.ndarray, res: float, pad: float):
        self.res = res
        self.x0, self.y0 = poly.min(axis=0) - pad
        x1, y1 = poly.max(axis=0) + pad
        self.nx = int(math.ceil((x1 - self.x0) / res)) + 1
        self.ny = int(math.ceil((y1 - self.y0) / res)) + 1
        gx = self.x0 + np.arange(self.nx) * res
        gy = self.y0 + np.arange(self.ny) * res
        X, Y = np.meshgrid(gx, gy, indexing="ij")
        pts = np.column_stack([X.ravel(), Y.ravel()])
        inside = MplPath(poly).contains_points(pts).reshape(self.nx, self.ny)
        d = np.full(pts.shape[0], np.inf)
        a, b = poly, np.roll(poly, -1, axis=0)
        for p, q in zip(a, b):            # exact distance to every edge
            v = q - p
            t = np.clip(((pts - p) @ v) / (v @ v), 0.0, 1.0)
            d = np.minimum(d, np.hypot(*(pts - (p + t[:, None] * v)).T))
        self.sdf = np.where(inside, 1, -1) * d.reshape(self.nx, self.ny)
        self.inside = inside

    def sample(self, xy: np.ndarray) -> np.ndarray:
        i = (xy[:, 0] - self.x0) / self.res
        j = (xy[:, 1] - self.y0) / self.res
        return ndimage.map_coordinates(self.sdf, [i, j], order=1, mode="constant", cval=-1e9)


def transform(local: np.ndarray, x: float, y: float, th: float) -> np.ndarray:
    c, s = math.cos(th), math.sin(th)
    return np.column_stack([x + c * local[:, 0] - s * local[:, 1], y + s * local[:, 0] + c * local[:, 1]])


def arc_end(x, y, th, kappa, ds):
    if abs(kappa) < 1e-12:
        return x + ds * math.cos(th), y + ds * math.sin(th), th
    th2 = th + kappa * ds
    return (x + (math.sin(th2) - math.sin(th)) / kappa, y - (math.cos(th2) - math.cos(th)) / kappa, th2)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def mod2pi(a):
    return a % (2 * math.pi)


def dubins_words(q0, q1, r):
    """All Dubins paths q0 -> q1 (forward driving, turning radius r).

    Returns a list of (total_length, [(curvature_sign, length), ...]) sorted by length.
    """
    dx, dy = q1[0] - q0[0], q1[1] - q0[1]
    D = math.hypot(dx, dy) / r
    th = math.atan2(dy, dx)
    a, b = mod2pi(q0[2] - th), mod2pi(q1[2] - th)
    sa, sb, ca, cb, cab = math.sin(a), math.sin(b), math.cos(a), math.cos(b), math.cos(a - b)
    out = []

    def add(word, t, p, q):
        signs = {"L": 1, "S": 0, "R": -1}
        segs = [(signs[c], v * r) for c, v in zip(word, (t, p, q))]
        out.append((sum(v for _, v in segs), segs))

    p2 = 2 + D * D - 2 * cab + 2 * D * (sa - sb)                      # LSL
    if p2 >= 0:
        tmp = math.atan2(cb - ca, D + sa - sb)
        add("LSL", mod2pi(tmp - a), math.sqrt(p2), mod2pi(b - tmp))
    p2 = 2 + D * D - 2 * cab + 2 * D * (sb - sa)                      # RSR
    if p2 >= 0:
        tmp = math.atan2(ca - cb, D - sa + sb)
        add("RSR", mod2pi(a - tmp), math.sqrt(p2), mod2pi(tmp - b))
    p2 = -2 + D * D + 2 * cab + 2 * D * (sa + sb)                     # LSR
    if p2 >= 0:
        p = math.sqrt(p2)
        tmp = math.atan2(-ca - cb, D + sa + sb) - math.atan2(-2.0, p)
        add("LSR", mod2pi(tmp - a), p, mod2pi(tmp - b))
    p2 = -2 + D * D + 2 * cab - 2 * D * (sa + sb)                     # RSL
    if p2 >= 0:
        p = math.sqrt(p2)
        tmp = math.atan2(ca + cb, D - sa - sb) - math.atan2(2.0, p)
        add("RSL", mod2pi(a - tmp), p, mod2pi(b - tmp))
    tmp = (6 - D * D + 2 * cab + 2 * D * (sa - sb)) / 8               # RLR
    if abs(tmp) <= 1:
        p = mod2pi(2 * math.pi - math.acos(tmp))
        t = mod2pi(a - math.atan2(ca - cb, D - sa + sb) + p / 2)
        add("RLR", t, p, mod2pi(a - b - t + p))
    tmp = (6 - D * D + 2 * cab + 2 * D * (sb - sa)) / 8               # LRL
    if abs(tmp) <= 1:
        p = mod2pi(2 * math.pi - math.acos(tmp))
        t = mod2pi(-a + math.atan2(-ca + cb, D + sa - sb) + p / 2)
        add("LRL", t, p, mod2pi(b - a - t + p))
    return sorted(out, key=lambda w: w[0])


def plan(data: dict, margin: float, step: float, steer_fraction: float = 0.95, max_iter: int = 400000):
    units = data.get("units", "cm")
    car = Car.from_vehicle({**CAYENNE_E3, **data.get("vehicle", {})}, units)
    poly = np.array(data["limits"], float)
    res = 5.0 * (10 * UNIT_PER_MM[units])            # 5 cm grid
    fs = FreeSpace(poly, res, pad=50 * res)
    outline = car.outline(spacing=res * 2)
    centre = 0.5 * (car.front - car.rear)

    def pose(vec):
        (ax, ay), (bx, by) = vec
        th = math.atan2(by - ay, bx - ax)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        return mx - centre * math.cos(th), my - centre * math.sin(th), th

    def free(x, y, th):
        return fs.sample(transform(outline, x, y, th)).min() >= margin

    start, goal = pose(data["start"]), pose(data["end"])
    for name, p in (("start", start), ("end", goal)):
        if not free(*p):
            raise SystemExit(f"{name} pose collides with the boundary (clearance < {margin})")

    # obstacle-aware heuristic: 8-connected Dijkstra from the goal on the free grid
    ok = fs.sdf >= car.hw * 0.9
    gi, gj = int(round((goal[0] - fs.x0) / res)), int(round((goal[1] - fs.y0) / res))
    h2d = np.full(ok.shape, np.inf)
    h2d[gi, gj] = 0.0
    pq = [(0.0, gi, gj)]
    nb = [(1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]
    while pq:
        c, i, j = heapq.heappop(pq)
        if c > h2d[i, j]:
            continue
        for di, dj, w in nb:
            a, b = i + di, j + dj
            if 0 <= a < fs.nx and 0 <= b < fs.ny and ok[a, b] and c + w * res < h2d[a, b]:
                h2d[a, b] = c + w * res
                heapq.heappush(pq, (h2d[a, b], a, b))

    def heur(x, y, th):
        i, j = int(round((x - fs.x0) / res)), int(round((y - fs.y0) / res))
        v = h2d[i, j] if 0 <= i < fs.nx and 0 <= j < fs.ny else np.inf
        if not np.isfinite(v):
            v = math.dist((x, y), goal[:2]) * 3
        return max(v, math.dist((x, y), goal[:2]))

    kmax = steer_fraction / car.rmin
    steers = [-1.0, 0.0, 1.0]
    # one full-lock primitive must turn the car by at least one heading bin, otherwise curved
    # successors fall into the bin of the straight one and are pruned (classic hybrid A* trap)
    th_bin = kmax * step * 0.999
    xy_bin = res * 2
    rev_w, switch_cost, steer_cost = 1.6, 400.0 * res / 5.0, 2.0 * res / 5.0
    tol_xy, tol_th = 4.0 * res / 5.0 * 2.5, math.radians(2.0)

    analytic_range, analytic_every, tail = 2500.0 * res / 5.0, 3, []

    def analytic(x, y, th, d, st):
        """Exact Dubins tail to the goal in the node's gear (both gears from the start node)."""
        for gear in ((1, -1) if d == 0 else (d,)):
            flip = 0.0 if gear > 0 else math.pi          # reversing = forward Dubins of the flipped car
            q0, q1 = (x, y, th + flip), (goal[0], goal[1], goal[2] + flip)
            for total, segs in dubins_words(q0, q1, 1.0 / kmax):
                if total > 4 * math.dist((x, y), goal[:2]) + 2000:
                    break
                prims_t, ok_t = [], True
                cx, cy, cth = q0
                for sgn, ln in segs:
                    if ln < 1e-6:
                        continue
                    n = max(1, int(math.ceil(ln / (res * 2))))
                    for i in range(1, n + 1):
                        px, py, pth = arc_end(cx, cy, cth, sgn * kmax, ln * i / n)
                        if not free(px, py, pth - flip):
                            ok_t = False
                            break
                    if not ok_t:
                        break
                    nx_, ny_, nth = arc_end(cx, cy, cth, sgn * kmax, ln)
                    # store in real-car terms: heading without flip, signed curvature per unit of travel
                    prims_t.append({"from": (cx, cy, cth - flip), "to": (nx_, ny_, nth - flip), "dir": gear,
                                    "kappa": sgn * kmax * gear, "len": ln})
                    cx, cy, cth = nx_, ny_, nth
                if ok_t:
                    return prims_t
        return None

    def key(x, y, th, d):
        return (int(round(x / xy_bin)), int(round(y / xy_bin)), int(round(wrap(th) / th_bin)), d)

    # node: (x, y, th, dir, steer, parent, g)
    nodes = [(start[0], start[1], start[2], 0, 0.0, -1, 0.0)]
    best = {key(*start, 0): 0.0}
    openq = [(heur(*start), 0)]
    found, it = None, 0
    while openq and it < max_iter:
        it += 1
        _, ni = heapq.heappop(openq)
        x, y, th, d, st, _, g = nodes[ni]
        if math.dist((x, y), goal[:2]) <= tol_xy and abs(wrap(th - goal[2])) <= tol_th:
            found = ni
            break
        if math.dist((x, y), goal[:2]) < analytic_range and it % analytic_every == 0:
            tail_found = analytic(x, y, th, d, st)
            if tail_found:
                found, tail = ni, tail_found
                break
        for nd in (1, -1):
            switching = d != 0 and nd != d
            for s in steers:
                if switching and (st != 0.0 or s != 0.0):
                    continue                      # gear change only between straight pieces
                k = s * kmax
                ds = step * nd
                ok_move, cx, cy, cth = True, x, y, th
                for sub in range(1, 5):           # check intermediate poses
                    px, py, pth = arc_end(x, y, th, k, ds * sub / 4)
                    if not free(px, py, pth):
                        ok_move = False
                        break
                    cx, cy, cth = px, py, pth
                if not ok_move:
                    continue
                cost = step * (rev_w if nd < 0 else 1.0) + steer_cost * abs(s - st) + (switch_cost if switching else 0)
                cost += step * 0.1 * abs(s)
                ng = g + cost
                kk = key(cx, cy, cth, nd)
                if ng < best.get(kk, np.inf) - 1e-9:
                    best[kk] = ng
                    nodes.append((cx, cy, cth, nd, s, ni, ng))
                    heapq.heappush(openq, (ng + 1.05 * heur(cx, cy, cth), len(nodes) - 1))
    if found is None:
        raise SystemExit(f"No route found after {it} expansions")

    chain = []
    i = found
    while i > 0:
        chain.append(nodes[i])
        i = nodes[i][5]
    chain.reverse()
    prims, prev = [], nodes[0]
    for n in chain:
        prims.append({"from": prev[:3], "to": n[:3], "dir": n[3], "kappa": n[4] * kmax, "len": step})
        prev = n
    prims += tail

    # merge equal primitives, then group by gear
    merged = []
    for p in prims:
        if merged and merged[-1]["dir"] == p["dir"] and abs(merged[-1]["kappa"] - p["kappa"]) < 1e-12:
            merged[-1]["to"], merged[-1]["len"] = p["to"], merged[-1]["len"] + p["len"]
        else:
            merged.append(dict(p))
    segments = []
    for p in merged:
        if not segments or segments[-1]["gear"] != ("forward" if p["dir"] > 0 else "reverse"):
            segments.append({"gear": "forward" if p["dir"] > 0 else "reverse", "pieces": []})
        segments[-1]["pieces"].append(p)

    out = []
    for sgm in segments:
        fwd = sgm["gear"] == "forward"
        verts = []
        for p in sgm["pieces"]:
            x, y, th = p["from"]
            dth = p["kappa"] * p["len"] * p["dir"]
            # forward: front axle centre (concentric arc, same swept angle); reverse: rear axle centre
            px, py = (x + car.L * math.cos(th), y + car.L * math.sin(th)) if fwd else (x, y)
            # the tangent of the drawn path turns by the same angle as the car axis (both gears)
            bulge = math.tan(dth / 4.0) if abs(dth) > 1e-12 else 0.0
            verts.append([px, py, bulge])
        x, y, th = sgm["pieces"][-1]["to"]
        verts.append([x + car.L * math.cos(th), y + car.L * math.sin(th), 0.0] if fwd else [x, y, 0.0])
        out.append({"gear": sgm["gear"], "path": "front axle centre" if fwd else "rear axle centre",
                    "length": round(sum(p["len"] for p in sgm["pieces"]), 1),
                    "vertices": [[round(a, 3), round(b, 3), round(c, 6)] for a, b, c in verts]})

    # dense replay of the final route (also checks that the merged pieces chain up exactly)
    replay = [tuple(start)]
    for p in prims:
        x, y, th = p["from"]
        n = max(1, int(math.ceil(p["len"] / res)))
        for i in range(1, n + 1):
            replay.append(arc_end(x, y, th, p["kappa"], p["len"] * p["dir"] * i / n))
    gaps = max(math.dist(a["to"][:2], b["from"][:2]) for a, b in zip(prims, prims[1:])) if len(prims) > 1 else 0.0
    endp = replay[-1]
    clearance = min(fs.sample(transform(outline, *q)).min() for q in replay)
    return {
        "units": units,
        "vehicle": {"rear_axle_min_radius": round(car.rmin, 1), "planning_radius": round(1 / kmax, 1)},
        "start_rear_axle": [round(v, 3) for v in start],
        "goal_rear_axle": [round(v, 3) for v in goal],
        "reached_rear_axle": [round(v, 3) for v in endp],
        "end_error": {"distance": round(math.dist(endp[:2], goal[:2]), 2),
                      "heading_deg": round(math.degrees(wrap(endp[2] - goal[2])), 2)},
        "min_clearance": round(float(clearance), 1),
        "margin": margin,
        "gear_changes": len(out) - 1,
        "total_length": round(sum(s["length"] for s in out), 1),
        "expansions": it,
        "segments": out,
        "max_piece_gap": round(gaps, 4),
        "poses": [[round(q[0], 2), round(q[1], 2), round(q[2], 5)] for q in replay[:: max(1, len(replay) // 400)]],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--margin", type=float, default=10.0, help="clearance to the boundary, drawing units")
    ap.add_argument("--step", type=float, default=25.0, help="primitive length, drawing units")
    a = ap.parse_args()
    res = plan(json.load(open(a.input, encoding="utf-8")), a.margin, a.step)
    json.dump(res, open(a.output, "w", encoding="utf-8"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k not in ("segments", "poses")}, indent=1))
    for s in res["segments"]:
        print(s["gear"], s["length"], len(s["vertices"]), "vertices")


if __name__ == "__main__":
    main()
