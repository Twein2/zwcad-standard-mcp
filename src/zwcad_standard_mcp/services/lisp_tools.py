"""Tools backed by the AutoLISP channel, plus screenshot and offset (mixed into CadService)."""

from __future__ import annotations

import math
import os
from typing import Any

from zwcad_standard_mcp import archtools
from zwcad_standard_mcp.adapters.lisp_channel import lisp_call, strip_lisp_comments
from zwcad_standard_mcp.errors import LispDisabledError, ValidationError
from zwcad_standard_mcp.models import (
    AreaFieldRequest,
    OffsetRequest,
    RoundPathRequest,
    SunRulerRequest,
    SweptPathRequest,
)
from zwcad_standard_mcp.permissions.policy import PermissionLevel
from zwcad_standard_mcp.permissions_bridge import require_permission


_UNITS_PER_M = {4: 1000.0, 5: 100.0, 6: 1.0}
_CAYENNE_UNITS = {4: 1, 5: 2, 6: 3}


def lisp_enabled() -> bool:
    """execute_lisp (arbitrary code) is opt-in: ZWCAD_MCP_ENABLE_LISP=1."""
    return os.getenv("ZWCAD_MCP_ENABLE_LISP", "0").strip().lower() in {"1", "true", "yes", "on"}


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
                     "interval": request.outline_interval_mm, "auto_fillet": request.auto_fillet})
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
        value = self.adapter.lisp_call_expr(
            lisp_call("mo:round-path", request.path_handle, opts, codepage=self.adapter.lisp_codepage()),
            undo=True, name="round_path_corners")
        return {"success": bool(value and value.get("success")), "dry_run": False, "preview": info, "result": value}

