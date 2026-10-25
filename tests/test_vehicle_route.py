"""plan_vehicle_route / draw_vehicle_route (route planner in the server)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from zwcad_standard_mcp import archtools, route_planner
from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import VehicleRouteRequest
from zwcad_standard_mcp.services import CadService
from zwcad_standard_mcp.tools.register import register_all_tools

# L-shaped yard in cm: 6 m wide lane going north, then a 6 m wide lane going west
YARD = [[0, 0], [600, 0], [600, 2400], [-1800, 2400], [-1800, 1800], [0, 1800]]
START = [[300, 300], [300, 800]]          # car facing north in the lane
END = [[-1000, 2100], [-1500, 2100]]      # car facing west in the upper lane


def test_kinked_forward_route_is_refused() -> None:
    # FINDINGS #57: the rigid-model planner produces front-axle kinks (instant steering); they must not be returned
    r = route_planner.plan_route(YARD, START, END, archtools.vehicle(), "cm", allow_reverse=False, time_budget_s=60)
    assert not r["found"]
    assert r.get("forward_path_kink_max_deg", 0) > 1.0 and "steering" in r["reason"]


def test_forward_only_dead_end_is_rejected_fast() -> None:
    # end vector points out of a 3.8 m wide dead-end bay: needs reversing (example from examples/vehicle_route)
    d = json.loads((Path(__file__).resolve().parents[1] / "examples/vehicle_route/input_example.json").read_text())
    r = route_planner.plan_route(d["limits"], d["start"], d["end"], archtools.vehicle(), "cm",
                                 allow_reverse=False, time_budget_s=30)
    assert not r["found"] and r["exhausted"] and r["elapsed_s"] < 10
    assert "allow_reverse=true" in r["reason"]


def test_bulged_boundary_is_split() -> None:
    pts = route_planner.polyline_points([[0, 0, 1.0], [100, 0, 0.0]], closed=False, max_seg=10)
    assert len(pts) > 10
    assert max(abs(y) for _, y in pts) > 45            # half circle of radius 50 (bulge 1 = 180 deg)


def _lisp(handler_values):
    def handler(expr: str, undo: bool):
        m = re.search(r'\(mo:curve-data "([0-9A-F]+)"', expr)
        if m:
            return {"ok": True, "value": handler_values[m.group(1)]}
        return {"ok": True, "value": None}
    return handler


def test_service_plan_and_draw_dry_run() -> None:
    adapter = FakeCadAdapter()
    curves = {
        "1B": {"type": "LWPOLYLINE", "closed": True, "insunits": 5, "vertices": [p + [0.0] for p in YARD]},
        "1C": {"type": "LWPOLYLINE", "closed": False, "insunits": 5, "vertices": [p + [0.0] for p in START]},
        "1E": {"type": "LINE", "closed": False, "insunits": 5, "vertices": [p + [0.0] for p in END]},
    }
    adapter.lisp_handler = _lisp(curves)
    service = CadService(adapter, Settings(adapter="fake"))
    res = service.plan_vehicle_route(VehicleRouteRequest(boundary_handle="1B", start_handle="1C", end_handle="1E",
                                                         wait_s=55, time_budget_s=60))
    if res["status"] == "running":
        res = service.get_vehicle_route_plan(res["plan_id"], 55)
    assert res["status"] == "done" and res["result"]["found"] is False   # refused until the QuickTurn rework
    assert "steering" in res["result"]["reason"]


def test_route_tools_registered() -> None:
    names: list[str] = []

    class StubMCP:
        def tool(self, *a, **k):
            def deco(fn):
                names.append(fn.__name__)
                return fn
            return deco

    register_all_tools(StubMCP(), CadService(FakeCadAdapter(), Settings(adapter="fake")))
    for name in ("plan_vehicle_route", "get_vehicle_route_plan", "draw_vehicle_route", "vehicle_swept_path"):
        assert name in names
