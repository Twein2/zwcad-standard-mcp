"""Vehicle route planner with steering in place (plan_vehicle_route, FINDINGS #62-#66).

Model (kinematic bicycle, rear axle guided, the same model as the owner's JAZDA simulator):

* state = rear axle (x, y), heading, wheel angle delta, gear;
* while the car moves the wheel angle changes continuously, at most ``steer_rate`` per metre (one steering level
  per search step); ``|delta| <= lock`` with lock = atan(L / RR) from the vehicle data (no hidden reserve);
* the car may stop and turn the wheels to any angle (a *stop*): at every gear change and, with
  ``allow_steer_in_place``, anywhere inside a run, for a cost of ``stop_penalty`` metres of driving;
* the whole car with mirrors keeps ``margin`` from the boundary (signed distance field on the outline plus an exact
  test of the boundary corners against the car rectangles); the final route is re-checked exactly.

Search: hybrid A* over (x, y, heading, wheel angle, gear) with an obstacle-aware heading-lattice heuristic, exact
Dubins tails to the goal (wheel angle changes in the tail are stops) and anytime refinement in the remaining time.
Goals are a pose (with optional tolerances) or a region (a bay rectangle: the car centre anywhere inside, heading
along the long axis either way round).

Output per gear: the rear-axle path as LWPOLYLINE vertices [x, y, bulge] in driving order (one arc per constant
wheel angle), the stops (position, wheel angle before -> after) and the wheel angle at the start of the run.
"""
from __future__ import annotations

import heapq
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from zwcad_standard_mcp.route_check import car_rects, contact, exact_clearance, point_in_polygon
from zwcad_standard_mcp.route_planner import Car, FreeSpace, HeadingLattice, arc_end, dubins_words, wrap


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- collision model

class Model:
    """Clearance of car poses against the boundary: SDF on the outline + boundary corners vs the car rectangles."""

    def __init__(self, poly, car: Car, res: float):
        self.car, self.res = car, res
        self.poly = np.asarray(poly, dtype=float)[:, :2]
        self.fs = FreeSpace(self.poly, res, pad=max(10 * res, car.front + car.rear))   # a start may stick out
        self.outline = car.outline(spacing=res)
        self.rects = car_rects(car)
        # boundary vertices that point into the free space (reflex corners): the outline samples can miss them
        p = self.poly
        area = 0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1]))
        prev, nxt = np.roll(p, 1, axis=0), np.roll(p, -1, axis=0)
        cross = (p[:, 0] - prev[:, 0]) * (nxt[:, 1] - p[:, 1]) - (p[:, 1] - prev[:, 1]) * (nxt[:, 0] - p[:, 0])
        reflex = cross < 0 if area > 0 else cross > 0
        self.corners = p[reflex] if reflex.any() else p[:0]
        n_out = 12
        self.cx_out = np.linspace(-car.rear, car.front, n_out)
        half_gap = (car.front + car.rear) / (2 * (n_out - 1))
        self.r_out = math.hypot(max(car.hw, car.mirror[2]), half_gap) + res      # + res: SDF interpolation
        x_in0, x_in1 = -car.rear + car.hw, car.front - car.hw
        self.cx_in = np.linspace(x_in0, x_in1, max(2, int(math.ceil((x_in1 - x_in0) / car.hw)) + 1))
        self.r_in = car.hw

    def clearance(self, P) -> np.ndarray:
        """Approximate signed clearance (exact for straight walls and for boundary corners)."""
        ox, oy = self.outline[:, 0], self.outline[:, 1]
        c, s = np.cos(P[:, 2])[:, None], np.sin(P[:, 2])[:, None]
        X = P[:, 0][:, None] + c * ox[None, :] - s * oy[None, :]
        Y = P[:, 1][:, None] + s * ox[None, :] + c * oy[None, :]
        clr = self.fs.sample(X, Y).min(axis=1)
        if len(self.corners):
            dx = self.corners[:, 0][None, :] - P[:, 0][:, None]
            dy = self.corners[:, 1][None, :] - P[:, 1][:, None]
            u, v = c * dx + s * dy, -s * dx + c * dy
            best = np.full(u.shape, np.inf)
            for x0, x1, y0, y1 in self.rects:
                ex = np.maximum(np.maximum(x0 - u, u - x1), 0.0)
                ey = np.maximum(np.maximum(y0 - v, v - y1), 0.0)
                outside = np.hypot(ex, ey)
                inside = np.minimum.reduce([u - x0, x1 - u, v - y0, y1 - v])
                best = np.minimum(best, np.where(inside > 0, -inside, outside))
            clr = np.minimum(clr, best.min(axis=1))
        return clr

    def free(self, P, thr: float) -> np.ndarray:
        """clearance >= thr for every pose (two-sided disk pre-test, exact-ish test for the unsure ones)."""
        c, s = np.cos(P[:, 2])[:, None], np.sin(P[:, 2])[:, None]
        d_in = self.fs.sample(P[:, 0][:, None] + c * self.cx_in[None, :],
                              P[:, 1][:, None] + s * self.cx_in[None, :]).min(axis=1)
        result = d_in >= self.r_in + thr
        d_out = self.fs.sample(P[:, 0][:, None] + c * self.cx_out[None, :],
                               P[:, 1][:, None] + s * self.cx_out[None, :]).min(axis=1)
        unsure = result & (d_out < self.r_out + thr)
        if unsure.any():
            idx = np.nonzero(unsure)[0]
            result[idx] = self.clearance(P[idx]) >= thr
        return result


# --------------------------------------------------------------------------- goals

@dataclass
class Goal:
    kind: str                                  # "pose" | "region"
    targets: list                              # exact rear-axle poses for the analytic tails
    seeds: list                                # rear-axle poses seeding the lattice heuristic
    test: Callable[[float, float, float], bool]
    info: dict = field(default_factory=dict)


def pose_goal(pose, pos_tol: float, head_tol: float) -> Goal:
    gx, gy, gth = pose

    def test(x, y, th):
        return math.hypot(x - gx, y - gy) <= pos_tol and abs(wrap(th - gth)) <= head_tol

    return Goal("pose", [tuple(pose)], [tuple(pose)], test,
                {"position_tolerance": pos_tol, "heading_tolerance_deg": round(math.degrees(head_tol), 3)})


def region_frame(rect):
    """Centre, long-axis angle, half length and half width of a bay polygon (oriented by its longest edge)."""
    R = np.asarray(rect, dtype=float)[:, :2]
    if len(R) > 1 and np.allclose(R[0], R[-1]):
        R = R[:-1]
    edges = np.roll(R, -1, axis=0) - R
    i = int(np.argmax(np.hypot(edges[:, 0], edges[:, 1])))
    axis = math.atan2(edges[i, 1], edges[i, 0])
    u = np.array([math.cos(axis), math.sin(axis)])
    n = np.array([-u[1], u[0]])
    c = R.mean(axis=0)
    a, b = (R - c) @ u, (R - c) @ n
    c = c + u * float(a.max() + a.min()) / 2 + n * float(b.max() + b.min()) / 2
    return R, c, axis, float(a.max() - a.min()) / 2, float(b.max() - b.min()) / 2


def region_starts(rect, car: Car, head_tol: float, which: int) -> list:
    """Candidate start poses (rear axle) in a bay: car centre on the bay axis, heading = long axis (which=0) or
    the opposite direction (which=1), ± head_tol."""
    _, centre, axis, half_len, _ = region_frame(rect)
    u = np.array([math.cos(axis), math.sin(axis)])
    h = axis + (math.pi if which else 0.0)
    c_off = 0.5 * (car.front - car.rear)
    room = max(0.0, half_len - 0.5 * (car.front + car.rear))
    out = []
    for t in sorted({0.0, room, -room, 0.5 * room, -0.5 * room}):
        cpt = centre + u * t
        for dh in (0.0, -head_tol, head_tol, -0.5 * head_tol, 0.5 * head_tol):
            hh = h + dh
            out.append((float(cpt[0] - c_off * math.cos(hh)), float(cpt[1] - c_off * math.sin(hh)), wrap(hh)))
    return out


def region_goal(rect, car: Car, head_tol: float, res: float, which: int | None = None) -> Goal:
    """Bay rectangle (closed polyline, 4+ points): car centre inside, heading along the long axis ± head_tol
    (which = 0: along the first long edge, 1: opposite, None: either way)."""
    R, centre, axis, half_len, half_wid = region_frame(rect)
    u = np.array([math.cos(axis), math.sin(axis)])
    n = np.array([-u[1], u[0]])
    c_off = 0.5 * (car.front - car.rear)               # rear axle -> body centre
    headings = [axis, axis + math.pi] if which is None else [axis + (math.pi if which else 0.0)]
    targets, seeds = [], []
    shifts = [0.0]
    room = max(0.0, half_len - 0.5 * (car.front + car.rear))
    if room > 0:
        shifts += [room, -room]
    for h in headings:
        hu = np.array([math.cos(h), math.sin(h)])
        for t in shifts:
            cpt = centre + u * t
            targets.append((float(cpt[0] - c_off * hu[0]), float(cpt[1] - c_off * hu[1]), wrap(h)))
        for t in np.arange(-half_len, half_len + 1e-9, max(res, half_len / 4)):
            for w in np.arange(-half_wid, half_wid + 1e-9, max(res, half_wid / 2)):
                cpt = centre + u * t + n * w
                for dh in (-head_tol, 0.0, head_tol):
                    hh = h + dh
                    seeds.append((float(cpt[0] - c_off * math.cos(hh)), float(cpt[1] - c_off * math.sin(hh)), wrap(hh)))

    def test(x, y, th):
        cx, cy = x + c_off * math.cos(th), y + c_off * math.sin(th)
        if not point_in_polygon(np.array([cx]), np.array([cy]), R)[0]:
            return False
        return min(abs(wrap(th - h)) for h in headings) <= head_tol

    return Goal("region", targets, seeds, test,
                {"heading_deg": [round(math.degrees(wrap(h_)), 2) for h_ in headings], "heading_tolerance_deg": round(math.degrees(head_tol), 2),
                 "centre": [round(float(centre[0]), 3), round(float(centre[1]), 3)],
                 "size": [round(2 * half_len, 2), round(2 * half_wid, 2)]})


# --------------------------------------------------------------------------- planner

def _word_poses(q0, pieces, r: float, spacing: float, flip: float) -> np.ndarray:
    """Rear-axle poses along Dubins pieces [(sign, length)] from q0 (flipped frame for reverse), vectorised."""
    out = []
    cx, cy, cth = q0
    for sg, ln in pieces:
        n = max(1, int(math.ceil(ln / spacing)))
        s_ = ln * np.arange(1, n + 1) / n
        if sg == 0:
            X, Y, T = cx + s_ * math.cos(cth), cy + s_ * math.sin(cth), np.full(n, cth)
        else:
            k = sg / r
            T = cth + k * s_
            X = cx + (np.sin(T) - math.sin(cth)) / k
            Y = cy - (np.cos(T) - math.cos(cth)) / k
        out.append(np.column_stack([X, Y, T - flip]))
        cx, cy, cth = float(X[-1]), float(Y[-1]), float(T[-1])
    return np.vstack(out)


@dataclass
class Options:
    margin: float
    lock: float
    allow_reverse: bool = True
    max_gear_changes: int = 3
    allow_steer_in_place: bool = True
    stop_penalty: float = 200.0                # drawing units of driving
    steer_rate_deg_per_m: float = 30.0
    step: float = 25.0
    n_sub: int = 5
    rev_w: float = 1.3
    switch_cost: float = 400.0
    min_run: float = 150.0
    xy_bin: float = 10.0
    th_bin_deg: float = 2.0
    h_weight: float = 2.0
    start_collision: str = "warn"
    unit_m: float = 100.0                      # drawing units per metre


class Planner:
    def __init__(self, poly, car: Car, starts, goal: Goal, opt: Options, res: float,
                 progress: Callable[[dict], None] | None = None, cancel: threading.Event | None = None,
                 model: Model | None = None, lattice=None):
        """starts: one rear-axle pose or a list of candidate start poses (start region)."""
        if len(starts) == 3 and not hasattr(starts[0], "__len__"):
            starts = [starts]
        self.car, self.goal, self.opt, self.res = car, goal, opt, res
        self.model = model if model is not None else Model(poly, car, res)
        self.progress = progress or (lambda info: None)
        self.cancel = cancel
        L = car.L
        self.L = L
        self.lock = opt.lock
        self.r_min = L / math.tan(self.lock)
        step_deg = opt.steer_rate_deg_per_m * opt.step / opt.unit_m
        self.nL = max(1, int(math.ceil(math.degrees(self.lock) / max(step_deg, 1e-6))))
        self.levels = [self.lock * i / self.nL for i in range(-self.nL, self.nL + 1)]   # index = level + nL
        half = int(round(self.nL / 2))
        self.stop_levels = sorted({-self.nL, -half, 0, half, self.nL})
        self.dirs = (1, -1) if opt.allow_reverse else (1,)
        ds = opt.step / opt.n_sub
        self.prims: dict[tuple[int, int, int], np.ndarray] = {}
        for d in self.dirs:
            for l0 in range(-self.nL, self.nL + 1):
                for l1 in (l0 - 1, l0, l0 + 1):
                    if abs(l1) > self.nL:
                        continue
                    x = y = th = 0.0
                    sub = []
                    for k in range(1, opt.n_sub + 1):
                        dl = self.levels[l0 + self.nL] + (self.levels[l1 + self.nL] - self.levels[l0 + self.nL]) * k / opt.n_sub
                        x, y, th = arc_end(x, y, th, math.tan(dl) / L, d * ds)
                        sub.append((x, y, th))
                    self.prims[(d, l0, l1)] = np.array(sub)
        self.stats = {"expansions": 0, "analytic_tries": 0}
        self.lattice = lattice
        clr = self.model.clearance(np.array([tuple(p_) for p_ in starts], dtype=float))
        roots = [(tuple(map(float, p_)), float(c_)) for p_, c_ in zip(starts, clr)]
        clear_roots = [r_ for r_ in roots if r_[1] >= opt.margin]
        # a start region: use only the poses that keep the margin when there are any
        self.roots = clear_roots or roots
        self.start = max(self.roots, key=lambda r_: r_[1])[0]
        self.start_clear = max(r_[1] for r_ in self.roots)
        self.nonesc_thr = min(opt.margin, min(r_[1] for r_ in self.roots))

    # ------------------------------------------------------------------ helpers
    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def _thr(self, escaped: bool) -> float:
        if escaped:
            return self.opt.margin
        return self.nonesc_thr

    def build_lattice(self, seeds_from_goal: bool = True):
        res_xy = 25.0 * self.car.k * 10.0       # 25 cm
        free_many = lambda xs, ys, ths, certain_only=True: self._lattice_free(xs, ys, ths)  # noqa: E731
        self.lattice = HeadingLattice(free_many, self.model.poly, self.goal.seeds[0], self.r_min * 0.8,
                                      res_xy=res_xy, n_head=24, allow_reverse=self.opt.allow_reverse,
                                      rev_w=self.opt.rev_w, goals=self.goal.seeds)
        return self.lattice

    def _lattice_free(self, xs, ys, ths):
        # relaxed (certain collision only, body without mirrors): an unreachable start means no route exists
        m = self.model
        c, s = np.cos(ths)[:, None], np.sin(ths)[:, None]
        d_in = m.fs.sample(xs[:, None] + c * m.cx_in[None, :], ys[:, None] + s * m.cx_in[None, :]).min(axis=1)
        return d_in >= m.r_in + min(self.opt.margin, 0.0)

    def heur(self, x, y, th) -> float:
        v = self.lattice.cost(x, y, th)
        e = min(math.hypot(x - t[0], y - t[1]) for t in self.goal.targets)
        return max(v, e) if v < math.inf else 3 * e + 10 * self.car.front

    # ------------------------------------------------------------------ analytic tail
    def tail(self, x, y, th, lvl, d, run_len, gc, escaped):
        """Dubins tail of the rear axle to each target, in the current gear or after one gear change."""
        if not escaped:
            return None
        opt = self.opt
        gears = []
        if d == 0:
            gears = [(g, False) for g in self.dirs]
        else:
            gears.append((d, False))
            if opt.allow_reverse and run_len >= opt.min_run - 1e-6 and gc < opt.max_gear_changes:
                gears.append((-d, True))
        best = None
        r = self.r_min
        for tgt in self.goal.targets:
            dist = math.hypot(x - tgt[0], y - tgt[1])
            for g, switching in gears:
                flip = 0.0 if g > 0 else math.pi
                q0, q1 = (x, y, th + flip), (tgt[0], tgt[1], tgt[2] + flip)
                for total, segs in dubins_words(q0, q1, r)[:4]:
                    if total > 3 * dist + 4 * self.car.front:
                        break
                    pieces = [(sg, ln) for sg, ln in segs if ln > 1e-9]
                    # wheel angle of each piece (reverse: path curvature sign flips the steering)
                    dls = [sg * g for sg, _ in pieces]
                    stops = sum(1 for a, b in zip(dls[:-1], dls[1:]) if a != b)
                    cur = self.levels[lvl + self.nL] / self.lock
                    first_stop = 0 if (d == 0 or switching) else (1 if abs(dls[0] - cur) > 1e-9 else 0)
                    stops += first_stop
                    if stops and not opt.allow_steer_in_place:
                        continue
                    cost = total * (opt.rev_w if g < 0 else 1.0) + stops * opt.stop_penalty + \
                        (opt.switch_cost if switching else 0.0)
                    if best is not None and cost >= best[0]:
                        continue
                    P = _word_poses(q0, pieces, r, self.res, flip)
                    # cheap rejection on every 4th pose first, then all
                    if not self.model.free(P[3::4], opt.margin).all() or not self.model.free(P, opt.margin).all():
                        continue
                    best = (cost, g, switching, [(dl_ * self.lock, ln) for dl_, (_, ln) in zip(dls, pieces)], tgt)
        return best

    # ------------------------------------------------------------------ search
    def search(self, deadline: float, h_weight: float, bound: float = math.inf):
        opt = self.opt
        xyb, thb = opt.xy_bin, math.radians(opt.th_bin_deg)

        def key(x, y, th, lvl, d, esc):
            return (int(round(x / xyb)), int(round(y / xyb)), int(round(wrap(th) / thb)), lvl, d, esc)

        # node: x, y, th, lvl, dir, parent, g, gc, run_len, escaped, prim, stop(from_lvl or None), switch
        nodes, best_g, openq = [], {}, []
        closest = (math.inf, 0)
        for (sx, sy, sth), sc in self.roots:
            esc0 = sc >= opt.margin
            nodes.append((sx, sy, sth, 0, 0, -1, 0.0, 0, 0.0, esc0, None, None, False))
            best_g[key(sx, sy, sth, 0, 0, esc0)] = 0.0
            h0 = self.heur(sx, sy, sth)
            heapq.heappush(openq, (h_weight * h0, len(nodes) - 1))
            closest = min(closest, (h0, len(nodes) - 1))
        found = None
        it = 0
        t_last = time.monotonic()
        while openq:
            if time.monotonic() > deadline:
                return None, nodes, closest, "timeout"
            it += 1
            if it % 200 == 0:
                self._check_cancel()
                if time.monotonic() - t_last > 2.0:
                    t_last = time.monotonic()
                    self.progress({"expansions": self.stats["expansions"] + it})
            f, ni = heapq.heappop(openq)
            x, y, th, lvl, d, _, g, gc, run_len, esc, _, _, _ = nodes[ni]
            if g > best_g.get(key(x, y, th, lvl, d, esc), math.inf) + 1e-9:
                continue
            hv = self.heur(x, y, th)
            if g + hv >= bound:          # the heuristic never overestimates (relaxed lattice / straight line)
                continue
            improved = hv < closest[0]
            if improved:
                closest = (hv, ni)
            if esc and self.goal.test(x, y, th) and d != 0:
                found = (ni, None)
                break
            if hv < 30 * self.L and (improved or it % 6 == 1):
                self.stats["analytic_tries"] += 1
                t = self.tail(x, y, th, lvl, d, run_len, gc, esc)
                if t is not None and g + t[0] < bound:
                    found = (ni, t)
                    break
            cands = []
            for nd in self.dirs:
                switching = d != 0 and nd != d
                if switching and (gc >= opt.max_gear_changes or run_len < opt.min_run - 1e-6):
                    continue
                if d == 0 or switching:
                    for l0 in self.stop_levels:
                        cands.append((nd, l0, l0, 0.0, None, switching))
                    continue
                for j in (-1, 0, 1):
                    l1 = lvl + j
                    if abs(l1) <= self.nL:
                        cands.append((nd, lvl, l1, 0.0, None, False))
                if opt.allow_steer_in_place:
                    for l0 in self.stop_levels:
                        if abs(l0 - lvl) >= 2:
                            cands.append((nd, l0, l0, opt.stop_penalty, lvl, False))
            if not cands:
                continue
            c, s = math.cos(th), math.sin(th)
            subs = [self.prims[(nd, l0, l1)] for nd, l0, l1, _, _, _ in cands]
            A = np.vstack(subs)
            X = x + c * A[:, 0] - s * A[:, 1]
            Y = y + s * A[:, 0] + c * A[:, 1]
            T = th + A[:, 2]
            P = np.column_stack([X, Y, T])
            n_sub = opt.n_sub
            if esc:
                okm = self.model.free(P, opt.margin).reshape(len(cands), n_sub).all(axis=1)
                escm = np.ones(len(cands), dtype=bool)
            else:
                clr = self.model.clearance(P).reshape(len(cands), n_sub)
                okm = (clr >= self._thr(False)).all(axis=1)
                escm = clr[:, -1] >= opt.margin
            for ci, (nd, l0, l1, extra, stop_from, switching) in enumerate(cands):
                if not okm[ci]:
                    continue
                k_ = ci * n_sub + n_sub - 1
                cx, cy, cth = float(X[k_]), float(Y[k_]), float(T[k_])
                dl = self.levels[l1 + self.nL]
                cost = opt.step * (opt.rev_w if nd < 0 else 1.0) + extra
                cost += 0.05 * opt.step * abs(dl) / self.lock + (0.03 * opt.step if l1 != l0 else 0.0)
                if switching:
                    cost += opt.switch_cost
                ng = g + cost
                if ng >= bound:
                    continue
                nesc = bool(esc or escm[ci])
                kk = key(cx, cy, cth, l1, nd, nesc)
                if ng < best_g.get(kk, math.inf) - 1e-9:
                    best_g[kk] = ng
                    nrun = opt.step if (switching or d == 0) else run_len + opt.step
                    nodes.append((cx, cy, cth, l1, nd, ni, ng, gc + (1 if switching else 0), nrun, nesc,
                                  (nd, l0, l1), stop_from, switching))
                    heapq.heappush(openq, (ng + h_weight * self.heur(cx, cy, cth), len(nodes) - 1))
        self.stats["expansions"] += it
        if found is None:
            return None, nodes, closest, "exhausted"
        return found, nodes, closest, "found"

    # ------------------------------------------------------------------ route reconstruction
    def route_from(self, found, nodes) -> dict:
        ni, tail = found
        chain = []
        while nodes[ni][5] >= 0:
            chain.append(nodes[ni])
            ni = nodes[ni][5]
        chain.reverse()
        root = nodes[ni]
        # steps: (dir, [(delta, length)], stop_before: (from_delta, to_delta) | None)
        steps = []
        ds = self.opt.step / self.opt.n_sub
        for n in chain:
            nd, l0, l1 = n[10]
            a0, a1 = self.levels[l0 + self.nL], self.levels[l1 + self.nL]
            subs = [(a0 + (a1 - a0) * k / self.opt.n_sub, ds) for k in range(1, self.opt.n_sub + 1)]
            stop = (self.levels[n[11] + self.nL], a0) if n[11] is not None else None
            steps.append((nd, subs, stop))
        if tail is not None:
            _, g, _switching, pieces, _tgt = tail
            prev = steps[-1][1][-1][0] if steps else None
            prev_dir = steps[-1][0] if steps else 0
            for dl, ln in pieces:
                n = max(1, int(math.ceil(ln / ds)))
                stop = None
                if prev is not None and prev_dir == g and abs(dl - prev) > 1e-9:
                    stop = (prev, dl)
                steps.append((g, [(dl, ln / n)] * n, stop))
                prev, prev_dir = dl, g
        return self._assemble(steps, root[:3])

    def _assemble(self, steps, start) -> dict:
        x, y, th = start
        runs = []
        for nd, subs, stop in steps:
            if not runs or runs[-1]["dir"] != nd:
                runs.append({"dir": nd, "poses": [(x, y, th)], "subs": [], "stops": [], "length": 0.0,
                             "start_delta": subs[0][0]})
            run = runs[-1]
            if stop is not None and run["subs"]:
                run["stops"].append({"at": run["length"], "pos": (x, y), "before": stop[0], "after": stop[1]})
            for dl, ln in subs:
                x, y, th = arc_end(x, y, th, math.tan(dl) / self.L, nd * ln)
                run["poses"].append((x, y, th))
                run["subs"].append((dl, ln))
                run["length"] += ln
        return {"runs": runs, "start": tuple(start), "end": (x, y, th)}

    # ------------------------------------------------------------------ diagnosis
    def stuck(self, nodes, closest) -> dict:
        """Where the search got stuck: the explored state closest to the goal and what blocks it."""
        _, ni = closest
        x, y, th, lvl, d = nodes[ni][:5]
        out = {"rear_axle": [round(x, 2), round(y, 2)], "heading_deg": round(math.degrees(wrap(th)), 2),
               "gear": {1: "forward", -1: "reverse", 0: "start"}[d]}
        blocks = []
        for nd in self.dirs:
            for l0 in self.stop_levels:
                P = self.prims[(nd, l0, l0)]
                c, s = math.cos(th), math.sin(th)
                Q = np.column_stack([x + c * P[:, 0] - s * P[:, 1], y + s * P[:, 0] + c * P[:, 1], th + P[:, 2]])
                ok = self.model.free(Q, self.opt.margin)
                if not ok.all():
                    k = int(np.argmin(ok))
                    ct = contact(tuple(Q[k]), self.car, self.model.poly)
                    blocks.append({"move": ("forward" if nd > 0 else "reverse") + f", wheels {math.degrees(self.levels[l0 + self.nL]):+.1f} deg",
                                   **ct})
        # the most common contact
        out["blocked_moves"] = blocks[:6]
        return out


def runs_to_segments(route: dict, lock: float, L: float) -> list[dict]:
    """Rear-axle path per gear as LWPOLYLINE vertices (one arc per constant wheel angle) + stops."""
    segments = []
    prev_end_delta = None
    for run in route["runs"]:
        x, y, th = run["poses"][0]
        fwd = run["dir"] > 0
        verts = []
        merged: list[list[float]] = []
        for dl, ln in run["subs"]:
            if merged and abs(merged[-1][0] - dl) < 1e-12:
                merged[-1][1] += ln
            else:
                merged.append([dl, ln])
        dist = 0.0
        stop_at = [s_["at"] for s_ in run["stops"]]
        cut_pts = []
        for dl, ln in merged:
            kappa = math.tan(dl) / L
            dth = kappa * ln * (1 if fwd else -1)          # change of the car heading = change of the path tangent
            verts.append([round(x, 4), round(y, 4), round(math.tan(dth / 4.0), 8) if abs(dth) > 1e-12 else 0.0])
            x, y, th = arc_end(x, y, th, kappa, ln * (1 if fwd else -1))
            dist += ln
            cut_pts.append(dist)
        verts.append([round(x, 4), round(y, 4), 0.0])
        stops = [{"position": [round(s_["pos"][0], 3), round(s_["pos"][1], 3)],
                  "distance": round(s_["at"], 2),
                  "wheel_angle_before_deg": round(math.degrees(s_["before"]), 2),
                  "wheel_angle_after_deg": round(math.degrees(s_["after"]), 2)} for s_ in run["stops"]]
        _ = stop_at
        seg = {"gear": "forward" if fwd else "reverse", "direction": "forward" if fwd else "reverse",
               "path": "rear axle centre", "length": round(run["length"], 2), "vertices": verts,
               "wheel_angle_start_deg": round(math.degrees(run["start_delta"]), 2),
               "wheel_angle_end_deg": round(math.degrees(run["subs"][-1][0]), 2), "stops": stops,
               "max_steer_deg": round(math.degrees(max(abs(dl) for dl, _ in run["subs"])), 2)}
        if prev_end_delta is not None:
            seg["wheel_angle_before_deg"] = round(math.degrees(prev_end_delta), 2)
        prev_end_delta = run["subs"][-1][0]
        segments.append(seg)
    return segments


def route_poses(route: dict, L: float, every: float) -> np.ndarray:
    """Dense poses along the route (for the exact clearance check), spacing <= every."""
    out = []
    for run in route["runs"]:
        x, y, th = run["poses"][0]
        out.append((x, y, th))
        sgn = 1 if run["dir"] > 0 else -1
        for dl, ln in run["subs"]:
            n = max(1, int(math.ceil(ln / every)))
            k = math.tan(dl) / L
            for i in range(1, n + 1):
                out.append(arc_end(x, y, th, k, sgn * ln * i / n))
            x, y, th = out[-1]
    return np.array(out)


def _summary(route: dict) -> dict:
    return {"gear_changes": len(route["runs"]) - 1, "stops": sum(len(r["stops"]) for r in route["runs"]),
            "length": round(sum(r["length"] for r in route["runs"]), 1)}


def _rank(route: dict) -> tuple:
    """Fewest gear changes, then shortest length (then fewer stops)."""
    sm = _summary(route)
    return (sm["gear_changes"], sm["length"], sm["stops"])


def solve(poly, car: Car, start_variants, goal_variants, opt: Options, time_budget_s: float, res: float,
          progress: Callable[[dict], None] | None = None, cancel: threading.Event | None = None,
          search_share: float = 1.0) -> dict[str, Any]:
    """start_variants: [(label, [rear-axle poses])]; goal_variants: [(label, Goal)].

    Stage 'heuristic': lattice per goal (also answers impossible cases). Stage 'search': first route for every
    start/goal combination (only as long as needed). Stage 'refine': all remaining time goes to the chosen
    combination (anytime search with a lower heuristic weight and the best cost as a bound).
    """
    t0 = time.monotonic()
    deadline = t0 + time_budget_s
    search_deadline = t0 + search_share * time_budget_s
    report = progress or (lambda info: None)

    def rep(stage, **kw):
        report({"stage": stage, "elapsed_s": round(time.monotonic() - t0, 1), **kw})

    model = Model(poly, car, res)
    rep("heuristic")
    lattices = {}
    planners = []
    for gl, goal in goal_variants:
        for sl, starts in start_variants:
            pl = Planner(poly, car, starts, goal, opt, res, cancel=cancel, model=model, lattice=lattices.get(gl),
                         progress=lambda info: rep("search", **info))
            if pl.lattice is None:
                lattices[gl] = pl.build_lattice()
            planners.append((sl, gl, pl))
    base: dict[str, Any] = {"heuristic_s": round(time.monotonic() - t0, 2),
                            "steering_levels": len(planners[0][2].levels),
                            "stop_levels_deg": [round(math.degrees(planners[0][2].levels[l_ + planners[0][2].nL]), 2)
                                                for l_ in planners[0][2].stop_levels]}
    if opt.start_collision == "strict":
        bad = [pl for _, _, pl in planners if pl.start_clear < opt.margin]
        if len(bad) == len(planners):
            c_ = max(pl.start_clear for pl in bad)
            return {**base, "found": False, "success": False, "stage": "input",
                    "reason": (f"The start pose is closer to the boundary than the margin (clearance {c_:.1f} < "
                               f"{opt.margin:.1f} drawing units); start_collision='warn' plans from it anyway.")}
        planners = [p_ for p_ in planners if p_[2].start_clear >= opt.margin]
    # (a start that collides cannot be judged by the relaxed lattice: it is searched anyway)
    reachable = [p_ for p_ in planners
                 if not all(p_[2].lattice.unreachable(r_[0]) and r_[1] >= min(opt.margin, 0.0) for r_ in p_[2].roots)]
    if not reachable:
        return {**base, "found": False, "success": False, "exhausted": True, "stage": "heuristic",
                "elapsed_s": round(time.monotonic() - t0, 2),
                "reason": ("the end cannot be reached from the start"
                           + (" without reversing (checked on a relaxed model: 80 % of the minimum turning radius, "
                              "body without mirrors); set allow_reverse=true" if not opt.allow_reverse
                              else " even with reversing (relaxed model: 80 % of the minimum turning radius, body "
                                   "without mirrors); the space is too tight for this vehicle"))}
    variants, results = [], []
    first_found_s = 0.0
    try:
        for i, (sl, gl, pl) in enumerate(reachable):
            left = search_deadline - time.monotonic()
            if left <= 0:
                variants.append({"start": sl, "end": gl, "found": False, "status": "not searched (no time left)"})
                continue
            share = left if len(reachable) == 1 else max(0.6 * left / (len(reachable) - i), min(left, 20.0))
            if results:   # a route exists already: the other variants only get a comparable time
                share = min(share, max(15.0, 2.0 * first_found_s))
            rep("search", variant=f"start {sl}, end {gl}")
            t_v = time.monotonic()
            found, nodes, closest, status = pl.search(time.monotonic() + share, opt.h_weight)
            if found is not None and not results:
                first_found_s = time.monotonic() - t_v
            entry = {"start": sl, "end": gl, "found": found is not None, "status": status}
            if found is not None:
                route = pl.route_from(found, nodes)
                cost = nodes[found[0]][6] + (found[1][0] if found[1] else 0.0)
                entry.update(_summary(route))
                results.append((_rank(route), cost, route, pl, sl, gl))
                best = min(results, key=lambda r_: r_[0])
                rep("search", best_so_far=_summary(best[2]))
            else:
                entry["stuck"] = pl.stuck(nodes, closest)
            variants.append(entry)
    except Cancelled:
        if not results:
            return {**base, "found": False, "success": False, "cancelled": True, "stage": "search",
                    "elapsed_s": round(time.monotonic() - t0, 1), "variants": variants}
    if not results:
        st = next((v_["stuck"] for v_ in variants if "stuck" in v_), None)
        out = {**base, "found": False, "success": False, "stage": "search",
               "exhausted": all(v_["status"] == "exhausted" for v_ in variants),
               "timed_out": any(v_["status"] != "exhausted" for v_ in variants),
               "elapsed_s": round(time.monotonic() - t0, 1), "variants": variants, "stuck": st}
        where = ""
        if st:
            where = (f"; closest explored pose: rear axle ({st['rear_axle'][0]}, {st['rear_axle'][1]}), heading "
                     f"{st['heading_deg']} deg")
            if st["blocked_moves"]:
                b = st["blocked_moves"][0]
                where += f" ({b['move']}: {b['car_part']} hits edge {b['edge_index']} at {b['point']})"
        out["reason"] = (("search space exhausted: no route at this margin" if out["exhausted"]
                          else f"no route found within {time_budget_s:.0f} s") + where)
        return out
    rank, best_cost, best, pl, sl, gl = min(results, key=lambda r_: r_[0])
    first_s = round(time.monotonic() - t0, 1)
    rep("refine", best_so_far=_summary(best))
    refinements, cancelled = 0, False
    try:
        # anytime refinement: lower heuristic weight, the best cost as a bound; each pass gets a slice of the
        # remaining time proportional to how hard the first route was (a simple yard does not wait minutes)
        slice_s = max(10.0, 10.0 * (time.monotonic() - t0))
        for w in (1.5, 1.2, 1.0):
            if time.monotonic() > deadline - 1.0:
                break
            f2, n2, _, st2 = pl.search(min(deadline, time.monotonic() + slice_s), w, bound=best_cost - 1e-6)
            if f2 is not None:
                cand = pl.route_from(f2, n2)
                c2 = n2[f2[0]][6] + (f2[1][0] if f2[1] else 0.0)
                if _rank(cand)[0] <= rank[0] and c2 < best_cost - 1e-6:
                    best, best_cost, rank = cand, c2, _rank(cand)
                    refinements += 1
                    rep("refine", best_so_far=_summary(best))
    except Cancelled:
        cancelled = True
    return {**base, "found": True, "success": True, "route": best, "cost": round(best_cost, 2),
            "chosen": {"start": sl, "end": gl}, "variants": variants, "first_route_s": first_s,
            "refinements": refinements, "cancelled": cancelled,
            "expansions": sum(p_[2].stats["expansions"] for p_ in reachable),
            "elapsed_s": round(time.monotonic() - t0, 1), "planner": pl}
