"""plan_vehicle_route / draw_vehicle_route / verify_vehicle_path (route planner in the server)."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import numpy as np

from zwcad_standard_mcp import archtools, route_check, route_planner
from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import DrawVehicleRouteRequest, VehicleRouteRequest, VerifyVehiclePathRequest
from zwcad_standard_mcp.services import CadService
from zwcad_standard_mcp.tools.register import register_all_tools

DATA = Path(__file__).resolve().parent / "data"
# L-shaped yard in cm: 6 m wide lane going north, then a 6 m wide lane going west
YARD = [[0, 0], [600, 0], [600, 2400], [-1800, 2400], [-1800, 1800], [0, 1800]]
START = [[300, 300], [300, 800]]          # car facing north in the lane
END = [[-1000, 2100], [-1500, 2100]]      # car facing west in the upper lane
CAR = route_planner.Car.from_vehicle(archtools.vehicle(), "cm")
LOCK = math.degrees(CAR.lock)


def _soj():
    trace = np.loadtxt(DATA / "soj_trace_rear_axle.csv", delimiter=",")
    boundary = np.loadtxt(DATA / "soj_boundary.csv", delimiter=",")
    return trace, boundary


def _vec(x, y, deg):
    a = math.radians(deg)
    return [[x, y], [x + 100 * math.cos(a), y + 100 * math.sin(a)]]


def _steering_profile(seg, L):
    """Wheel angle per arc of a segment (rear-axle path, bulges) and the distance where each arc starts."""
    v = seg["vertices"]
    out, dist = [], 0.0
    for a, b in zip(v[:-1], v[1:]):
        chord = math.dist(a[:2], b[:2])
        ang = 4 * math.atan(a[2])
        ln = chord if abs(ang) < 1e-12 else abs(ang) * chord / (2 * math.sin(abs(ang) / 2))
        kappa = 0.0 if abs(ang) < 1e-12 else ang / ln
        sgn = 1 if seg["gear"] == "forward" else -1
        out.append((dist, math.degrees(math.atan(sgn * L * kappa)), ln))
        dist += ln
    return out


def _assert_steering_continuous(r, rate_deg_per_m=30.0):
    """Between two arcs the wheel angle may only change by the steering rate, except at a declared stop."""
    for seg in r["segments"]:
        prof = _steering_profile(seg, CAR.L)
        stops = [s["distance"] for s in seg["stops"]]
        for (d0, a0, l0), (d1, a1, _l1) in zip(prof[:-1], prof[1:]):
            jump = abs(a1 - a0)
            allowed = rate_deg_per_m * max(l0, 5.0) / 100.0 + 1e-6
            if jump > allowed:
                assert any(abs(s - d1) < 1.0 for s in stops), (seg["gear"], d1, a0, a1, stops)
        assert max(abs(a) for _, a, _ in prof) <= LOCK + 0.01


def test_lock_and_radii_from_vehicle_data() -> None:
    assert abs(LOCK - 32.90) < 0.01                       # atan(L / RR), RR = sqrt((TC/2)^2 - L^2) - TRF/2
    assert abs(CAR.rmin - 447.44) < 0.01


def test_forward_route_in_l_yard() -> None:
    r = route_planner.plan_route(YARD, START, END, archtools.vehicle(), "cm", allow_reverse=False, time_budget_s=20)
    assert r["found"], r
    assert r["gear_changes"] == 0 and r["segments"][0]["gear"] == "forward"
    assert r["segments"][0]["path"] == "rear axle centre"
    assert r["clearance_ok"] and not r["violations"] and r["min_clearance"] >= 5.0
    assert r["end_error"]["distance"] < 0.5 and abs(r["end_error"]["heading_deg"]) <= 1.0
    assert r["lock_used_deg"] == r["lock_deg"] == 32.9 and r["rear_axle_min_radius"] == 447.44
    _assert_steering_continuous(r)


def test_steer_fraction_reserve_is_explicit() -> None:
    r = route_planner.plan_route(YARD, START, END, archtools.vehicle(), "cm", allow_reverse=False, time_budget_s=20,
                                 steer_fraction=0.9)
    assert r["found"] and abs(r["lock_used_deg"] - 0.9 * 32.9) < 0.02
    assert r["max_steer_deg"] <= r["lock_used_deg"] + 0.01


def test_legacy_quickturn_planner_without_steer_in_place() -> None:
    r = route_planner.plan_route(YARD, START, END, archtools.vehicle(), "cm", allow_reverse=False, time_budget_s=60,
                                 allow_steer_in_place=False)
    assert r["found"], r
    assert r["path_tangent_jump_max_deg"] < 0.05            # QuickTurn: front-axle path tangent-continuous
    assert r["rear_axle_min_radius"] == 447.44              # full lock, no 0.95 factor any more


def test_margin_too_large_reports_violated_edges() -> None:
    # a 1.5 m margin cannot be kept in the 6 m lanes: the route is returned with the violated edges
    r = route_planner.plan_route(YARD, START, END, archtools.vehicle(), "cm", allow_reverse=False,
                                 time_budget_s=30, margin_mm=1500)
    assert r["found"] and r["violations"] and not r["clearance_ok"] and "warning" in r
    v = r["violations"][0]
    assert {"edge_index", "from", "to", "intrusion", "crosses_boundary"} <= set(v)


def test_forward_only_dead_end_is_rejected_fast() -> None:
    # end vector points out of a 3.8 m wide dead-end bay: needs reversing (example from examples/vehicle_route)
    d = json.loads((Path(__file__).resolve().parents[1] / "examples/vehicle_route/input_example.json").read_text())
    t0 = time.monotonic()
    r = route_planner.plan_route(d["limits"], d["start"], d["end"], archtools.vehicle(), "cm",
                                 allow_reverse=False, time_budget_s=30)
    assert not r["found"] and r["exhausted"] and time.monotonic() - t0 < 2.0
    assert "allow_reverse=true" in r["reason"]


def test_bulged_boundary_is_split() -> None:
    pts = route_planner.polyline_points([[0, 0, 1.0], [100, 0, 0.0]], closed=False, max_seg=10)
    assert len(pts) > 10
    assert max(abs(y) for _, y in pts) > 45            # half circle of radius 50 (bulge 1 = 180 deg)


# --------------------------------------------------------------------------- SOJ garage (owner's JAZDA trace)

def test_verify_owner_trace() -> None:
    trace, boundary = _soj()
    a = route_check.analyse_trace(trace, CAR, boundary)
    assert a["gear_changes"] == 1
    assert [r_["gear"] for r_ in a["runs"]] == ["forward", "reverse"]
    assert abs(a["runs"][0]["length"] - 1800) < 0.5 and abs(a["runs"][1]["length"] - 1440) < 0.5
    assert abs(a["max_steer_deg"] - 32.90) < 0.1 and not a["lock_exceeded"]
    assert a["start_collision"]["poses"] == 22 and 85 < a["start_collision"]["max_penetration"] < 90
    assert a["collision_free"] and 0.0 <= a["min_clearance"] < 1.5
    assert a["stops"]                                     # e.g. full left -> full right while standing


def test_exact_clearance_sees_a_wall_end_inside_the_car() -> None:
    # 12 cm wall stub poking 30 cm into the car side between two outline samples
    trace, boundary = _soj()
    stub_tip = boundary[16]                       # (19873.9, -30447.1): end of the thin partition
    pose = (stub_tip[0] - 150.0, stub_tip[1] - 30.0 + CAR.hw, math.radians(0.0))
    assert route_check.exact_clearance(np.array([pose]), CAR, boundary)[0] < -20.0


def test_soj_route_margin_zero_exact_poses() -> None:
    trace, boundary = _soj()
    r = route_planner.plan_route(boundary.tolist(), _vec(20056.44, -31322.24, 74.95), _vec(18859.63, -30722.73, 1.56),
                                 archtools.vehicle(), "cm", anchor="rear_axle", margin_mm=0, time_budget_s=40)
    assert r["found"], r.get("reason")
    assert r["min_clearance"] >= 0.0 and not r["violations"]            # no crossing
    assert r["max_steer_deg"] <= 32.91 and r["gear_changes"] <= 2
    assert r["end_error"]["distance"] < 0.5 and abs(r["end_error"]["heading_deg"]) < 1.0
    _assert_steering_continuous(r)


def test_soj_start_on_the_bay_edge_warns() -> None:
    trace, boundary = _soj()
    d = trace[1] - trace[0]
    start = _vec(trace[0][0], trace[0][1], math.degrees(math.atan2(d[1], d[0])))
    end = _vec(18859.63, -30722.73, 1.56)
    r = route_planner.plan_route(boundary.tolist(), start, end, archtools.vehicle(), "cm", anchor="rear_axle",
                                 margin_mm=0, time_budget_s=30)
    assert r["found"] and r["start_collision"]["max_penetration"] > 80 and r["start_collision"]["length"] < 200
    assert not r["violations"]
    s = route_planner.plan_route(boundary.tolist(), start, end, archtools.vehicle(), "cm", anchor="rear_axle",
                                 margin_mm=0, time_budget_s=30, start_collision="strict")
    assert not s["found"] and "start pose" in s["reason"]


def test_soj_end_region() -> None:
    trace, boundary = _soj()
    bay = [[18760, -30540], [19260, -30540], [19260, -30810], [18760, -30810]]
    r = route_planner.plan_route(boundary.tolist(), _vec(20056.44, -31322.24, 74.95), None, archtools.vehicle(),
                                 "cm", anchor="rear_axle", margin_mm=50, end_region=bay, time_budget_s=60)
    assert r["found"], r.get("reason")
    assert r["clearance_ok"] and r["chosen"]["end"].startswith("bay")
    assert len(r["variants"]) == 2
    ex, ey, eth = r["end_rear_axle"]
    cx, cy = ex + 0.5 * (CAR.front - CAR.rear) * math.cos(eth), ey + 0.5 * (CAR.front - CAR.rear) * math.sin(eth)
    assert 18760 <= cx <= 19260 and -30810 <= cy <= -30540
    assert min(abs(math.degrees(route_planner.wrap(eth))), abs(math.degrees(route_planner.wrap(eth - math.pi)))) <= 10.01


# --------------------------------------------------------------------------- service

def _lisp(handler_values, log=None):
    def handler(expr: str, undo: bool):
        if log is not None:
            log.append(expr)
        m = re.search(r'\(mo:curve-data "([0-9A-F]+)"', expr)
        if m:
            return {"ok": True, "value": handler_values[m.group(1)]}
        if expr.startswith("(mo:route-draw"):
            n = expr.count('"gear"')
            return {"ok": True, "value": [{"gear": "forward", "handle": f"A{i}"} for i in range(n)]}
        if expr.startswith("(mo:swept-path"):
            return {"ok": True, "value": {"success": True, "feasible": True, "steering_continuous": True,
                                          "envelope_handle": "E1"}}
        if expr.startswith("(mo:envelope-check"):
            return {"ok": True, "value": [{"envelope_handle": "E1", "intersections": 0, "min_distance": 3.0}]}
        return {"ok": True, "value": None}
    return handler


def _service(curves, log=None):
    adapter = FakeCadAdapter()
    adapter.lisp_handler = _lisp(curves, log)
    return CadService(adapter, Settings(adapter="fake"))


def test_service_plan_and_draw() -> None:
    curves = {
        "1B": {"type": "LWPOLYLINE", "closed": True, "insunits": 5, "vertices": [p + [0.0] for p in YARD]},
        "1C": {"type": "LWPOLYLINE", "closed": False, "insunits": 5, "vertices": [p + [0.0] for p in START]},
        "1E": {"type": "LINE", "closed": False, "insunits": 5, "vertices": [p + [0.0] for p in END]},
    }
    log: list[str] = []
    service = _service(curves, log)
    res = service.plan_vehicle_route(VehicleRouteRequest(boundary_handle="1B", start_handle="1C", end_handle="1E",
                                                         wait_s=55, time_budget_s=20))
    if res["status"] == "running":
        res = service.get_vehicle_route_plan(res["plan_id"], 55)
    assert res["status"] == "done" and res["result"]["found"], res
    assert "progress" in res and res["progress"]["stage"] == "done"
    draw = service.draw_vehicle_route(DrawVehicleRouteRequest(plan_id=res["plan_id"]), dry_run=True)
    assert draw["dry_run"] and draw["preview"]["segments"]
    out = service.draw_vehicle_route(DrawVehicleRouteRequest(plan_id=res["plan_id"]), dry_run=False, confirm=True)
    assert out["verified"] is True
    swept = [e for e in log if e.startswith("(mo:swept-path")]
    assert swept and all('"guide"' in e and '"rear"' in e for e in swept)


def test_service_cancel_keeps_running_state_consistent() -> None:
    trace, boundary = _soj()
    curves = {
        "1B": {"type": "LWPOLYLINE", "closed": True, "insunits": 5, "vertices": [list(p) + [0.0] for p in boundary]},
        "1C": {"type": "LINE", "closed": False, "insunits": 5, "vertices": [p + [0.0] for p in _vec(20056.44, -31322.24, 74.95)]},
        "1E": {"type": "LINE", "closed": False, "insunits": 5, "vertices": [p + [0.0] for p in _vec(18859.63, -30722.73, 1.56)]},
    }
    service = _service(curves)
    res = service.plan_vehicle_route(VehicleRouteRequest(boundary_handle="1B", start_handle="1C", end_handle="1E",
                                                         anchor="rear_axle", margin_mm=0, wait_s=0, time_budget_s=300))
    assert res["status"] == "running" and "progress" in res
    time.sleep(1.0)
    c = service.cancel_vehicle_route_plan(res["plan_id"])
    assert c["status"] in ("done", "cancelled")


def test_region_request_validation() -> None:
    import pydantic
    try:
        VehicleRouteRequest(boundary_handle="1", start_handle="2")
    except pydantic.ValidationError as exc:
        assert "end_handle" in str(exc)
    else:
        raise AssertionError("missing end accepted")
    VehicleRouteRequest(boundary_handle="1", start_handle="2", end_region_handle="3")


def test_service_verify_vehicle_path_dry_run() -> None:
    trace, boundary = _soj()
    curves = {
        "2A": {"type": "LWPOLYLINE", "closed": False, "insunits": 5, "vertices": [list(p) + [0.0] for p in trace]},
        "1B": {"type": "LWPOLYLINE", "closed": True, "insunits": 5, "vertices": [list(p) + [0.0] for p in boundary]},
    }
    service = _service(curves)
    out = service.verify_vehicle_path(VerifyVehiclePathRequest(path_handle="2A", boundary_handle="1B"), dry_run=True)
    a = out["analysis"]
    assert a["gear_changes"] == 1 and a["collision_free"] and a["start_collision"]["poses"] == 22


def test_route_tools_registered() -> None:
    names: list[str] = []

    class StubMCP:
        def tool(self, *a, **k):
            def deco(fn):
                names.append(fn.__name__)
                return fn
            return deco

    register_all_tools(StubMCP(), CadService(FakeCadAdapter(), Settings(adapter="fake")))
    for name in ("plan_vehicle_route", "get_vehicle_route_plan", "cancel_vehicle_route_plan", "draw_vehicle_route",
                 "vehicle_swept_path", "verify_vehicle_path"):
        assert name in names
