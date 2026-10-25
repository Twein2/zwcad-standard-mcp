"""Stage 2 of plan_vehicle_route: QuickTurn-consistent route built like QuickTurn2's dynamic path (QT:DRAWDPATH).

QuickTurn2 (R. Livingston, https://github.com/RFLTools/QuickTurn2) draws the path of the guided point as a chain
of arcs: every new arc starts in the direction the steered wheels point (so the path is tangent-continuous) and
ends at the next picked point; an arc tighter than RMIN = wheelbase / sin(lock angle) is replaced by an RMIN arc
in the same direction. The vehicle follows by the exact tractrix step (QT:GETALIST, Mathematical and Computer
Modelling 49/2009, p. 2052): a' = 2·atan(tan(a/2)·exp(-d/L)).

Here the "picked points" are placed automatically: they start on the corridor found by the fast rigid search
(route_rigid) and are moved by a pattern search until the whole body with mirrors keeps the margin from the
boundary. Forward: the FRONT axle is the guided point (QuickTurn / CAYENNE 'Przod'). Reverse: the REAR axle is
guided and the car axis stays tangent to its path (CAYENNE 'Tyl'). The last piece reaches the end vector exactly
(reverse: Dubins of the rear axle; forward: Dubins of the front axle plus a straight run-in).
"""
from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from zwcad_standard_mcp.route_planner import Car, FreeSpace, _tractrix, arc_end, dubins_words, wrap


def two_point_arc(p, tau: float, w, rmin: float, max_half: float = math.radians(50)) -> tuple[float, float]:
    """QT MAKEPLINE: arc from p (tangent tau) through w; returns (curvature, length), radius clamped to rmin."""
    cx, cy = w[0] - p[0], w[1] - p[1]
    chord = math.hypot(cx, cy)
    if chord < 1e-9:
        return 0.0, 0.0
    ang = wrap(math.atan2(cy, cx) - tau)          # half of the turning angle
    if abs(ang) > max_half:                       # a point far to the side or behind: limited turn, no loops
        ang = math.copysign(max_half, ang)
    if abs(math.sin(ang)) < 1e-9:
        return 0.0, chord if abs(ang) < 1.0 else 0.0
    r = chord / (2 * abs(math.sin(ang)))
    if r < rmin:                                  # QuickTurn keeps the direction and uses RMIN
        r = rmin
    return math.copysign(1.0 / r, ang), 2 * abs(ang) * r


def sample(x: float, y: float, tau: float, pieces, ds: float):
    """Points and tangents along pieces [(curvature, length)]."""
    pts, tans = [(x, y)], [tau]
    for kappa, ln in pieces:
        if ln < 1e-9:
            continue
        n = max(1, int(math.ceil(ln / ds)))
        for i in range(1, n + 1):
            px, py, pt = arc_end(x, y, tau, kappa, ln * i / n)
            pts.append((px, py))
            tans.append(pt)
        x, y, tau = arc_end(x, y, tau, kappa, ln)
    return np.array(pts), np.array(tans), (x, y, tau)


class Problem:
    def __init__(self, boundary, start, goal, car: Car, kmax: float, margin: float, res: float,
                 heading_tol: float = math.radians(1.0)):
        self.car, self.L = car, car.L
        self.r_rear = 1.0 / kmax
        self.r_front = math.hypot(self.r_rear, car.L)
        self.start, self.goal = start, goal
        self.margin, self.res, self.heading_tol = margin, res, heading_tol
        poly = np.asarray(boundary, dtype=float)[:, :2]
        self.fs = FreeSpace(poly, res, pad=10 * res)
        self.outline = car.outline(spacing=2 * res)

    # ------------------------------------------------------------------ simulation
    def clearance(self, poses):
        ox, oy = self.outline[:, 0], self.outline[:, 1]
        c, s = np.cos(poses[:, 2])[:, None], np.sin(poses[:, 2])[:, None]
        X = poses[:, 0][:, None] + c * ox[None, :] - s * oy[None, :]
        Y = poses[:, 1][:, None] + s * ox[None, :] + c * oy[None, :]
        return self.fs.sample(X, Y).min(axis=1)

    def run_poses(self, fwd: bool, gx, gy, tau, phi, pieces):
        pts, tans, endq = sample(gx, gy, tau, pieces, self.res)
        if fwd:
            phis = _tractrix(pts, phi, self.L)
            rear = pts - self.L * np.column_stack([np.cos(phis), np.sin(phis)])
            delta = float(np.abs((tans - phis + np.pi) % (2 * np.pi) - np.pi).max())
            return np.column_stack([rear, phis]), endq, delta
        delta = max((math.atan(self.L * abs(k)) for k, ln in pieces if ln > 1e-9), default=0.0)
        return np.column_stack([pts, tans - math.pi]), endq, delta

    def final_pieces(self, fwd: bool, gx, gy, tau, phi):
        """Exact approach to the goal (several Dubins words); returns candidates [(pieces, poses, delta)]."""
        out = []
        g = self.goal
        if fwd:
            r = self.r_front
            fx, fy = g[0] + self.L * math.cos(g[2]), g[1] + self.L * math.sin(g[2])
            variants = [((fx - d * math.cos(g[2]), fy - d * math.sin(g[2]), g[2]), d)
                        for d in (0.5 * self.L, self.L, 2 * self.L, 3 * self.L, 4 * self.L)]
        else:
            r = self.r_rear
            variants = [((g[0], g[1], g[2] + math.pi), 0.0)]
        for q1, run_in in variants:
            for _total, segs in dubins_words((gx, gy, tau), q1, r):
                pieces = [(sg / r, ln) for sg, ln in segs if ln > 1e-9] + ([(0.0, run_in)] if run_in else [])
                poses, _, delta = self.run_poses(fwd, gx, gy, tau, phi, pieces)
                end = poses[-1]
                # driving forward the car heading lags the front-axle path (tractrix): allow a few degrees
                tol = self.heading_tol if not fwd else max(self.heading_tol, math.radians(3.0))
                ok = (math.hypot(end[0] - g[0], end[1] - g[1]) <= 2.5 * self.res + self.L * math.sin(tol)
                      and abs(wrap(end[2] - g[2])) <= tol)
                out.append((pieces, poses, delta, ok))
        return out

    def build(self, runs):
        """runs: [(fwd, [waypoints])] → (cost, detail). Waypoints are guided-axle points."""
        x, y, phi = self.start
        allp, segs_out, dmax = [np.array([[x, y, phi]])], [], 0.0
        prev_fwd, tau = None, phi
        length = 0.0
        for ri, (fwd, wps) in enumerate(runs):
            last = ri == len(runs) - 1
            if fwd:
                gx, gy = x + self.L * math.cos(phi), y + self.L * math.sin(phi)
                t0 = tau if prev_fwd is True else phi
                r = self.r_front
            else:
                gx, gy = x, y
                t0 = tau if prev_fwd is False else phi + math.pi
                r = self.r_rear
            run_start = (gx, gy, t0)
            pieces = []
            cx, cy, ct = gx, gy, t0
            for w in wps:
                k, ln = two_point_arc((cx, cy), ct, w, r)
                if ln > 1e-9:
                    pieces.append((k, ln))
                    cx, cy, ct = arc_end(cx, cy, ct, k, ln)
            poses, endq, delta = self.run_poses(fwd, gx, gy, t0, phi, pieces)
            if last:
                best = None
                for fp, fposes, fdelta, ok in self.final_pieces(fwd, endq[0], endq[1], endq[2], poses[-1, 2]):
                    viol = self._violation(fposes)
                    score = viol + (0.0 if ok else 1e6)
                    if best is None or score < best[0]:
                        best = (score, fp, fposes, fdelta, ok)
                _, fp, fposes, fdelta, ok = best
                pieces = pieces + fp
                poses = np.vstack([poses, fposes[1:]])
                delta = max(delta, fdelta)
                end_ok = ok
            allp.append(poses[1:])
            dmax = max(dmax, delta)
            length += sum(ln for _, ln in pieces)
            segs_out.append((fwd, run_start, pieces))
            x, y, phi = poses[-1]
            tau = endq[2] if not last else None
            prev_fwd = fwd
        arr = np.vstack(allp)
        viol = self._violation(arr)
        cost = 1e3 * viol + (0.0 if end_ok else 1e7) + 0.01 * length
        return cost, {"segments": segs_out, "poses": arr, "max_delta": dmax, "end_ok": end_ok,
                      "violation": viol, "length": length}

    def _violation(self, poses) -> float:
        car = self.car
        if not hasattr(self, "_disks"):
            n_out = 10
            cx_out = np.linspace(-car.rear, car.front, n_out)
            half_gap = (car.front + car.rear) / (2 * (n_out - 1))
            self._disks = (cx_out, math.hypot(max(car.hw, car.mirror[2]), half_gap))
        cx_out, r_out = self._disks
        c, s = np.cos(poses[:, 2])[:, None], np.sin(poses[:, 2])[:, None]
        d_out = self.fs.sample(poses[:, 0][:, None] + c * cx_out[None, :],
                               poses[:, 1][:, None] + s * cx_out[None, :]).min(axis=1)
        near = d_out < r_out + self.margin
        if not near.any():
            return 0.0
        clr = self.clearance(poses[near])
        short = np.clip(self.margin - clr, 0.0, None)
        return float((short ** 2).sum())


def waypoints_from_reference(ref_poses, ref_dirs, L: float, spacing: float):
    """Split the stage-1 route into runs and pick guided-axle points every ``spacing``."""
    runs = []
    i = 1
    n = len(ref_poses)
    while i < n:
        d = ref_dirs[i]
        j = i
        while j + 1 < n and ref_dirs[j + 1] == d:
            j += 1
        seg = ref_poses[i - 1:j + 1]
        if d > 0:
            pts = seg[:, :2] + L * np.column_stack([np.cos(seg[:, 2]), np.sin(seg[:, 2])])
        else:
            pts = seg[:, :2]
        dist = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1])))])
        total = dist[-1]
        marks = np.arange(spacing, total, spacing)
        idx = [int(np.searchsorted(dist, m)) for m in marks] + [len(pts) - 1]
        wps = [(float(pts[k][0]), float(pts[k][1]), float(seg[k][2])) for k in sorted(set(idx))]
        runs.append((d > 0, wps))
        i = j + 1
    return runs


def optimise(problem: Problem, runs, time_budget_s: float):
    """Pattern search on the waypoint positions (QuickTurn 'picked points')."""
    t0 = time.monotonic()
    runs = [(f, [list(w) for w in wps]) for f, wps in runs]
    # the last run's final waypoints are replaced by the exact approach: drop those within 2 wheelbases
    f_last, w_last = runs[-1]
    g = problem.goal
    keep = [w for w in w_last if math.hypot(w[0] - g[0], w[1] - g[1]) > 3 * problem.L]
    runs[-1] = (f_last, keep)
    best_cost, best = problem.build(runs)
    evals = 1
    for step in (80.0, 40.0, 20.0, 10.0, 5.0):
        step *= problem.res / 5.0
        improved = True
        while improved and time.monotonic() - t0 < time_budget_s:
            improved = False
            for ri in range(len(runs)):
                for wi in range(len(runs[ri][1])):
                    for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
                        w = runs[ri][1][wi]
                        w[0] += dx
                        w[1] += dy
                        cost, det = problem.build(runs)
                        evals += 1
                        if cost < best_cost - 1e-9:
                            best_cost, best, improved = cost, det, True
                        else:
                            w[0] -= dx
                            w[1] -= dy
                    if time.monotonic() - t0 > time_budget_s:
                        break
            if best["violation"] == 0.0 and best["end_ok"] and step <= 10.0 * problem.res / 5.0:
                break
        if time.monotonic() - t0 > time_budget_s:
            break
    best["evaluations"] = evals
    best["cost"] = best_cost
    return best


def _finish(problem: Problem, fwd: bool, state, soft: bool, w_viol: float, best):
    """Try the exact approach to the goal from one beam state; keep the better of it and ``best``."""
    sc, x, y, phi, tau, _pf, rout, pl = state
    L = problem.L
    gx, gy = (x + L * math.cos(phi), y + L * math.sin(phi)) if fwd else (x, y)
    for fp, fposes, _fdelta, ok in problem.final_pieces(fwd, gx, gy, tau, phi):
        if not ok:
            continue
        viol = problem._violation(fposes)
        if viol > 0 and not soft:
            continue
        total = sc + sum(ln for _, ln in fp) + w_viol * viol
        if best is None or total < best[0]:
            nr = [*rout[:-1], (rout[-1][0], rout[-1][1], rout[-1][2] + list(fp))]
            best = (total, nr, pl + [fposes[1:]])
    return best


def beam_search(problem: Problem, runs, time_budget_s: float, width: int = 12, reach: float = 60.0,
                step: float = 15.0, soft: bool = True, w_viol: float = 50.0):
    """Build the route click by click (like QT:DRAWDPATH): for every next reference point try offsets around it,
    keep the ``width`` best collision-free partial routes, finish with the exact approach to the goal."""
    t0 = time.monotonic()
    g = problem.goal
    f_last, w_last = runs[-1]
    runs = runs[:-1] + [(f_last, [w for w in w_last if math.hypot(w[0] - g[0], w[1] - g[1]) > 1.5 * problem.L])]
    offs = [(dx, dy) for dx in np.arange(-reach, reach + 1e-9, step) for dy in np.arange(-reach, reach + 1e-9, step)]
    wide = 3 * reach
    offs_wide = [(dx, dy) for dx in np.arange(-wide, wide + 1e-9, 2 * step)
                 for dy in np.arange(-wide, wide + 1e-9, 2 * step)]
    x, y, phi = problem.start
    finished = [None]
    # beam state: (score, x, y, phi, tau, prev_fwd, runs_out[list of (fwd, start, pieces)], poses list)
    beam = [(0.0, x, y, phi, phi, None, [], [np.array([[x, y, phi]])])]
    L = problem.L
    for ri, (fwd, wps) in enumerate(runs):
        r = problem.r_front if fwd else problem.r_rear
        # start of the run for each beam state
        nb = []
        for sc, x, y, phi, tau, pf, rout, pl in beam:
            if fwd:
                gx, gy, t0_ = x + L * math.cos(phi), y + L * math.sin(phi), (tau if pf is True else phi)
            else:
                gx, gy, t0_ = x, y, (tau if pf is False else phi + math.pi)
            nb.append((sc, x, y, phi, (gx, gy, t0_), rout + [(fwd, (gx, gy, t0_), [])], pl))
        if ri == len(runs) - 1:
            for sc_, x_, y_, phi_, gq_, rout_, pl_ in nb[:3]:
                finished[0] = _finish(problem, fwd, (sc_, x_, y_, phi_, gq_[2], fwd, rout_, pl_), soft, w_viol,
                                      finished[0])
        cur = nb
        for wi, (wx, wy, wphi) in enumerate(wps):
            cand = []
            # gear-change points are free to move further (the car cannot copy the corridor's sharp turn)
            edge = wi == len(wps) - 1 or wi == 0
            my_offs = offs_wide if edge else offs
            for sc, x, y, phi, gq, rout, pl in cur:
                for dx, dy in my_offs:
                    k, ln = two_point_arc((gq[0], gq[1]), gq[2], (wx + dx, wy + dy), r)
                    if ln < 1e-6:
                        continue
                    poses, endq, delta = problem.run_poses(fwd, gq[0], gq[1], gq[2], phi, [(k, ln)])
                    viol = problem._violation(poses)
                    if viol > 0.0 and not soft:
                        continue
                    e = poses[-1]
                    dev = math.hypot(e[0] - (wx if not fwd else wx - L * math.cos(wphi)),
                                     e[1] - (wy if not fwd else wy - L * math.sin(wphi)))
                    score = (sc + ln + (0.5 if edge else 2.0) * dev + (0.2 if edge else 1.0) * L * abs(wrap(e[2] - wphi))
                             + w_viol * viol)
                    nr = [*rout[:-1], (rout[-1][0], rout[-1][1], rout[-1][2] + [(k, ln)])]
                    cand.append((score, float(e[0]), float(e[1]), float(e[2]), endq, nr, pl + [poses[1:]]))
            if not cand:
                break
            cand.sort(key=lambda c: c[0])
            if ri == len(runs) - 1:      # last run: also try to finish from here (turn earlier, longer run-in)
                for sc_, x_, y_, phi_, gq_, rout_, pl_ in cand[:3]:
                    finished[0] = _finish(problem, fwd, (sc_, x_, y_, phi_, gq_[2], fwd, rout_, pl_), soft, w_viol,
                                          finished[0])
            # keep diverse states: at most one per 10 cm cell and 3 degrees of heading
            seen, cur = set(), []
            for c in cand:
                key = (round(c[1] / 10), round(c[2] / 10), round(math.degrees(c[3]) / 3))
                if key in seen:
                    continue
                seen.add(key)
                cur.append(c)
                if len(cur) >= width:
                    break
            if time.monotonic() - t0 > time_budget_s:
                break
        beam = [(sc, x, y, phi, gq[2], fwd, rout, pl) for sc, x, y, phi, gq, rout, pl in cur]
        if not beam:
            return None
    # exact approach to the goal from every final beam state (last run continues in its gear)
    best = finished[0]
    for st in beam:
        best = _finish(problem, runs[-1][0], st, soft, w_viol, best)
    if best is None:
        return None
    _, rout, pl = best
    arr = np.vstack(pl)
    return {"segments": rout, "poses": arr, "end_ok": True, "violation": problem._violation(arr), "evaluations": 0,
            "max_delta": math.atan2(problem.L, problem.r_rear), "cost": best[0]}


def edge_violations(problem: Problem, poses, margin_user: float, boundary) -> list[dict]:
    """Boundary edges the car comes closer to than the margin (or crosses), with the affected part of each edge."""
    poly = np.asarray(boundary, dtype=float)[:, :2]
    a, b = poly, np.roll(poly, -1, axis=0)
    ox, oy = problem.outline[:, 0], problem.outline[:, 1]
    c, s = np.cos(poses[:, 2])[:, None], np.sin(poses[:, 2])[:, None]
    X = (poses[:, 0][:, None] + c * ox[None, :] - s * oy[None, :]).ravel()
    Y = (poses[:, 1][:, None] + s * ox[None, :] + c * oy[None, :]).ravel()
    sdf = problem.fs.sample(X, Y)
    bad = sdf < margin_user
    if not bad.any():
        return []
    X, Y, sdf = X[bad], Y[bad], sdf[bad]
    # nearest edge of every offending body point
    best_d = np.full(X.shape, np.inf)
    best_i = np.zeros(X.shape, dtype=int)
    best_t = np.zeros(X.shape)
    for i, ((ax, ay), (bx, by)) in enumerate(zip(a, b)):
        vx, vy = bx - ax, by - ay
        ll = vx * vx + vy * vy
        t = np.clip(((X - ax) * vx + (Y - ay) * vy) / ll, 0.0, 1.0) if ll > 0 else np.zeros_like(X)
        dd = np.hypot(X - (ax + t * vx), Y - (ay + t * vy))
        better = dd < best_d
        best_d[better], best_i[better], best_t[better] = dd[better], i, t[better]
    out = []
    for i in np.unique(best_i):
        m = best_i == i
        t0_, t1_ = float(best_t[m].min()), float(best_t[m].max())
        ax, ay = a[i]
        bx, by = b[i]
        out.append({"edge_index": int(i),
                    "edge": [[round(float(ax), 3), round(float(ay), 3)], [round(float(bx), 3), round(float(by), 3)]],
                    "from": [round(float(ax + t0_ * (bx - ax)), 3), round(float(ay + t0_ * (by - ay)), 3)],
                    "to": [round(float(ax + t1_ * (bx - ax)), 3), round(float(ay + t1_ * (by - ay)), 3)],
                    "min_clearance": round(float(sdf[m].min()), 2),
                    "intrusion": round(float(margin_user - sdf[m].min()), 2),
                    "crosses_boundary": bool(sdf[m].min() < 0.0)})
    out.sort(key=lambda v: -v["intrusion"])
    return out


def pieces_to_vertices(start_xy, tau, pieces):
    verts, (x, y), t = [], start_xy, tau
    merged: list[list[float]] = []
    for k, ln in pieces:
        if ln < 1e-9:
            continue
        if merged and abs(merged[-1][0] - k) < 1e-12:
            merged[-1][1] += ln
        else:
            merged.append([k, ln])
    for k, ln in merged:
        verts.append([round(x, 4), round(y, 4), round(math.tan(k * ln / 4.0), 8) if k else 0.0])
        x, y, t = arc_end(x, y, t, k, ln)
    verts.append([round(x, 4), round(y, 4), 0.0])
    return verts, sum(ln for _, ln in merged)


def solve(boundary, start, goal, car: Car, kmax: float, margin: float, res: float, reference,
          time_budget_s: float, margin_user: float) -> dict[str, Any]:
    problem = Problem(boundary, start, goal, car, kmax, margin, res)
    ref_poses, ref_dirs = reference
    runs = waypoints_from_reference(ref_poses, ref_dirs, car.L, spacing=max(4 * res, 1.5 * car.L))
    t_start = time.monotonic()
    best = beam_search(problem, runs, 0.5 * time_budget_s, soft=False)
    if best is None:   # nothing keeps the margin: least-violating drivable route, violations reported
        best = beam_search(problem, runs, max(5.0, time_budget_s - (time.monotonic() - t_start)), soft=True)
    if best is None:
        best = optimise(problem, [(f, [w[:2] for w in wps]) for f, wps in runs], time_budget_s)
    segments = []
    for fwd, (sx, sy, stau), pieces in best["segments"]:
        verts, ln = pieces_to_vertices((sx, sy), stau, pieces)
        segments.append({"gear": "forward" if fwd else "reverse", "direction": "forward" if fwd else "reverse",
                         "path": "front axle centre" if fwd else "rear axle centre",
                         "min_radius": round(problem.r_front if fwd else problem.r_rear, 2),
                         "length": round(ln, 2), "vertices": verts})
    arr = best["poses"]
    clr = float(problem.clearance(arr).min())
    jumps = []
    for sgm in segments:
        v = sgm["vertices"]
        for i in range(len(v) - 2):
            t_end = math.atan2(v[i + 1][1] - v[i][1], v[i + 1][0] - v[i][0]) + 2 * math.atan(v[i][2])
            t_start = math.atan2(v[i + 2][1] - v[i + 1][1], v[i + 2][0] - v[i + 1][0]) - 2 * math.atan(v[i + 1][2])
            jumps.append(abs(math.degrees(wrap(t_start - t_end))))
    endp = arr[-1]
    violations = edge_violations(problem, arr, margin_user, boundary)
    ok = best["end_ok"] and clr > -2.0 * car.L     # a "route" far outside the boundary is no answer
    return {"found": bool(ok), "success": bool(ok), "gear_changes": len(segments) - 1,
            "total_length": round(sum(s["length"] for s in segments), 2), "min_clearance": round(clr, 2),
            "clearance_ok": clr >= margin_user, "max_steer_deg": round(math.degrees(best["max_delta"]), 2),
            "path_tangent_jump_max_deg": round(max(jumps), 4) if jumps else 0.0,
            "end_error": {"distance": round(math.hypot(endp[0] - goal[0], endp[1] - goal[1]), 3),
                          "heading_deg": round(math.degrees(wrap(endp[2] - goal[2])), 3)},
            "violations": violations, "evaluations": best["evaluations"], "segments": segments}
