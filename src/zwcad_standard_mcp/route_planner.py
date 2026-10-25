"""Collision-free car route planning inside a closed boundary (used by the plan_vehicle_route tool).

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


def _sample_dubins(x, y, tau, segs, radius: float, ds: float):
    """Points and tangents along a Dubins word (segs: [(sign, length)]) starting at (x, y, tau)."""
    pts, tans = [(x, y)], [tau]
    for sgn, ln in segs:
        if ln < 1e-9:
            continue
        n = max(1, int(math.ceil(ln / ds)))
        k = sgn / radius
        for i in range(1, n + 1):
            px, py, pt = arc_end(x, y, tau, k, ln * i / n)
            pts.append((px, py))
            tans.append(pt)
        x, y, tau = arc_end(x, y, tau, k, ln)
    return np.array(pts), np.array(tans), (x, y, tau)


def _tractrix(pts, phi0: float, L: float):
    """Exact QuickTurn step (tan(a/2)·exp(-d/L)): car headings while the front axle follows ``pts``."""
    phis = np.empty(len(pts))
    phi = phi0
    phis[0] = phi
    for i in range(1, len(pts)):
        dx, dy = pts[i, 0] - pts[i - 1, 0], pts[i, 1] - pts[i - 1, 1]
        dd = math.hypot(dx, dy)
        if dd > 1e-12:
            th = math.atan2(dy, dx)
            a0 = wrap(phi - th)
            a1 = 0.0 if abs(a0) < 1e-12 else math.copysign(2 * math.atan(math.tan(abs(a0) / 2) * math.exp(-dd / L)), a0)
            phi = th + a1
        phis[i] = phi
    return phis


def _segments_to_vertices(start_xy, start_tau, pieces, radius):
    verts, (x, y), tau = [], start_xy, start_tau
    for sgn, ln in pieces:
        if ln < 1e-9:
            continue
        dth = sgn * ln / radius
        verts.append([round(x, 4), round(y, 4), round(math.tan(dth / 4.0), 8) if sgn else 0.0])
        x, y, tau = arc_end(x, y, tau, sgn / radius, ln)
    verts.append([round(x, 4), round(y, 4), 0.0])
    # merge consecutive pieces with the same curvature
    out = [verts[0]]
    for v in verts[1:]:
        out.append(v)
    return out


def _plan_once(boundary, start_vec, end_vec, vehicle: dict, units: str = "cm", *, margin_mm: float = 100.0,
               allow_reverse: bool = True, max_gear_changes: int = 3, step_mm: float = 250.0,
               steer_fraction: float = 0.95, time_budget_s: float = 50.0, anchor: str = "centre",
               h_weight: float = 2.0, guide: str = "relaxed", switch_factor: float = 1.03,
               xy_bin_factor: float = 0.2, steer_factor: float = 0.08, th_bin_factor: float = 0.5,
               _lattice=None, reference=None) -> dict[str, Any]:
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

    if reference is not None:
        # follow the stage-1 corridor: cost-to-go along the reference + distance and heading deviation from it
        ref_p, ref_d = reference
        seg = np.hypot(np.diff(ref_p[:, 0]), np.diff(ref_p[:, 1])) * np.where(ref_d[1:] < 0, rev_w, 1.0)
        seg = seg + np.where(ref_d[1:] != ref_d[:-1], switch_factor * car.front, 0.0)
        rem = np.concatenate([np.cumsum(seg[::-1])[::-1], [0.0]])
        RX, RY, RT = ref_p[:, 0], ref_p[:, 1], ref_p[:, 2]

        def heur(x, y, th):  # noqa: F811 - replaces the lattice heuristic
            dist = np.hypot(RX - x, RY - y)
            dth = np.abs((RT - th + np.pi) % (2 * np.pi) - np.pi)
            score = dist + car.L * dth
            i = int(np.argmin(score))
            return float(rem[i] + 2.0 * dist[i] + car.L * dth[i])

    # ---------------------------------------------------------------- QuickTurn-consistent search
    # forward: the FRONT axle follows straights and arcs (tangent-continuous), the car follows by the exact
    # tractrix; reverse: the REAR axle follows straights and arcs with the car axis tangent (CAYENNE 'Tyl').
    L = car.L
    r_rear = 1.0 / kmax
    r_front = math.hypot(r_rear, L)
    d_max = math.atan2(L, car.rmin) + 1e-6
    steers = (-1.0, 0.0, 1.0)
    dirs = (1, -1) if allow_reverse else (1,)
    th_bin = kmax * step * th_bin_factor
    xy_bin = step * xy_bin_factor
    d_bin = step / r_front * 0.999       # one front-arc primitive must change the steering bin (else pruned)
    switch_cost, steer_cost = switch_factor * car.front, steer_factor * step
    tol_xy, tol_th = 2.5 * res, math.radians(1.0)
    n_sub = 4

    def guided_start(x, y, phi, d, tau, nd):
        """Guided point and its tangent when continuing (same gear) or starting/switching (car at rest)."""
        if nd > 0:
            return x + L * math.cos(phi), y + L * math.sin(phi), (tau if d > 0 else phi)
        return x, y, (tau if d < 0 else phi + math.pi)

    def simulate(nd, gx0, gy0, tau0, phi0, pieces, ds_):
        """Poses (rear x, y, heading) along guided pieces [(sign, length)]; returns poses, end tau, max steer."""
        r = r_front if nd > 0 else r_rear
        pts, tans, endq = _sample_dubins(gx0, gy0, tau0, pieces, r, ds_)
        if nd > 0:
            phis = _tractrix(pts, phi0, L)
            rear = pts - L * np.column_stack([np.cos(phis), np.sin(phis)])
            delta = float(np.abs((tans - phis + np.pi) % (2 * np.pi) - np.pi).max())
            return np.column_stack([rear, phis]), endq, delta
        delta = math.atan2(L, r) if any(sg for sg, ln in pieces if ln > 1e-9) else 0.0
        return np.column_stack([pts, tans - math.pi]), endq, delta

    def key(x, y, th, d, delta):
        return (int(round(x / xy_bin)), int(round(y / xy_bin)), int(round(wrap(th) / th_bin)), d,
                int(round(delta / d_bin)) if d > 0 else 0)

    analytic_range = 25.0 * L

    def analytic(x, y, phi, d, tau):
        """Exact tail to the goal in the current gear (both gears from the start)."""
        for nd in ((1, -1) if d == 0 else (d,)):
            if nd < 0 and not allow_reverse:
                continue
            gx0, gy0, t0_ = guided_start(x, y, phi, d, tau, nd)
            if nd < 0:
                variants = [((gx, gy, goal[2] + math.pi), 0.0)]
                r = r_rear
            else:
                fx, fy = gx + L * math.cos(goal[2]), gy + L * math.sin(goal[2])
                variants = [((fx - dd * math.cos(goal[2]), fy - dd * math.sin(goal[2]), goal[2]), dd)
                            for dd in (0.0, 2 * L, 4 * L)]
                r = r_front
            for q1, run_in in variants:
                for total, segs in dubins_words((gx0, gy0, t0_), q1, r)[:4]:
                    if total > 3 * math.hypot(x - gx, y - gy) + 4 * car.front:
                        break
                    pieces = [(sg, ln) for sg, ln in segs if ln > 1e-9] + ([(0, run_in)] if run_in > 0 else [])
                    poses_t, _, dl = simulate(nd, gx0, gy0, t0_, phi, pieces, 2 * res)
                    if dl > d_max:
                        continue
                    end = poses_t[-1]
                    if math.hypot(end[0] - gx, end[1] - gy) > tol_xy or abs(wrap(end[2] - goal[2])) > tol_th:
                        continue
                    if not free_many(poses_t[:, 0], poses_t[:, 1], poses_t[:, 2]).all():
                        continue
                    return [(nd, sg, ln) for sg, ln in pieces]
        return None

    # node: x, y, phi, dir, steer, parent, g, gear_changes, tau, prim(dir, sign, length)
    min_run = 2.0 * car.L                    # no 25 cm shuffles: each gear is used for at least 2 wheelbases
    nodes = [(start[0], start[1], start[2], 0, 0.0, -1, 0.0, 0, start[2], None, 0.0)]
    best = {key(*start, 0, 0.0): 0.0}
    openq = [(heur(*start), 0)]
    found, it, exhausted, timed_out = None, 0, False, False
    tail: list[tuple] = []
    while True:
        if not openq:
            exhausted = True
            break
        if time.monotonic() - t0 > time_budget_s:
            timed_out = True
            break
        it += 1
        _, ni = heapq.heappop(openq)
        x, y, phi, d, st, _, g, gc, tau, _p, run_len = nodes[ni]
        if math.hypot(x - gx, y - gy) <= tol_xy and abs(wrap(phi - goal[2])) <= tol_th:
            found = ni
            break
        if math.hypot(x - gx, y - gy) < analytic_range and it % 3 == 1:
            t = analytic(x, y, phi, d, tau)
            if t:
                found, tail = ni, t
                break
        cands = []
        for nd in dirs:
            switching = d != 0 and nd != d
            if switching and (gc >= max_gear_changes or run_len < min_run - 1e-6):
                continue
            gx0, gy0, t0_ = guided_start(x, y, phi, d, tau, nd)
            r = r_front if nd > 0 else r_rear
            for sgn in steers:
                subs = [arc_end(gx0, gy0, t0_, sgn / r, step * q / n_sub) for q in range(1, n_sub + 1)]
                if nd > 0:
                    pts = np.array([(gx0, gy0)] + [(p[0], p[1]) for p in subs])
                    phis = _tractrix(pts, phi, L)[1:]
                    tans = np.array([p[2] for p in subs])
                    if np.abs((tans - phis + np.pi) % (2 * np.pi) - np.pi).max() > d_max:
                        continue
                    rear = pts[1:] - L * np.column_stack([np.cos(phis), np.sin(phis)])
                    sub_poses = np.column_stack([rear, phis])
                else:
                    sub_poses = np.array([(p[0], p[1], p[2] - math.pi) for p in subs])
                cands.append((nd, sgn, switching, sub_poses, subs[-1][2]))
        if not cands:
            continue
        allp = np.vstack([c_[3] for c_ in cands])
        okm = free_many(allp[:, 0], allp[:, 1], allp[:, 2]).reshape(len(cands), n_sub)
        for idx, (nd, sgn, switching, sub_poses, tau_end) in enumerate(cands):
            if not okm[idx].all():
                continue
            cx, cy, cth = sub_poses[-1]
            cost = step * (rev_w if nd < 0 else 1.0) + 0.1 * step * abs(sgn)
            cost += switch_cost if switching else (steer_cost * abs(sgn - st) if d == nd else 0.0)
            ng = g + cost
            delta = wrap(tau_end - cth) if nd > 0 else 0.0
            kk = key(cx, cy, cth, nd, delta)
            if ng < best.get(kk, math.inf) - 1e-9:
                best[kk] = ng
                nodes.append((float(cx), float(cy), float(cth), nd, sgn, ni, ng, gc + (1 if switching else 0),
                              float(tau_end), (nd, sgn, step), (step if (switching or d == 0) else run_len + step)))
                heapq.heappush(openq, (ng + h_weight * heur(cx, cy, cth), len(nodes) - 1))

    elapsed = time.monotonic() - t0
    base = {"units": units, "allow_reverse": allow_reverse, "expansions": it, "elapsed_s": round(elapsed, 1),
            "heuristic_s": lattice_s,
            "start_rear_axle": [round(v, 3) for v in start], "goal_rear_axle": [round(v, 3) for v in goal],
            "planning_radius_rear_axle": round(r_rear, 1), "planning_radius_front_axle": round(r_front, 1),
            "margin": round(margin_user, 3)}
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
        chain.append(nodes[i][9])
        i = nodes[i][5]
    prims = list(reversed(chain)) + tail            # [(dir, sign, length)]

    def runs_of(pr):
        out_ = []
        for nd, sg, ln in pr:
            if out_ and out_[-1][0] == nd:
                out_[-1][1].append((sg, ln))
            else:
                out_.append([nd, [(sg, ln)]])
        return out_

    def replay(runs_):
        """Simulate runs from the start: per run guided start, poses; overall poses, max steering."""
        x, y, phi, d, tau = start[0], start[1], start[2], 0, start[2]
        allposes, info, dmx = [np.array([[x, y, phi]])], [], 0.0
        for nd, pieces in runs_:
            gx0, gy0, t0_ = guided_start(x, y, phi, d, tau, nd)
            poses_r, endq, dl = simulate(nd, gx0, gy0, t0_, phi, pieces, res)
            info.append((nd, (gx0, gy0, t0_), pieces))
            allposes.append(poses_r[1:])
            dmx = max(dmx, dl)
            x, y, phi = poses_r[-1]
            d, tau = nd, endq[2]
        return info, np.vstack(allposes), dmx

    def smooth(runs_):
        """Greedy shortcuts per run with Dubins pieces of the guided axle, simulated in the same model."""
        out_runs = []
        x, y, phi, d, tau = start[0], start[1], start[2], 0, start[2]
        raw_info, _, _ = replay(runs_)
        for ri, (nd, run_start, pieces) in enumerate(raw_info):
            r = r_front if nd > 0 else r_rear
            last = ri == len(raw_info) - 1
            wps, q = [], run_start           # guided poses after every raw piece
            for sg, ln in pieces:
                q = arc_end(q[0], q[1], q[2], sg / r, ln)
                wps.append(q)
            gx0, gy0, t0_ = guided_start(x, y, phi, d, tau, nd)
            new_pieces, a = [], -1
            while a < len(wps) - 1:
                done = False
                for bb in range(len(wps) - 1, a, -1):
                    for total, segs in dubins_words((gx0, gy0, t0_), wps[bb], r)[:3]:
                        seglen = sum(ln for _, ln in pieces[a + 1:bb + 1])
                        if total > 1.25 * seglen + 1e-6:
                            break
                        pc = [(sg, ln) for sg, ln in segs if ln > 1e-9]
                        poses_c, endq, dl = simulate(nd, gx0, gy0, t0_, phi, pc, 2 * res)
                        if dl > d_max or not free_many(poses_c[:, 0], poses_c[:, 1], poses_c[:, 2]).all():
                            continue
                        if last and bb == len(wps) - 1:
                            e_ = poses_c[-1]
                            if math.hypot(e_[0] - gx, e_[1] - gy) > tol_xy or abs(wrap(e_[2] - goal[2])) > tol_th:
                                continue
                        new_pieces += pc
                        gx0, gy0, t0_ = endq
                        x, y, phi = poses_c[-1]
                        a, done = bb, True
                        break
                    if done:
                        break
                if not done:
                    sg, ln = pieces[a + 1]          # keep the original piece, re-simulated from the actual state
                    poses_c, endq, dl = simulate(nd, gx0, gy0, t0_, phi, [(sg, ln)], 2 * res)
                    if dl > d_max or not free_many(poses_c[:, 0], poses_c[:, 1], poses_c[:, 2]).all():
                        return None
                    new_pieces.append((sg, ln))
                    gx0, gy0, t0_ = endq
                    x, y, phi = poses_c[-1]
                    a += 1
            d, tau = nd, t0_
            out_runs.append([nd, new_pieces])
        end = (x, y, phi)
        if math.hypot(end[0] - gx, end[1] - gy) > tol_xy or abs(wrap(end[2] - goal[2])) > tol_th:
            return None
        return out_runs

    runs = runs_of(prims)
    smoothed = smooth(runs)
    used = smoothed if smoothed is not None else runs
    info, arr, dmx = replay(used)
    segments = []
    for nd, (sx, sy, stau), pieces in info:
        merged: list[list] = []
        for sg, ln in pieces:
            if merged and merged[-1][0] == sg:
                merged[-1][1] += ln
            else:
                merged.append([sg, ln])
        r = r_front if nd > 0 else r_rear
        segments.append({"gear": "forward" if nd > 0 else "reverse", "direction": "forward" if nd > 0 else "reverse",
                         "path": "front axle centre" if nd > 0 else "rear axle centre",
                         "path_radius": round(r, 2), "length": round(sum(ln for _, ln in merged), 2),
                         "vertices": _segments_to_vertices((sx, sy), stau, merged, r)})
    min_clear = float(clearance_many(arr[:, 0], arr[:, 1], arr[:, 2]).min())
    endp = arr[-1]
    jumps = []
    for sgm in segments:
        v = sgm["vertices"]
        for i in range(len(v) - 2):
            t_end = math.atan2(v[i + 1][1] - v[i][1], v[i + 1][0] - v[i][0]) + 2 * math.atan(v[i][2])
            t_start = math.atan2(v[i + 2][1] - v[i + 1][1], v[i + 2][0] - v[i + 1][0]) - 2 * math.atan(v[i + 1][2])
            jumps.append(abs(math.degrees(wrap(t_start - t_end))))
    result = {**base, "success": True, "found": True, "smoothed": smoothed is not None,
              "gear_changes": len(segments) - 1,
              "total_length": round(sum(s_["length"] for s_ in segments), 2),
              "min_clearance": round(min_clear, 2),
              "clearance_ok": min_clear >= margin_user,
              "max_steer_deg": round(math.degrees(dmx), 2),
              "path_tangent_jump_max_deg": round(max(jumps), 4) if jumps else 0.0,
              "model": ("forward: front axle follows straights + arcs (tangent-continuous, steering changes "
                        "continuously), car by the exact QuickTurn tractrix; reverse: rear axle follows straights "
                        "+ arcs with the car axis tangent (CAYENNE 'Tyl')"),
              "end_error": {"distance": round(math.hypot(endp[0] - gx, endp[1] - gy), 3),
                            "heading_deg": round(math.degrees(wrap(endp[2] - goal[2])), 3)},
              "segments": segments}
    result["_lattice"] = lat
    return result


def plan_route(boundary, start_vec, end_vec, vehicle: dict, units: str = "cm", *, time_budget_s: float = 120.0,
               max_gear_changes: int = 3, margin_mm: float = 100.0, allow_reverse: bool = True,
               anchor: str = "centre", **kw) -> dict[str, Any]:
    """Stage 1: fast rigid-model search for the corridor and the gear changes (route_rigid).
    Stage 2: the route the car really drives, built like QuickTurn2's dynamic path (route_qt): tangent-continuous
    arcs with R >= RMIN for the guided axle, car pose by the exact tractrix; waypoints optimised for clearance."""
    from zwcad_standard_mcp import route_qt, route_rigid

    require_numpy()
    t0 = time.monotonic()
    ref = route_rigid.plan_route(boundary, start_vec, end_vec, vehicle, units, max_gear_changes=max_gear_changes,
                                 time_budget_s=min(60.0, 0.4 * time_budget_s), margin_mm=margin_mm,
                                 allow_reverse=allow_reverse, anchor=anchor)
    reference = ref.pop("_reference", None)
    ref.pop("_lattice", None)
    if not ref.get("found") or reference is None:
        ref["stage"] = "corridor"
        ref.pop("segments", None)
        return ref
    k = UNIT_PER_MM[units]
    car = Car.from_vehicle(vehicle, units)
    offsets = {"centre": 0.5 * (car.front - car.rear), "rear_axle": 0.0, "front_axle": car.L}
    start = vector_pose(start_vec, offsets[anchor], anchor)
    goal = vector_pose(end_vec, offsets[anchor], anchor)
    pts = np.asarray(boundary, dtype=float)[:, :2]
    left = max(10.0, time_budget_s - (time.monotonic() - t0))
    res = route_qt.solve(pts, start, goal, car, 0.95 / car.rmin, margin_mm * k + 10.0 * k, 50.0 * k,
                         reference, left, margin_mm * k)
    out = {"units": units, "allow_reverse": allow_reverse, "margin": round(margin_mm * k, 3),
           "start_rear_axle": [round(v, 3) for v in start], "goal_rear_axle": [round(v, 3) for v in goal],
           "corridor": {"gear_changes": ref["gear_changes"], "length": ref["total_length"],
                        "elapsed_s": ref["elapsed_s"]},
           "model": ("QuickTurn2 dynamic path: the guided axle (front when driving forward, rear when reversing) "
                     "follows tangent-continuous arcs with R >= RMIN = wheelbase / sin(lock); the car follows by "
                     "the exact tractrix step; reverse keeps the car axis tangent to the rear-axle path"),
           "front_axle_min_radius": round(math.hypot(car.rmin / 0.95, car.L), 2),
           "rear_axle_min_radius": round(car.rmin / 0.95, 2), **res}
    if not res["found"]:
        out["reason"] = ("a corridor was found, but the drivable route (continuous steering) does not keep the "
                         f"margin: min clearance {res['min_clearance']} < {round(margin_mm * k, 2)}, end reached: "
                         f"{res['end_error']}; try a smaller margin_mm or more time")
    out["elapsed_s"] = round(time.monotonic() - t0, 1)
    return out
