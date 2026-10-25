from __future__ import annotations

import os

from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import AreaFieldRequest, OffsetRequest, SunRulerRequest, SweptPathRequest
from zwcad_standard_mcp.services import CadService
from zwcad_standard_mcp.tools.common import invoke
from zwcad_standard_mcp.tools.register import register_all_tools


def _service(handler=None) -> tuple[CadService, FakeCadAdapter]:
    adapter = FakeCadAdapter()
    adapter.lisp_handler = handler
    return CadService(adapter, Settings(adapter="fake")), adapter


class _Env:
    def __init__(self, **values: str) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_execute_lisp_is_opt_in() -> None:
    service, _ = _service()
    with _Env(ZWCAD_MCP_ENABLE_LISP="0"):
        result = invoke(service.execute_lisp, "(+ 1 2)", True, False, True)
    assert result["success"] is False and result["code"] == "LISP_DISABLED"


def test_execute_lisp_dry_run_confirm_and_run() -> None:
    service, adapter = _service(lambda expr, undo: {"ok": True, "value": {"expr": expr, "undo": undo}})
    code = "; count lines\n(setq n 0)\n(sslength (ssget \"_X\" '((0 . \"LINE\")))) ; total"
    with _Env(ZWCAD_MCP_ENABLE_LISP="1", ZWCAD_MCP_CONFIRM_MODE="strict"):
        preview = service.execute_lisp(code)
        assert preview["dry_run"] and preview["forms"] == 2 and not adapter.lisp_calls
        refused = service.execute_lisp(code, dry_run=False, confirm=False)
        assert refused.get("success") is not True and not adapter.lisp_calls
        done = service.execute_lisp(code, dry_run=False, confirm=True)
    assert done["success"] and done["value"]["undo"] is True
    assert done["value"]["expr"].startswith("(progn (setq n 0)") and ";" not in done["value"]["expr"]


def test_execute_lisp_reports_lisp_error() -> None:
    service, _ = _service(lambda expr, undo: {"ok": False, "error": "bad argument type: numberp: nil"})
    with _Env(ZWCAD_MCP_ENABLE_LISP="1", ZWCAD_MCP_CONFIRM_MODE="off"):
        result = service.execute_lisp("(+ 1 nil)", dry_run=False)
    assert result == {"success": False, "dry_run": False, "code": "LISP_ERROR", "error": "bad argument type: numberp: nil"}


def test_insert_area_fields_builds_lisp_call() -> None:
    seen = {}

    def handler(expr, undo):
        seen["expr"], seen["undo"] = expr, undo
        return {"ok": True, "value": [{"success": True, "text": "POKOJ 12.5 m2"}]}

    service, adapter = _service(handler)
    adapter.drawing_units = lambda: 5  # type: ignore[method-assign]
    adapter.curve_areas = lambda handles: {h: {"success": True, "type": "LWPOLYLINE", "area": 125000.0} for h in handles}  # type: ignore[method-assign]
    request = AreaFieldRequest(pairs=[{"text_handle": "2A", "source_handle": "2B"}], decimal_separator=",")
    preview = service.insert_area_fields(request)
    assert preview["dry_run"] and preview["pairs"][0]["area_m2"] == 12.5
    assert preview["format"] == "%lu2%pr1%ds44%ct8[.0001]"
    with _Env(ZWCAD_MCP_CONFIRM_MODE="off"):
        result = service.insert_area_fields(request, dry_run=False)
    assert result["success"] and seen["undo"] is True
    assert seen["expr"].startswith('(mo:area-fields (list (list "2A" "2B")) (list (cons "format" "%lu2%pr1%ds44%ct8[.0001]")')


def test_sun_ruler_preview_and_params() -> None:
    seen = {}
    service, adapter = _service(lambda expr, undo: seen.update(expr=expr) or {"ok": True, "value": {"success": True}})
    adapter.drawing_units = lambda: 6  # type: ignore[method-assign]
    request = SunRulerRequest(base_point=[100, 200], obstruction_height_m=12, terrain_height_m=2,
                              start_time=8.0, end_time=12.5, room_number="M3", room_label_point=[110, 190])
    preview = service.draw_sun_ruler(request)["preview"]
    assert preview["duration"] == "4:30" and preview["h_m"] == 10 and preview["drawing_units_per_m"] == 1.0
    with _Env(ZWCAD_MCP_CONFIRM_MODE="off"):
        assert service.draw_sun_ruler(request, dry_run=False)["success"]
    expr = seen["expr"]
    assert expr.startswith("(mo:sun-ruler (list (cons \"base\" (list 100.0 200.0))")
    assert '(cons "room" "M3")' in expr and '(cons "make_block" T)' in expr and "start_dir" not in expr


def test_swept_path_needs_units_and_validates_vehicle() -> None:
    service, adapter = _service(lambda expr, undo: {"ok": True, "value": {"success": True, "expr": expr}})
    adapter.drawing_units = lambda: 0  # type: ignore[method-assign]
    result = invoke(service.vehicle_swept_path, SweptPathRequest(path_handle="AB"))
    assert result["code"] == "VALIDATION_ERROR" and "INSUNITS" in result["error"]
    request = SweptPathRequest(path_handle="AB", units="cm", direction="reverse", vehicle={"L": 2900})
    preview = service.vehicle_swept_path(request)["preview"]
    assert preview["path"] == "rear axle centre" and preview["units"] == "cm"
    with _Env(ZWCAD_MCP_CONFIRM_MODE="off"):
        done = service.vehicle_swept_path(request, dry_run=False)
    expr = done["result"]["expr"]
    assert expr.startswith('(mo:swept-path "AB" (list (cons "mode" "Tyl") (cons "units" 2)')
    assert '(cons "vehicle" (list (cons "L" 2900.0)))' in expr


def test_offset_validates_layer() -> None:
    service, _ = _service()
    result = invoke(service.offset_entities, OffsetRequest(handles=["10"], distance=5, layer="NOPE"))
    assert result["code"] == "VALIDATION_ERROR"


def test_new_tools_are_registered() -> None:
    names: list[str] = []

    class StubMCP:
        def tool(self, *a, **k):
            def deco(fn):
                names.append(fn.__name__)
                return fn
            return deco

    service, _ = _service()
    register_all_tools(StubMCP(), service)
    for name in ("get_screenshot", "zoom_view", "lisp_status", "execute_lisp", "offset_entities", "insert_area_fields",
                 "draw_sun_ruler", "vehicle_swept_path", "round_path_corners"):
        assert name in names
    assert len(names) == len(set(names))
