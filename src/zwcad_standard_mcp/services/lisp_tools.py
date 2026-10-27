"""Tools backed by the AutoLISP channel, plus screenshot and offset (mixed into CadService)."""

from __future__ import annotations

import itertools
import math
import os
import threading
import time
from typing import Any

from zwcad_standard_mcp import archtools, route_planner
from zwcad_standard_mcp.adapters.lisp_channel import lisp_call, strip_lisp_comments
from zwcad_standard_mcp.errors import LispDisabledError, ValidationError
from zwcad_standard_mcp.models import (
    AreaFieldRequest,
    DrawVehicleRouteRequest,
    OffsetRequest,
    RoundPathRequest,
    SunRulerRequest,
    SweptPathRequest,
    VehicleRouteRequest,
    VerifyVehiclePathRequest,
)
from zwcad_standard_mcp.permissions.policy import PermissionLevel
from zwcad_standard_mcp.permissions_bridge import require_permission


_UNITS_PER_M = {4: 1000.0, 5: 100.0, 6: 1.0}
_CAYENNE_UNITS = {4: 1, 5: 2, 6: 3}


def lisp_enabled() -> bool:
    """execute_lisp (arbitrary code) is opt-in: ZWCAD_MCP_ENABLE_LISP=1."""
    return os.getenv("ZWCAD_MCP_ENABLE_LISP", "0").strip().lower() in {"1", "true", "yes", "on"}


_ROUTE_PLANS: dict[str, dict[str, Any]] = {}
_ROUTE_LOCK = threading.Lock()
_ROUTE_SEQ = itertools.count(1)
_UNIT_NAME = {4: "mm", 5: "cm", 6: "m"}
_UNIT_K = {"mm": 1.0, "cm": 0.1, "m": 0.001}


def _plan_view(plan: dict, include_segments: bool = True) -> dict:
    view = {k: v for k, v in plan.items() if k not in ("thread", "geometry", "result", "cancel")}
    result = plan.get("result")
    if result is not None:
        view["result"] = result if include_segments else {k: v for k, v in result.items() if k != "segments"}
    if plan["status"] == "running":
        view["next_step"] = "call get_vehicle_route_plan(plan_id) until status is 'done'"
    elif result and result.get("found"):
        view["next_step"] = "call draw_vehicle_route(plan_id) to draw and verify the route"
    if plan["status"] == "running":
        view["next_step"] += "; cancel_vehicle_route_plan(plan_id) stops it (the best route so far is kept)"
    return view


def _drop_none(values: dict) -> dict:
    return {k: v for k, v in values.items() if v is not None}



class LispToolsMixin:
    adapter: Any
    _UNIT_NAMES = {"mm": 4, "cm": 5, "m": 6}

    def _insunits(self, units: str) -> int:
        return self._UNIT_NAMES[units] if units != "auto" else self.adapter.drawing_units()

    def lisp_status(self) -> dict:
        envelope = self.adapter.run_lisp("(zwmcp:ping)", name="lisp_status", timeout_s=30.0)
        return {
            "success": bool(envelope.get("ok")),
            "lisp": envelope.get("value"),
            "error": envelope.get("error"),
            "channel": self.adapter.lisp_info(),
            "execute_lisp_enabled": lisp_enabled(),
        }

    def execute_lisp(self, code: str, undo: bool = True, dry_run: bool = True, confirm: bool = False,
                     timeout_s: float = 300.0) -> dict:
        if not lisp_enabled():
            raise LispDisabledError(
                "execute_lisp is switched off. Enable it with ZWCAD_MCP_ENABLE_LISP=1 in the MCP server env "
                "(install.bat ... lisp, or configure --enable-lisp) and restart the client."
            )
        forms = strip_lisp_comments(code)
        if not forms:
            raise ValidationError("No LISP expression in code.")
        expression = forms[0] if len(forms) == 1 else "(progn " + "\n".join(forms) + ")"
        try:
            expression.encode(self.adapter.lisp_codepage())
        except UnicodeEncodeError as exc:
            raise ValidationError(f"Code has characters outside {self.adapter.lisp_codepage()}; use (chr N): {exc}") from exc
        if dry_run:
            return {"success": True, "dry_run": True, "forms": len(forms), "undo_group": undo,
                    "expression_preview": expression[:600]}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        envelope = self.adapter.run_lisp(expression, undo=undo, timeout_s=timeout_s, name="execute_lisp")
        if envelope.get("ok"):
            return {"success": True, "dry_run": False, "value": envelope.get("value")}
        return {"success": False, "dry_run": False, "code": "LISP_ERROR", "error": envelope.get("error")}

    def get_screenshot(self, region: str = "drawing", max_width: int = 1280) -> dict:
        if region not in {"drawing", "window"}:
            raise ValidationError("region must be 'drawing' or 'window'.")
        return self.adapter.screenshot(region=region, max_width=max(320, min(int(max_width), 2560)))

    def zoom_view(self, mode: str = "extents", lower_left: list[float] | None = None,
                  upper_right: list[float] | None = None, handles: list[str] | None = None,
                  margin: float = 0.05) -> dict:
        if mode not in {"extents", "window", "entities"}:
            raise ValidationError("mode must be 'extents', 'window' or 'entities'.")
        return self.adapter.zoom_view(mode, lower_left, upper_right, handles, max(0.0, margin))

    def offset_entities(self, request: OffsetRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(request.handles, "handles")
        if request.layer:
            existing = {item["name"].lower() for item in self.adapter.list_layers(detail=False)}
            if request.layer.lower() not in existing:
                raise ValidationError(f"Layer does not exist: {request.layer} (create it with ensure_layers).")
        if dry_run:
            return {"success": True, "dry_run": True, "handles": request.handles, "distance": request.distance,
                    "both_sides": request.both_sides, "layer": request.layer}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.offset_entities(request.handles, request.distance, request.both_sides, request.layer)
        return {"dry_run": False, **self._summary(results), "success": all(r.get("success") for r in results)}

    def insert_area_fields(self, request: AreaFieldRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(request.pairs, "pairs")
        insunits = self._insunits(request.units)
        fmt = archtools.area_field_format(insunits, request.decimals, request.decimal_separator)
        factor = archtools.UNIT_AREA_FACTOR.get(insunits, 1.0)
        areas = self.adapter.curve_areas([p.source_handle for p in request.pairs])
        preview = []
        for pair in request.pairs:
            area = areas.get(pair.source_handle, {})
            item = {"text_handle": pair.text_handle, "source_handle": pair.source_handle,
                    "source_ok": bool(area.get("success")), "source_type": area.get("type")}
            if area.get("success"):
                item["area_m2"] = round(float(area["area"]) * factor, request.decimals)
            else:
                item["error"] = area.get("error")
            preview.append(item)
        if dry_run:
            return {"success": all(i["source_ok"] for i in preview), "dry_run": True, "format": fmt,
                    "insunits": insunits, "pairs": preview}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        opts = _drop_none({"format": fmt, "with_layer": request.with_layer, "keep_text": request.keep_text,
                           "suffix": request.suffix})
        expr = lisp_call("mo:area-fields", [[p.text_handle, p.source_handle] for p in request.pairs], opts,
                         codepage=self.adapter.lisp_codepage())
        results = self.adapter.lisp_call_expr(expr, undo=True, name="insert_area_fields") or []
        return {"success": all(r.get("success") for r in results), "dry_run": False, "format": fmt, "results": results}

    def draw_sun_ruler(self, request: SunRulerRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        insunits = self._insunits(request.units)
        scale = _UNITS_PER_M.get(insunits, 1.0)
        if request.obstruction_height_m <= request.terrain_height_m:
            raise ValidationError("obstruction_height_m must be greater than terrain_height_m.")
        eq = "W" if request.equinox == "spring" else "J"
        h = request.obstruction_height_m - request.terrain_height_m
        base = tuple(request.base_point[:2])
        window = archtools.exposure_window(
            base, request.latitude, request.longitude, eq,
            tuple(request.start_direction_point[:2]) if request.start_direction_point else None,
            tuple(request.end_direction_point[:2]) if request.end_direction_point else None,
            request.start_time, request.end_time,
        )
        longest = max(archtools.shadow(t, request.latitude, request.longitude, eq, h)[1] for t in (7.0, 17.0))
        summary = {"equinox": archtools.EQUINOX[eq]["label"], "h_m": round(h, 3), **window,
                   "drawing_units_per_m": scale, "longest_shadow_m": round(longest, 2),
                   "longest_shadow_drawing_units": round(longest * scale, 2)}
        if dry_run:
            return {"success": True, "dry_run": True, "preview": summary}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        params = _drop_none({
            "base": list(request.base_point[:2]),
            "equinox": eq,
            "h_obj": request.obstruction_height_m,
            "h_ter": request.terrain_height_m,
            "phi": request.latitude,
            "lambda": request.longitude,
            "scale": scale,
            "txt_h_hours": request.hour_text_height,
            "txt_h_desc": request.description_text_height,
            "start_dir": list(request.start_direction_point[:2]) if request.start_direction_point else None,
            "end_dir": list(request.end_direction_point[:2]) if request.end_direction_point else None,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "desc_point": list(request.description_point[:2]) if request.description_point else None,
            "room": request.room_number,
            "room_point": list(request.room_label_point[:2]) if request.room_label_point else None,
        })
        params["make_block"] = request.make_block
        value = self.adapter.lisp_call_expr(lisp_call("mo:sun-ruler", params, codepage=self.adapter.lisp_codepage()),
                                            undo=True, name="draw_sun_ruler")
        return {"success": bool(value and value.get("success")), "dry_run": False, "preview": summary, "result": value}

    def _vehicle_opts(self, direction: str, units: str, vehicle_overrides: dict) -> tuple[dict, dict, int]:
        overrides = {k: v for k, v in vehicle_overrides.items() if v is not None}
        try:
            veh = archtools.vehicle(overrides)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        insunits = self._insunits(units)
        code = _CAYENNE_UNITS.get(insunits)
        if code is None:
            raise ValidationError(f"Drawing units are not set (INSUNITS={insunits}); pass units='mm', 'cm' or 'm'.")
        opts = {"mode": "Tyl" if direction == "reverse" else "Przod", "units": code}
        if overrides:
            opts["vehicle"] = overrides
        return opts, veh, code

    # ------------------------------------------------------------------ vehicle route planning

    def _curve(self, handle: str) -> dict:
        value = self.adapter.lisp_call_expr(lisp_call("mo:curve-data", handle, codepage=self.adapter.lisp_codepage()),
                                            name="curve_data")
        if not value or not value.get("vertices"):
            raise ValidationError(f"Could not read the geometry of {handle}.")
        return value

    def _closed_region(self, handle: str, what: str, units: str) -> list:
        data = self._curve(handle)
        if data.get("type") != "LWPOLYLINE" or data.get("closed") is not True:
            raise ValidationError(f"The {what} {handle} must be a closed LWPOLYLINE.")
        return route_planner.polyline_points(data["vertices"], True, max_seg=200.0 * _UNIT_K[units])

    def plan_vehicle_route(self, request: VehicleRouteRequest) -> dict:
        try:
            route_planner.require_numpy()
        except route_planner.PlanningError as exc:
            raise ValidationError(str(exc)) from exc
        overrides = {k: v for k, v in request.vehicle.model_dump().items() if v is not None}
        try:
            veh = archtools.vehicle(overrides)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        boundary = self._curve(request.boundary_handle)
        insunits = self._insunits(request.units) if request.units != "auto" else int(boundary.get("insunits") or 0)
        units = _UNIT_NAME.get(insunits)
        if units is None:
            raise ValidationError(f"Drawing units are not set (INSUNITS={insunits}); pass units='mm', 'cm' or 'm'.")
        if boundary.get("type") != "LWPOLYLINE" or boundary.get("closed") is not True:
            raise ValidationError(f"The boundary {request.boundary_handle} must be a closed LWPOLYLINE.")
        vecs: dict[str, Any] = {"start": None, "end": None}
        for name, handle in (("start", request.start_handle), ("end", request.end_handle)):
            if handle is None:
                continue
            vec = self._curve(handle)
            if len(vec["vertices"]) != 2:
                raise ValidationError(f"The {name} vector must have exactly 2 points (it has {len(vec['vertices'])}).")
            vecs[name] = [v[:2] for v in vec["vertices"]]
        start_region = (self._closed_region(request.start_region_handle, "start region", units)
                        if request.start_region_handle else None)
        end_region = (self._closed_region(request.end_region_handle, "end region", units)
                      if request.end_region_handle else None)
        poly = route_planner.polyline_points(boundary["vertices"], True, max_seg=200.0 * _UNIT_K[units])
        plan_id = f"ROUTE{next(_ROUTE_SEQ):03d}"
        cancel = threading.Event()
        plan = {"plan_id": plan_id, "status": "running", "created": time.time(), "units": units,
                "boundary_handle": request.boundary_handle, "start_handle": request.start_handle,
                "end_handle": request.end_handle, "start_region_handle": request.start_region_handle,
                "end_region_handle": request.end_region_handle, "allow_reverse": request.allow_reverse,
                "margin_mm": request.margin_mm, "time_budget_s": request.time_budget_s,
                "vehicle": veh.get("NAME"), "vehicle_overrides": overrides, "result": None,
                "progress": {"stage": "starting", "elapsed_s": 0.0}, "cancel": cancel,
                "geometry": {"boundary": poly}}

        def progress(info: dict) -> None:
            with _ROUTE_LOCK:
                cur = dict(plan.get("progress") or {})
                cur.update(info)
                cur["elapsed_s"] = round(time.time() - plan["created"], 1)
                plan["progress"] = cur

        def work() -> None:
            try:
                result = route_planner.plan_route(
                    poly, vecs["start"], vecs["end"], veh, units,
                    margin_mm=request.margin_mm, allow_reverse=request.allow_reverse,
                    max_gear_changes=request.max_gear_changes, time_budget_s=request.time_budget_s,
                    anchor=request.anchor, steer_fraction=request.steer_fraction,
                    allow_steer_in_place=request.allow_steer_in_place, stop_penalty_m=request.stop_penalty_m,
                    start_collision=request.start_collision,
                    end_position_tolerance_mm=request.end_position_tolerance_mm,
                    end_heading_tolerance_deg=request.end_heading_tolerance_deg,
                    start_region=start_region, end_region=end_region,
                    heading_tolerance_deg=request.heading_tolerance_deg, progress=progress, cancel=cancel)
            except route_planner.PlanningError as exc:
                result = {"success": False, "found": False, "reason": str(exc)}
            except Exception as exc:  # report, never kill the server
                result = {"success": False, "found": False, "reason": f"planner error: {exc!r}"}
            with _ROUTE_LOCK:
                plan["result"] = result
                plan["status"] = "cancelled" if (cancel.is_set() and not result.get("found")) else "done"
                plan["progress"] = {**(plan.get("progress") or {}), "stage": "done" if plan["status"] == "done" else "cancelled",
                                    "elapsed_s": round(time.time() - plan["created"], 1)}

        thread = threading.Thread(target=work, name=plan_id, daemon=True)
        plan["thread"] = thread
        with _ROUTE_LOCK:
            _ROUTE_PLANS[plan_id] = plan
        thread.start()
        thread.join(request.wait_s)
        with _ROUTE_LOCK:
            return {"success": True, **_plan_view(plan)}

    def get_vehicle_route_plan(self, plan_id: str, wait_s: float = 45.0) -> dict:
        with _ROUTE_LOCK:
            plan = _ROUTE_PLANS.get(plan_id)
        if plan is None:
            raise ValidationError(f"Unknown plan_id {plan_id} (plans live only while the server runs).")
        thread = plan.get("thread")
        if thread is not None and plan["status"] == "running":
            thread.join(max(0.0, min(float(wait_s), 55.0)))
        with _ROUTE_LOCK:
            return {"success": True, **_plan_view(plan)}

    def cancel_vehicle_route_plan(self, plan_id: str) -> dict:
        with _ROUTE_LOCK:
            plan = _ROUTE_PLANS.get(plan_id)
        if plan is None:
            raise ValidationError(f"Unknown plan_id {plan_id} (plans live only while the server runs).")
        if plan["status"] != "running":
            with _ROUTE_LOCK:
                return {"success": True, "note": f"plan already {plan['status']}", **_plan_view(plan)}
        plan["cancel"].set()
        thread = plan.get("thread")
        if thread is not None:
            thread.join(10.0)
        with _ROUTE_LOCK:
            return {"success": True, **_plan_view(plan)}

    def _stop_marks(self, segments: list[dict], unit: float) -> list[dict]:
        def side(a: float) -> str:
            return "0%%d" if abs(a) < 0.05 else f"{abs(a):.1f}%%d {'L' if a > 0 else 'P'}"

        marks = []
        for i, seg in enumerate(segments):
            for st in seg.get("stops") or []:
                marks.append({"pos": st["position"],
                              "text": f"stop: ko\u0142a {side(st['wheel_angle_before_deg'])} \\U+2192 "
                                      f"{side(st['wheel_angle_after_deg'])}"})
            before = seg.get("wheel_angle_before_deg")
            if i > 0 and before is not None and abs(before - seg["wheel_angle_start_deg"]) > 0.05:
                v = seg["vertices"][0]
                marks.append({"pos": [v[0], v[1]],
                              "text": f"zmiana biegu: ko\u0142a {side(before)} \\U+2192 {side(seg['wheel_angle_start_deg'])}"})
        _ = unit
        return marks

    def _swept_checks(self, segments: list[dict], handles: list[str], units: str, overrides: dict,
                      outline_interval_mm: float, cp: str, lock_tol_deg: float = 0.1,
                      lock_marks: bool = True) -> tuple[list, list]:
        checks, envelopes = [], []
        for seg, handle in zip(segments, handles):
            opts, _, _ = self._vehicle_opts(seg["gear"], units, overrides)
            opts.update({"auto_fillet": False, "interval": outline_interval_mm, "show_layers": True,
                         "guide": "rear" if str(seg.get("path", "")).startswith("rear") else "front",
                         "stops": [st["distance"] for st in seg.get("stops") or []],
                         "lock_tol_deg": float(lock_tol_deg), "lock_marks": bool(lock_marks)})
            value = self.adapter.lisp_call_expr(lisp_call("mo:swept-path", handle, opts, codepage=cp),
                                                undo=True, name="vehicle_swept_path") or {}
            checks.append({"gear": seg["gear"], "path_handle": handle, "guide": opts["guide"],
                           "feasible": value.get("feasible"), "required_steer_deg": value.get("required_steer_deg"),
                           "max_steer_deg": value.get("max_steer_deg"), "envelope_handle": value.get("envelope_handle"),
                           "lock_tolerance_deg": value.get("lock_tolerance_deg"),
                           "overshoot_places": value.get("overshoot_places"),
                           "steering_continuous": value.get("steering_continuous"),
                           "max_steer_change_per_step_deg": value.get("max_steer_change_per_step_deg"),
                           "stops_declared": value.get("stops_declared"),
                           "steering_jumps_at_stops": value.get("steering_jumps_at_stops"),
                           "end_position": value.get("end_position"), "layers": value.get("layers")})
            if value.get("envelope_handle"):
                envelopes.append(value["envelope_handle"])
        return checks, envelopes

    def draw_vehicle_route(self, request: DrawVehicleRouteRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        with _ROUTE_LOCK:
            plan = _ROUTE_PLANS.get(request.plan_id)
        if plan is None:
            raise ValidationError(f"Unknown plan_id {request.plan_id} (plans live only while the server runs).")
        result = plan.get("result")
        if plan["status"] not in ("done", "cancelled") or not result or not result.get("found"):
            raise ValidationError(f"Plan {request.plan_id} has no route to draw (status {plan['status']}).")
        segments = result["segments"]
        preview = {"layer": request.layer, "segments": [{"gear": s["gear"], "length": s["length"],
                                                          "path": s.get("path"), "stops": len(s.get("stops") or []),
                                                          "vertices": len(s["vertices"])} for s in segments],
                   "verify": request.verify}
        if dry_run:
            return {"success": True, "dry_run": True, "preview": preview}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        cp = self.adapter.lisp_codepage()
        unit = _UNIT_K[plan["units"]]
        drawn = self.adapter.lisp_call_expr(
            lisp_call("mo:route-draw", request.layer,
                      [{"gear": s["gear"], "vertices": s["vertices"]} for s in segments], codepage=cp),
            undo=True, name="draw_vehicle_route")
        out: dict[str, Any] = {"success": True, "dry_run": False, "plan_id": request.plan_id, "paths": drawn}
        marks = self._stop_marks(segments, unit)
        if marks:
            out["stop_marks"] = self.adapter.lisp_call_expr(
                lisp_call("mo:mark-stops", request.stop_layer, marks, 150.0 * unit, 200.0 * unit, codepage=cp),
                undo=True, name="mark_stops")
        violations = result.get("violations") or []
        if violations:
            to_cm = {"mm": 0.1, "cm": 1.0, "m": 100.0}[plan["units"]]
            vmarks = [{"from": v["from"], "to": v["to"],
                       "text": (f"naruszenie {v['intrusion'] * to_cm:.0f} cm" if v["crosses_boundary"]
                                else f"za blisko {v['intrusion'] * to_cm:.0f} cm")} for v in violations]
            out["violation_marks"] = self.adapter.lisp_call_expr(
                lisp_call("mo:mark-violations", request.violation_layer, vmarks, 60.0 * unit, 250.0 * unit,
                          codepage=cp), undo=True, name="mark_violations")
            out["violations"] = violations
        if not request.verify:
            return out
        checks, envelopes = self._swept_checks(segments, [p["handle"] for p in drawn or []], plan["units"],
                                               plan.get("vehicle_overrides") or {}, request.outline_interval_mm, cp,
                                               lock_tol_deg=request.lock_tolerance_deg)
        collision = self.adapter.lisp_call_expr(
            lisp_call("mo:envelope-check", plan["boundary_handle"], envelopes, codepage=cp), name="envelope_check") or []
        start_part = result.get("start_collision")
        ok = (not violations and all(c.get("feasible") is True for c in checks)
              and all(c.get("steering_continuous") is True for c in checks)
              and all(c.get("intersections") == 0 for c in collision))
        out.update({"swept_path": checks, "collision_check": collision, "verified": ok,
                    "verified_rule": ("every segment (forward and reverse alike): steering within the lock, no "
                                      "steering jump while moving (jumps at declared stops are allowed), envelope "
                                      "does not cross the boundary, no margin violations")})
        if start_part and not ok:
            out["note"] = ("the start pose is closer than the margin (see result.start_collision); a crossing "
                           "envelope at the very start comes from the start pose itself")
        return out

    def verify_vehicle_path(self, request: VerifyVehiclePathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        try:
            route_planner.require_numpy()
        except route_planner.PlanningError as exc:
            raise ValidationError(str(exc)) from exc
        from zwcad_standard_mcp import route_check

        overrides = {k: v for k, v in request.vehicle.model_dump().items() if v is not None}
        try:
            veh = archtools.vehicle(overrides)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        path = self._curve(request.path_handle)
        boundary = self._curve(request.boundary_handle)
        insunits = self._insunits(request.units) if request.units != "auto" else int(path.get("insunits") or 0)
        units = _UNIT_NAME.get(insunits)
        if units is None:
            raise ValidationError(f"Drawing units are not set (INSUNITS={insunits}); pass units='mm', 'cm' or 'm'.")
        if boundary.get("type") != "LWPOLYLINE" or boundary.get("closed") is not True:
            raise ValidationError(f"The boundary {request.boundary_handle} must be a closed LWPOLYLINE.")
        k = _UNIT_K[units]
        verts = path["vertices"]
        pts = (route_planner.polyline_points(verts, False, max_seg=50.0 * k)
               if any(len(v) > 2 and v[2] for v in verts) else [v[:2] for v in verts])
        poly = route_planner.polyline_points(boundary["vertices"], True, max_seg=200.0 * k)
        car = route_planner.Car.from_vehicle(veh, units)
        try:
            ana = route_check.analyse_trace(pts, car, poly, anchor=request.anchor,
                                            jump_deg_per_m=request.steer_jump_deg_per_m,
                                            ignore_start_collision=request.ignore_start_collision,
                                            margin=request.margin_mm * k, first_gear=request.first_gear,
                                            lock_tolerance_deg=request.lock_tolerance_deg)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        poses = ana.pop("_poses")
        for key_ in ("_delta", "_gear", "_clearance"):
            ana.pop(key_, None)
        out: dict[str, Any] = {"success": True, "units": units, "vehicle": veh.get("NAME"), "analysis": ana}
        if not request.draw or dry_run:
            out["dry_run"] = dry_run
            return out
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        cp = self.adapter.lisp_codepage()
        segments = []
        P = [list(map(float, p_[:2])) for p_ in pts]
        for run in ana["runs"]:
            seg_pts = P[run["from_index"]:run["to_index"] + 1]
            dist = [0.0]
            for a_, b_ in zip(seg_pts[:-1], seg_pts[1:]):
                dist.append(dist[-1] + math.dist(a_, b_))
            stops = [{"position": st["position"], "distance": round(dist[st["index"] - run["from_index"]], 3),
                      "wheel_angle_before_deg": st["before_deg"], "wheel_angle_after_deg": st["after_deg"]}
                     for st in run["stops"]]
            segments.append({"gear": run["gear"], "path": "rear axle centre", "length": run["length"],
                             "vertices": [[p_[0], p_[1], 0.0] for p_ in seg_pts], "stops": stops,
                             "wheel_angle_start_deg": 0.0})
        drawn = self.adapter.lisp_call_expr(
            lisp_call("mo:route-draw", request.layer, [{"gear": s["gear"], "vertices": s["vertices"]} for s in segments],
                      codepage=cp), undo=True, name="verify_vehicle_path")
        out["paths"] = drawn
        marks = [m for m in self._stop_marks(segments, k) if m["text"].startswith("stop")]
        if marks:
            out["stop_marks"] = self.adapter.lisp_call_expr(
                lisp_call("mo:mark-stops", request.stop_layer, marks, 150.0 * k, 200.0 * k, codepage=cp),
                undo=True, name="mark_stops")
        if ana["collisions"]:
            vmarks = []
            for c_ in ana["collisions"]:
                e0, e1 = c_["edge"]
                vmarks.append({"from": c_["point"], "to": c_["point"],
                               "text": f"kolizja {c_['max_penetration'] / (10.0 * k):.0f} cm ({c_['car_part']})"})
                _ = e0, e1
            out["violation_marks"] = self.adapter.lisp_call_expr(
                lisp_call("mo:mark-violations", request.violation_layer, vmarks, 60.0 * k, 250.0 * k, codepage=cp),
                undo=True, name="mark_violations")
        if ana["lock_exceedances"]:      # one mark per place, from the analysis (the swept path draws none here)
            lmarks = [{"from": e_["from"], "to": e_["to"],
                       "text": f"skr\u0119t {e_['max_steer_deg']:.2f}%%d > {ana['lock_deg']:.2f}%%d"}
                      for e_ in ana["lock_exceedances"]]
            out["lock_marks"] = self.adapter.lisp_call_expr(
                lisp_call("mo:mark-violations", request.lock_layer, lmarks, 40.0 * k, 200.0 * k, codepage=cp),
                undo=True, name="mark_lock_exceedances")
        checks, envelopes = self._swept_checks(segments, [p_["handle"] for p_ in drawn or []], units, overrides,
                                               request.outline_interval_mm, cp, lock_marks=False)
        for c_ in checks:   # the swept path tool estimates steering from chords; the analysis above is exact
            for key_ in ("feasible", "required_steer_deg", "steering_continuous", "max_steer_change_per_step_deg"):
                c_.pop(key_, None)
        out["swept_path"] = checks
        out["collision_check"] = self.adapter.lisp_call_expr(
            lisp_call("mo:envelope-check", request.boundary_handle, envelopes, codepage=cp), name="envelope_check") or []
        out["note"] = ("envelopes include the start part; collision_check intersections there come from the start "
                       "pose" if ana.get("start_collision") else None)
        _ = poses
        return out

    def vehicle_swept_path(self, request: SweptPathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        opts, veh, code = self._vehicle_opts(request.direction, request.units, request.vehicle.model_dump())
        reverse = request.direction == "reverse"
        info = {
            "vehicle": veh["NAME"],
            "max_steer_deg": round(archtools.max_steer_deg(veh), 2),
            "min_path_radius_mm": round(archtools.min_path_radius_mm(veh, reverse), 1),
            "fillet_radius_min_cm": math.ceil(archtools.min_path_radius_mm(veh, reverse) / 10.0),
            "path": "rear axle centre" if reverse else "front axle centre",
            "units": {1: "mm", 2: "cm", 3: "m"}[code],
        }
        if dry_run:
            return {"success": True, "dry_run": True, "preview": info,
                    "layers": ["SP_OBWIEDNIA", "SP_OPONY", "SP_POZYCJA", "SP_SCIEZKA", "SP_PRZEKROCZENIE", "SP_OBRYS"]}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        opts.update({"reverse": request.against_drawing_direction, "step": request.step_mm,
                     "interval": request.outline_interval_mm, "auto_fillet": request.auto_fillet,
                     "show_layers": request.show_layers, "lock_tol_deg": request.lock_tolerance_deg})
        value = self.adapter.lisp_call_expr(
            lisp_call("mo:swept-path", request.path_handle, opts, codepage=self.adapter.lisp_codepage()),
            undo=True, name="vehicle_swept_path")
        return {"success": bool(value and value.get("success")), "dry_run": False, "preview": info, "result": value}

    def round_path_corners(self, request: RoundPathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        opts, veh, _ = self._vehicle_opts(request.direction, request.units, request.vehicle.model_dump())
        minimum = math.ceil(archtools.min_path_radius_mm(veh, request.direction == "reverse") / 10.0)
        radius = request.radius_cm or minimum
        info = {"radius_cm": radius, "min_radius_cm": minimum, "below_minimum": radius < minimum}
        if dry_run:
            return {"success": True, "dry_run": True, "preview": info}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        opts["radius_cm"] = float(radius)
        opts["show_layers"] = request.show_layers
        value = self.adapter.lisp_call_expr(
            lisp_call("mo:round-path", request.path_handle, opts, codepage=self.adapter.lisp_codepage()),
            undo=True, name="round_path_corners")
        return {"success": bool(value and value.get("success")), "dry_run": False, "preview": info, "result": value}

