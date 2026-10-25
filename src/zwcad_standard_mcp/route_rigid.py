"""Stage 1 of plan_vehicle_route: fast rigid-model corridor search (reference for the QuickTurn stage).

Hybrid A* over the rear-axle pose (x, y, heading) with circular-arc motion primitives (kinematic
bicycle model), an obstacle-aware 2D heuristic, an exact Dubins tail near the goal, and a collision
check of the whole body *with mirrors* against a signed distance field of the boundary.

Pure numpy (no scipy / shapely / matplotlib), CAD-independent: input is plain coordinates in drawing
units, output is per-gear paths ready for ``vehicle_swept_path``:
forward segment = front axle centre path, reverse segment = rear axle centre path, LWPOLYLINE
vertices ``[x, y, bulge]`` in driving order.
"""
from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass
from typing import Any

try:  # numpy is an optional dependency of the server; the tool reports a clear error without it
    import numpy as np
except ImportError:  # pragma: no cover - exercised only on installs without numpy
    np = None  # type: ignore[assignment]

UNIT_PER_MM = {"mm": 1.0, "cm": 0.1, "m": 0.001}


class PlanningError(ValueError):
    """Input problem (pose collides, boundary not closed, ...)."""


def require_numpy() -> None:
    if np is None:
        raise PlanningError(
            "plan_vehicle_route needs numpy in the server's Python environment. Install it with "
            "`.venv\\Scripts\\python -m pip install numpy` in the server folder (or re-run install.bat), "
            "then restart Claude Desktop."
        )


# --------------------------------------------------------------------------- geometry helpers

def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def arc_end(x: float, y: float, th: float, kappa: float, ds: float) -> tuple[float, float, float]:
    """Pose after driving ``ds`` (negative = reverse) on curvature ``kappa`` (+ = left)."""
    if abs(kappa) < 1e-12:
        return x + ds * math.cos(th), y + ds * math.sin(th), th
    th2 = th + kappa * ds
    return (x + (math.sin(th2) - math.sin(th)) / kappa, y - (math.cos(th2) - math.cos(th)) / kappa, th2)


def polyline_points(vertices: list[list[float]], closed: bool, max_seg: float) -> list[tuple[float, float]]:
    """Vertices ``[x, y, bulge?]`` → points with arcs (bulges) split into chords no longer than ``max_seg``."""
    pts: list[tuple[float, float]] = []
    n = len(vertices)
    last = n if closed else n - 1
    for i in range(last):
        x1, y1 = vertices[i][0], vertices[i][1]
        b = vertices[i][2] if len(vertices[i]) > 2 and vertices[i][2] else 0.0
        x2, y2 = vertices[(i + 1) % n][0], vertices[(i + 1) % n][1]
        pts.append((x1, y1))
        if b:
            chord = math.hypot(x2 - x1, y2 - y1)
            if chord < 1e-9:
                continue
            ang = 4 * math.atan(b)
            r = chord / (2 * math.sin(abs(ang) / 2))
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            d = r * math.cos(abs(ang) / 2)
            nx, ny = -(y2 - y1) / chord, (x2 - x1) / chord
            s = 1 if b > 0 else -1
            if abs(ang) > math.pi:
                d = -d
            cx, cy = mx + s * nx * d, my + s * ny * d
            a1 = math.atan2(y1 - cy, x1 - cx)
            steps = max(2, int(math.ceil(abs(ang) * r / max_seg)))
            for k in range(1, steps):
                a = a1 + ang * k / steps
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    if not closed:
        pts.append((vertices[-1][0], vertices[-1][1]))
    return pts


# --------------------------------------------------------------------------- vehicle and free space

@dataclass
class Car:
    L: float
    front: float      # rear axle -> front bumper
    rear: float       # rear axle -> rear bumper (positive)
    hw: float         # half body width
    mirror: tuple     # (x0, x1, half width with mirrors), rear-axle frame
    rmin: float       # minimum radius of the rear axle centre

    @classmethod
    def from_vehicle(cls, v: dict, units: str) -> "Car":
        k = UNIT_PER_MM[units]
        rr = math.sqrt((v["TC"] * 0.5) ** 2 - v["L"] ** 2) - v["TRF"] * 0.5
        mx1 = (v["L"] - v["MX"]) * k
        return cls(L=v["L"] * k, front=(v["L"] + v["FO"]) * k, rear=v["RO"] * k, hw=v["W"] * 0.5 * k,
                   mirror=(mx1 - v["MD"] * k, mx1, v["WM"] * 0.5 * k), rmin=rr * k)

    def outline(self, spacing: float):
        def seg(a, b):
            n = max(2, int(math.ceil(math.dist(a, b) / spacing)) + 1)
            return np.linspace(a, b, n)
        x0, x1, hm = self.mirror
        f, r, h = self.front, -self.rear, self.hw
        poly = [(r, -h), (f, -h), (f, h), (r, h), (r, -h)]
        parts = [seg(poly[i], poly[i + 1]) for i in range(4)]
        for s in (1, -1):
            parts += [seg((x0, s * h), (x0, s * hm)), seg((x0, s * hm), (x1, s * hm)), seg((x1, s * hm), (x1, s * h))]
        return np.unique(np.round(np.vstack(parts), 6), axis=0)


class FreeSpace:
    """Signed distance field of a polygon (positive inside), bilinear sampling, numpy only."""

    def __init__(self, poly, res: float, pad: float):
        self.res = res
        self.x0, self.y0 = poly.min(axis=0) - pad
        x1, y1 = poly.max(axis=0) + pad
        self.nx = int(math.ceil((x1 - self.x0) / res)) + 2
        self.ny = int(math.ceil((y1 - self.y0) / res)) + 2
        gx = self.x0 + np.arange(self.nx) * res
        gy = self.y0 + np.arange(self.ny) * res
        X, Y = np.meshgrid(gx, gy, indexing="ij")
        px, py = X.ravel(), Y.ravel()
        inside = np.zeros(px.shape, dtype=bool)
        d2 = np.full(px.shape, np.inf)
        a, b = poly, np.roll(poly, -1, axis=0)
        for (ax_, ay_), (bx_, by_) in zip(a, b):
            crosses = (ay_ > py) != (by_ > py)
            with np.errstate(divide="ignore", invalid="ignore"):
                xint = ax_ + (py - ay_) * (bx_ - ax_) / (by_ - ay_)
            inside ^= crosses & (px < xint)
            vx, vy = bx_ - ax_, by_ - ay_
            ll = vx * vx + vy * vy
            t = np.clip(((px - ax_) * vx + (py - ay_) * vy) / ll, 0.0, 1.0) if ll > 0 else np.zeros_like(px)
            dx, dy = px - (ax_ + t * vx), py - (ay_ + t * vy)
            np.minimum(d2, dx * dx + dy * dy, out=d2)
        self.sdf = (np.where(inside, 1.0, -1.0) * np.sqrt(d2)).reshape(self.nx, self.ny)

    def sample(self, x, y):
        fi = (x - self.x0) / self.res
        fj = (y - self.y0) / self.res
        i0 = np.floor(fi).astype(np.int64)
        j0 = np.floor(fj).astype(np.int64)
        ok = (i0 >= 0) & (j0 >= 0) & (i0 < self.nx - 1) & (j0 < self.ny - 1)
        i0c = np.clip(i0, 0, self.nx - 2)
        j0c = np.clip(j0, 0, self.ny - 2)
        u, v = fi - i0c, fj - j0c
        s = self.sdf
        val = (s[i0c, j0c] * (1 - u) * (1 - v) + s[i0c + 1, j0c] * u * (1 - v)
               + s[i0c, j0c + 1] * (1 - u) * v + s[i0c + 1, j0c + 1] * u * v)
        return np.where(ok, val, -1e9)


# --------------------------------------------------------------------------- Dubins (exact tail)

def _mod2pi(a: float) -> float:
    return a % (2 * math.pi)


def dubins_words(q0, q1, r: float):
    """All Dubins paths q0 -> q1 (forward, radius r): [(length, [(sign, length), ...])], shortest first."""
    dx, dy = q1[0] - q0[0], q1[1] - q0[1]
    D = math.hypot(dx, dy) / r
    th = math.atan2(dy, dx)
    a, b = _mod2pi(q0[2] - th), _mod2pi(q1[2] - th)
    sa, sb, ca, cb, cab = math.sin(a), math.sin(b), math.cos(a), math.cos(b), math.cos(a - b)
    out = []

    def add(word, t, p, q):
        signs = {"L": 1, "S": 0, "R": -1}
        segs = [(signs[c], v * r) for c, v in zip(word, (t, p, q))]
        out.append((sum(v for _, v in segs), segs))

    p2 = 2 + D * D - 2 * cab + 2 * D * (sa - sb)
    if p2 >= 0:
        tmp = math.atan2(cb - ca, D + sa - sb)
        add("LSL", _mod2pi(tmp - a), math.sqrt(p2), _mod2pi(b - tmp))
    p2 = 2 + D * D - 2 * cab + 2 * D * (sb - sa)
    if p2 >= 0:
        tmp = math.atan2(ca - cb, D - sa + sb)
        add("RSR", _mod2pi(a - tmp), math.sqrt(p2), _mod2pi(tmp - b))
    p2 = -2 + D * D + 2 * cab + 2 * D * (sa + sb)
    if p2 >= 0:
        p = math.sqrt(p2)
        tmp = math.atan2(-ca - cb, D + sa + sb) - math.atan2(-2.0, p)
        add("LSR", _mod2pi(tmp - a), p, _mod2pi(tmp - b))
    p2 = -2 + D * D + 2 * cab - 2 * D * (sa + sb)
    if p2 >= 0:
        p = math.sqrt(p2)
        tmp = math.atan2(ca + cb, D - sa - sb) - math.atan2(2.0, p)
        add("RSL", _mod2pi(a - tmp), p, _mod2pi(b - tmp))
    tmp = (6 - D * D + 2 * cab + 2 * D * (sa - sb)) / 8
    if abs(tmp) <= 1:
        p = _mod2pi(2 * math.pi - math.acos(tmp))
        t = _mod2pi(a - math.atan2(ca - cb, D - sa + sb) + p / 2)
        add("RLR", t, p, _mod2pi(a - b - t + p))
    tmp = (6 - D * D + 2 * cab + 2 * D * (sb - sa)) / 8
    if abs(tmp) <= 1:
        p = _mod2pi(2 * math.pi - math.acos(tmp))
        t = _mod2pi(-a + math.atan2(-ca + cb, D + sa - sb) + p / 2)
        add("LRL", t, p, _mod2pi(b - a - t + p))
    return sorted(out, key=lambda w: w[0])


# --------------------------------------------------------------------------- planner

def vector_pose(vec, centre_offset: float, anchor: str) -> tuple[float, float, float]:
    """Rear-axle pose from a drawn vector (tail -> head = heading).

    anchor: 'centre' (vector midpoint = body centre), 'rear_axle' / 'front_axle' (vector tail = that axle).
    centre_offset: distance rear axle -> body centre (front axle: wheelbase passed by the caller).
    """
    (ax, ay), (bx, by) = vec[0][:2], vec[-1][:2]
    th = math.atan2(by - ay, bx - ax)
    if anchor == "centre":
        px, py = (ax + bx) / 2, (ay + by) / 2
    else:
        px, py = ax, ay
    return px - centre_offset * math.cos(th), py - centre_offset * math.sin(th), th


class HeadingLattice:
    """Cost-to-go over a coarse (x, y, heading) lattice, computed backwards from the goal.

    Relaxed model (tighter turning radius, collision only where certain), so an unreachable start means no
    route exists; finite values guide the fine search far better than a 2D distance.
    """

    def __init__(self, free_many, poly, goal, radius, res_xy: float, n_head: int, allow_reverse: bool,
                 rev_w: float, certain_only: bool = True):
        self.res, self.n = res_xy, n_head
        self.x0, self.y0 = poly.min(axis=0) - res_xy
        x1, y1 = poly.max(axis=0) + res_xy
        self.nx = int(math.ceil((x1 - self.x0) / res_xy)) + 1
        self.ny = int(math.ceil((y1 - self.y0) / res_xy)) + 1
        nx, ny, n = self.nx, self.ny, n_head
        dth = 2 * math.pi / n
        ds = radius * dth
        # free states (certain-collision test only: relaxation)
        I, J, H = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(n), indexing="ij")
        xs = (self.x0 + I * res_xy).ravel().astype(float)
        ys = (self.y0 + J * res_xy).ravel().astype(float)
        ths = (H * dth).ravel().astype(float)
        free = np.zeros(xs.shape, dtype=bool)
        chunk = 20000
        for c0 in range(0, len(xs), chunk):
            free[c0:c0 + chunk] = free_many(xs[c0:c0 + chunk], ys[c0:c0 + chunk], ths[c0:c0 + chunk],
                                            certain_only=certain_only)
        self.free = free.tolist()
        # primitives: (heading h) -> list of (di, dj, dh, cost) for predecessor relation
        prims = []
        for h in range(n):
            th = h * dth
            lst = []
            for d in ((1, -1) if allow_reverse else (1,)):
                for sgn in (-1, 0, 1):
                    # predecessor of state (h) reached by a move with direction d: undo that move from h
                    px, py, pth = arc_end(0.0, 0.0, th, sgn / radius, -d * ds)
                    di, dj = int(round(px / res_xy)), int(round(py / res_xy))
                    dh = int(round(wrap(pth - th) / dth))
                    lst.append((di, dj, dh, ds * (1.0 if d > 0 else rev_w)))
            prims.append(lst)
        inf = math.inf
        cost = [inf] * (nx * ny * n)
        gi = min(max(int(round((goal[0] - self.x0) / res_xy)), 0), nx - 1)
        gj = min(max(int(round((goal[1] - self.y0) / res_xy)), 0), ny - 1)
        gh = int(round(wrap(goal[2]) / dth)) % n
        pq = []
        for di in (-1, 0, 1):          # seed the goal cell and its neighbours (rounding tolerance)
            for dj in (-1, 0, 1):
                for dh in (-1, 0, 1):
                    i, j, h = gi + di, gj + dj, (gh + dh) % n
                    if 0 <= i < nx and 0 <= j < ny:
                        idx = (i * ny + j) * n + h
                        c0 = (abs(di) + abs(dj)) * res_xy
                        if self.free[idx] and c0 < cost[idx]:
                            cost[idx] = c0
                            heapq.heappush(pq, (c0, i, j, h))
        fr = self.free
        while pq:
            c, i, j, h = heapq.heappop(pq)
            if c > cost[(i * ny + j) * n + h]:
                continue
            for di, dj, dh, w in prims[h]:
                a, b, hh = i + di, j + dj, (h + dh) % n
                if 0 <= a < nx and 0 <= b < ny:
                    idx = (a * ny + b) * n + hh
                    nc = c + w
                    if nc < cost[idx] and fr[idx]:
                        cost[idx] = nc
                        heapq.heappush(pq, (nc, a, b, hh))
        self.c = cost
        self.dth = dth

    def cost(self, x: float, y: float, th: float) -> float:
        i, j = int(round((x - self.x0) / self.res)), int(round((y - self.y0) / self.res))
        if not (0 <= i < self.nx and 0 <= j < self.ny):
            return math.inf
        h = int(round(wrap(th) / self.dth)) % self.n
        return self.c[(i * self.ny + j) * self.n + h]

    def unreachable(self, pose) -> bool:
        x, y, th = pose
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dh in (-1, 0, 1):
                    if self.cost(x + dx * self.res, y + dy * self.res, th + dh * self.dth) < math.inf:
                        return False
        return True


def _dubins_pieces(q0, q1, gear: int, kmax: float, free_many, res: float, max_len: float):
    """Collision-free Dubins connection q0 -> q1 driven in ``gear`` (pieces in real-car terms) or None."""
    flip = 0.0 if gear > 0 else math.pi
    a = (q0[0], q0[1], q0[2] + flip)
    b = (q1[0], q1[1], q1[2] + flip)
    for total, segs in dubins_words(a, b, 1.0 / kmax):
        if total > max_len + 1e-6:
            return None
        pieces, cx, cy, cth, good = [], a[0], a[1], a[2], True
        for sgn, ln in segs:
            if ln < 1e-6:
                continue
            n = max(1, int(math.ceil(ln / (2 * res))))
            pts = np.array([arc_end(cx, cy, cth, sgn * kmax, ln * i / n) for i in range(1, n + 1)])
            if not free_many(pts[:, 0], pts[:, 1], pts[:, 2] - flip).all():
                good = False
                break
            ex, ey, eth = pts[-1]
            pieces.append({"from": (cx, cy, cth - flip), "to": (float(ex), float(ey), float(eth) - flip),
                           "dir": gear, "kappa": sgn * kmax * gear, "len": ln})
            cx, cy, cth = float(ex), float(ey), float(eth)
        if good:
            return pieces
    return None


def _smooth(prims: list[dict], free_many, kmax: float, res: float) -> list[dict]:
    """Shortcut each same-gear run with collision-free Dubins connections (removes left/right zigzags)."""
    out: list[dict] = []
    i = 0
    while i < len(prims):
        j = i
        while j + 1 < len(prims) and prims[j + 1]["dir"] == prims[i]["dir"]:
            j += 1
        run = prims[i:j + 1]
        poses = [run[0]["from"]] + [p["to"] for p in run]
        cum = [0.0]
        for p in run:
            cum.append(cum[-1] + p["len"])
        a = 0
        while a < len(run):
            done = False
            for b in range(len(poses) - 1, a + 1, -1):     # farthest pose first
                pieces = _dubins_pieces(poses[a], poses[b], run[0]["dir"], kmax, free_many, res,
                                        max_len=cum[b] - cum[a])
                if pieces:
                    out.extend(pieces)
                    a = b
                    done = True
                    break
            if not done:
                out.append(run[a])
                a += 1
        i = j + 1
    return out


def _plan_once(boundary, start_vec, end_vec, vehicle: dict, units: str = "cm", *, margin_mm: float = 100.0,
               allow_reverse: bool = True, max_gear_changes: int = 3, step_mm: float = 250.0,
               steer_fraction: float = 0.95, time_budget_s: float = 50.0, anchor: str = "centre",
               h_weight: float = 2.0, guide: str = "relaxed", switch_factor: float = 1.03,
               xy_bin_factor: float = 0.4, steer_factor: float = 0.08, _lattice=None) -> dict[str, Any]:
    """Plan a route. ``boundary``: closed polygon points (drawing units). Vectors: [[x, y], [x, y]]."""
    require_numpy()
    t0 = time.monotonic()
    k = UNIT_PER_MM[units]
    car = Car.from_vehicle(vehicle, units)
    margin, step = margin_mm * k, step_mm * k
    margin_user = margin
    margin = margin + 10.0 * k          # 1 cm reserve for the sampling density of the search
    poly = np.asarray(boundary, dtype=float)[:, :2]
    if len(poly) < 3:
        raise PlanningError("The boundary needs at least 3 points.")
    res = 50.0 * k                                   # 5 cm grid
    fs = FreeSpace(poly, res, pad=10 * res)
    outline = car.outline(spacing=2 * res)
    ox, oy = outline[:, 0], outline[:, 1]

    def clearance_many(xs, ys, ths):
        c, s = np.cos(ths)[:, None], np.sin(ths)[:, None]
        X = xs[:, None] + c * ox[None, :] - s * oy[None, :]
        Y = ys[:, None] + s * ox[None, :] + c * oy[None, :]
        return fs.sample(X, Y).min(axis=1)

    # fast two-sided test with disks on the car axis: outer disks cover body + mirrors (all clear →
    # pose is free), inner disks lie inside the body (one too close → pose collides); only poses in
    # between get the exact outline check
    n_out = 10
    cx_out = np.linspace(-car.rear, car.front, n_out)
    half_gap = (car.front + car.rear) / (2 * (n_out - 1))
    r_out = math.hypot(max(car.hw, car.mirror[2]), half_gap)
    x_in0, x_in1 = -car.rear + car.hw, car.front - car.hw
    cx_in = np.linspace(x_in0, x_in1, max(2, int(math.ceil((x_in1 - x_in0) / car.hw)) + 1))
    r_in = car.hw

    def free_many(xs, ys, ths, certain_only=False):
        c, s = np.cos(ths)[:, None], np.sin(ths)[:, None]
        d_in = fs.sample(xs[:, None] + c * cx_in[None, :], ys[:, None] + s * cx_in[None, :]).min(axis=1)
        result = d_in >= r_in + margin                     # False here = certain collision
        if certain_only:
            return result
        d_out = fs.sample(xs[:, None] + c * cx_out[None, :], ys[:, None] + s * cx_out[None, :]).min(axis=1)
        unsure = result & (d_out < r_out + margin)
        if unsure.any():
            idx = np.nonzero(unsure)[0]
            result[idx] = clearance_many(xs[idx], ys[idx], ths[idx]) >= margin
        return result

    def clearance(x, y, th):
        return float(clearance_many(np.array([x]), np.array([y]), np.array([th]))[0])

    offsets = {"centre": 0.5 * (car.front - car.rear), "rear_axle": 0.0, "front_axle": car.L}
    if anchor not in offsets:
        raise PlanningError("anchor must be 'centre', 'rear_axle' or 'front_axle'.")
    start = vector_pose(start_vec, offsets[anchor], anchor)
    goal = vector_pose(end_vec, offsets[anchor], anchor)
    for name, p in (("start", start), ("end", goal)):
        c = clearance(*p)
        if c < margin:
            raise PlanningError(f"The {name} pose is too close to the boundary or outside it "
                                f"(clearance {c:.1f} < margin {margin:.1f} drawing units). "
                                "Check the vector direction and the anchor convention.")

    kmax = steer_fraction / car.rmin
    rev_w = 1.5
    gx, gy = goal[0], goal[1]
    lat = _lattice or HeadingLattice(free_many, poly, goal, car.rmin * 0.8, res_xy=250.0 * k, n_head=24,
                                     allow_reverse=allow_reverse, rev_w=rev_w)
    lat_guide = lat
    if guide == "tight" and not lat.unreachable(start):
        tight = HeadingLattice(free_many, poly, goal, 1.0 / kmax, res_xy=250.0 * k, n_head=24,
                               allow_reverse=allow_reverse, rev_w=rev_w, certain_only=False)
        if not tight.unreachable(start):
            lat_guide = tight
    lattice_s = round(time.monotonic() - t0, 2)
    if lat.unreachable(start):
        return {"units": units, "allow_reverse": allow_reverse, "expansions": 0,
                "elapsed_s": round(time.monotonic() - t0, 1), "success": False, "found": False,
                "exhausted": True, "timed_out": False,
                "start_rear_axle": [round(v, 3) for v in start], "goal_rear_axle": [round(v, 3) for v in goal],
                "reason": ("the end pose cannot be reached from the start pose"
                           + (" without reversing (checked on a relaxed model: 80 % of the minimum turning radius, "
                              "body without mirrors); set allow_reverse=true" if not allow_reverse
                              else " even with reversing (relaxed model); the space is too tight for this vehicle"))}

    if guide == "2d":
        okc = (fs.sdf >= car.hw * 0.9).tolist()
        gi2, gj2 = int(round((gx - fs.x0) / res)), int(round((gy - fs.y0) / res))
        hl = [[math.inf] * fs.ny for _ in range(fs.nx)]
        hl[gi2][gj2] = 0.0
        pq2 = [(0.0, gi2, gj2)]
        d2 = res * 1.41421356
        nb = [(1, 0, res), (-1, 0, res), (0, 1, res), (0, -1, res), (1, 1, d2), (1, -1, d2), (-1, 1, d2), (-1, -1, d2)]
        while pq2:
            c, i, j = heapq.heappop(pq2)
            if c > hl[i][j]:
                continue
            for di, dj, w in nb:
                a2, b2 = i + di, j + dj
                if 0 <= a2 < fs.nx and 0 <= b2 < fs.ny and okc[a2][b2] and c + w < hl[a2][b2]:
                    hl[a2][b2] = c + w
                    heapq.heappush(pq2, (c + w, a2, b2))

        class _G:
            @staticmethod
            def cost(x, y, th):
                i, j = int(round((x - fs.x0) / res)), int(round((y - fs.y0) / res))
                return hl[i][j] if 0 <= i < fs.nx and 0 <= j < fs.ny else math.inf
        lat_guide = _G

    def heur(x, y, th):
        v = lat_guide.cost(x, y, th)
        e = math.hypot(x - gx, y - gy)
        return max(v, e) if v < math.inf else 3 * e + 10 * car.front

    steers = (-1.0, 0.0, 1.0)
    dirs = (1, -1) if allow_reverse else (1,)
    th_bin = kmax * step * 0.999
    xy_bin = max(2 * res, step * xy_bin_factor)
    switch_cost, steer_cost = switch_factor * car.front, steer_factor * step
    tol_xy, tol_th = 2.5 * res, math.radians(2.0)
    n_sub = 4
    # precomputed primitive table: (dir, steer) -> sub-pose offsets in the node frame
    prim = []
    for nd in dirs:
        for s in steers:
            subs = [arc_end(0.0, 0.0, 0.0, s * kmax, nd * step * q / n_sub) for q in range(1, n_sub + 1)]
            prim.append((nd, s, np.array(subs)))

    def key(x, y, th, d):
        return (int(round(x / xy_bin)), int(round(y / xy_bin)), int(round(wrap(th) / th_bin)), d)

    analytic_range = 25.0 * car.L
    tail: list[dict] = []

    def analytic(x, y, th, d):
        options = (1, -1) if d == 0 else (d,)
        for gear in options:
            if gear < 0 and not allow_reverse:
                continue
            flip = 0.0 if gear > 0 else math.pi
            q0, q1 = (x, y, th + flip), (goal[0], goal[1], goal[2] + flip)
            for total, segs in dubins_words(q0, q1, 1.0 / kmax):
                if total > 3 * math.hypot(x - gx, y - gy) + 4 * car.front:
                    break
                prims_t, cx, cy, cth = [], q0[0], q0[1], q0[2]
                good = True
                for sgn, ln in segs:
                    if ln < 1e-6:
                        continue
                    n = max(1, int(math.ceil(ln / (2 * res))))
                    pts = [arc_end(cx, cy, cth, sgn * kmax, ln * i / n) for i in range(1, n + 1)]
                    arr = np.array(pts)
                    if not free_many(arr[:, 0], arr[:, 1], arr[:, 2] - flip).all():
                        good = False
                        break
                    ex, ey, eth = pts[-1]
                    prims_t.append({"from": (cx, cy, cth - flip), "to": (ex, ey, eth - flip), "dir": gear,
                                    "kappa": sgn * kmax * gear, "len": ln})
                    cx, cy, cth = ex, ey, eth
                if good:
                    return prims_t
        return None

    # node: x, y, th, dir, steer, parent, g, gear_changes
    nodes = [(start[0], start[1], start[2], 0, 0.0, -1, 0.0, 0)]
    best = {key(*start, 0): 0.0}
    openq = [(heur(*start), 0)]
    found, it, exhausted, timed_out = None, 0, False, False
    while True:
        if not openq:
            exhausted = True
            break
        if time.monotonic() - t0 > time_budget_s:
            timed_out = True
            break
        it += 1
        _, ni = heapq.heappop(openq)
        x, y, th, d, st, _, g, gc = nodes[ni]
        if math.hypot(x - gx, y - gy) <= tol_xy and abs(wrap(th - goal[2])) <= tol_th:
            found = ni
            break
        if math.hypot(x - gx, y - gy) < analytic_range and it % 3 == 1:
            t = analytic(x, y, th, d)
            if t:
                found, tail = ni, t
                break
        cands = []
        for nd, s, subs in prim:
            switching = d != 0 and nd != d
            if switching and (st != 0.0 or s != 0.0 or gc >= max_gear_changes):
                continue
            cands.append((nd, s, subs, switching))
        if not cands:
            continue
        c, sn = math.cos(th), math.sin(th)
        allsub = np.vstack([cs[2] for cs in cands])               # (n*sub, 3)
        X = x + c * allsub[:, 0] - sn * allsub[:, 1]
        Y = y + sn * allsub[:, 0] + c * allsub[:, 1]
        T = th + allsub[:, 2]
        okm = free_many(X, Y, T).reshape(len(cands), n_sub)
        for idx, (nd, s, subs, switching) in enumerate(cands):
            if not okm[idx].all():
                continue
            cx, cy, cth = X[idx * n_sub + n_sub - 1], Y[idx * n_sub + n_sub - 1], T[idx * n_sub + n_sub - 1]
            cost = step * (rev_w if nd < 0 else 1.0) + steer_cost * abs(s - st) + 0.1 * step * abs(s)
            if switching:
                cost += switch_cost
            ng = g + cost
            kk = key(cx, cy, cth, nd)
            if ng < best.get(kk, math.inf) - 1e-9:
                best[kk] = ng
                nodes.append((float(cx), float(cy), float(cth), nd, s, ni, ng, gc + (1 if switching else 0)))
                heapq.heappush(openq, (ng + h_weight * heur(cx, cy, cth), len(nodes) - 1))

    elapsed = time.monotonic() - t0
    base = {"units": units, "allow_reverse": allow_reverse, "expansions": it, "elapsed_s": round(elapsed, 1),
            "heuristic_s": lattice_s,
            "start_rear_axle": [round(v, 3) for v in start], "goal_rear_axle": [round(v, 3) for v in goal],
            "planning_radius_rear_axle": round(1 / kmax, 1), "margin": round(margin_user, 3)}
    if found is None:
        if exhausted:
            why = ("search space exhausted: no route exists at this resolution"
                   + ("" if allow_reverse else " without reversing; allow_reverse=true may solve it")
                   + (f" (max_gear_changes={max_gear_changes})" if allow_reverse else ""))
        else:
            why = (f"no route found within {time_budget_s:.0f} s"
                   + ("" if allow_reverse else "; forward-only may be impossible here, try allow_reverse=true")
                   + "; a larger time_budget_s or a smaller margin may help")
        return {**base, "success": False, "found": False, "exhausted": exhausted, "timed_out": timed_out,
                "reason": why}

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

    prims = _smooth(prims, free_many, kmax, res)
    merged: list[dict] = []
    for p in prims:
        if merged and merged[-1]["dir"] == p["dir"] and abs(merged[-1]["kappa"] - p["kappa"]) < 1e-12:
            merged[-1]["to"], merged[-1]["len"] = p["to"], merged[-1]["len"] + p["len"]
        else:
            merged.append(dict(p))
    groups: list[dict] = []
    for p in merged:
        gear = "forward" if p["dir"] > 0 else "reverse"
        if not groups or groups[-1]["gear"] != gear:
            groups.append({"gear": gear, "pieces": []})
        groups[-1]["pieces"].append(p)

    segments = []
    for grp in groups:
        fwd = grp["gear"] == "forward"
        verts = []
        for p in grp["pieces"]:
            x, y, th = p["from"]
            dth = p["kappa"] * p["len"] * p["dir"]
            px, py = (x + car.L * math.cos(th), y + car.L * math.sin(th)) if fwd else (x, y)
            verts.append([round(px, 4), round(py, 4), round(math.tan(dth / 4.0), 8) if abs(dth) > 1e-12 else 0.0])
        x, y, th = grp["pieces"][-1]["to"]
        end = (x + car.L * math.cos(th), y + car.L * math.sin(th)) if fwd else (x, y)
        verts.append([round(end[0], 4), round(end[1], 4), 0.0])
        segments.append({"gear": grp["gear"], "direction": grp["gear"],
                         "path": "front axle centre" if fwd else "rear axle centre",
                         "length": round(sum(p["len"] for p in grp["pieces"]), 2), "vertices": verts})

    replay = [tuple(start)]
    rdirs = [merged[0]["dir"] if merged else 1]
    for p in merged:
        x, y, th = p["from"]
        n = max(1, int(math.ceil(p["len"] / res)))
        replay += [arc_end(x, y, th, p["kappa"], p["len"] * p["dir"] * q / n) for q in range(1, n + 1)]
        rdirs += [p["dir"]] * n
    arr = np.array(replay)
    min_clear = float(clearance_many(arr[:, 0], arr[:, 1], arr[:, 2]).min())
    endp = replay[-1]
    result = {**base, "success": True, "found": True, "gear_changes": len(segments) - 1,
            "total_length": round(sum(s["length"] for s in segments), 2),
            "min_clearance": round(min_clear, 2),
            "clearance_ok": min_clear >= margin_user,
            "end_error": {"distance": round(math.hypot(endp[0] - gx, endp[1] - gy), 3),
                          "heading_deg": round(math.degrees(wrap(endp[2] - goal[2])), 3)},
            "segments": segments}
    result["_lattice"] = lat
    result["_reference"] = (arr, np.array(rdirs))
    return result


def plan_route(boundary, start_vec, end_vec, vehicle: dict, units: str = "cm", *, time_budget_s: float = 50.0,
               max_gear_changes: int = 3, **kw) -> dict[str, Any]:
    """Plan, then (if time is left) try again with costlier gear changes to get a simpler manoeuvre."""
    t0 = time.monotonic()
    first = _plan_once(boundary, start_vec, end_vec, vehicle, units, time_budget_s=time_budget_s,
                       max_gear_changes=max_gear_changes, **kw)
    lattice = first.pop("_lattice", None)
    best = first
    if first.get("found") and first["gear_changes"] > 0:
        while best["gear_changes"] > 0:
            left = time_budget_s - (time.monotonic() - t0)
            if left < 5:
                break
            alt = _plan_once(boundary, start_vec, end_vec, vehicle, units, time_budget_s=left,
                             max_gear_changes=best["gear_changes"] - 1, _lattice=lattice, **kw)
            alt.pop("_lattice", None)
            if not alt.get("found"):
                break
            best = alt
        best["attempts_note"] = ("first route found, then searched again for fewer gear changes "
                                 "within time_budget_s")
    best["elapsed_s"] = round(time.monotonic() - t0, 1)
    return best
