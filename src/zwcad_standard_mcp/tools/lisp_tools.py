from __future__ import annotations

import json
from typing import Any

from zwcad_standard_mcp.models import (
    AreaFieldRequest,
    DrawVehicleRouteRequest,
    OffsetRequest,
    RoundPathRequest,
    SunRulerRequest,
    SweptPathRequest,
    VehicleRouteRequest,
)
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_lisp_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def get_screenshot(region: str = "drawing", max_width: int = 1280):
        """Screenshot of the ZWCAD window (PrintWindow; works when ZWCAD is behind other windows).

        region='drawing' crops to the drawing area, 'window' returns the whole window (ribbon, command line).
        Use it after writes whose display matters: COM properties can differ from what is drawn until REGEN.
        """
        from mcp.server.fastmcp import Image

        result = invoke(service.get_screenshot, region, max_width)
        if "png" not in result:
            return result
        meta = {k: v for k, v in result.items() if k != "png"}
        return [Image(data=result["png"], format="png"), json.dumps(meta)]

    @mcp.tool()
    def zoom_view(mode: str = "extents", lower_left: list[float] | None = None, upper_right: list[float] | None = None,
                  handles: list[str] | None = None, margin: float = 0.05) -> dict:
        """Change the view of the active drawing: mode 'extents', 'window' (lower_left/upper_right) or 'entities'
        (bounding box of handles plus margin as a fraction). Use before get_screenshot."""
        return invoke(service.zoom_view, mode, lower_left, upper_right, handles, margin)

    @mcp.tool()
    def lisp_status() -> dict:
        """Check the AutoLISP channel (loads the bundled LISP library into the active drawing if needed)."""
        return invoke(service.lisp_status)

    @mcp.tool()
    def execute_lisp(code: str, undo: bool = True, dry_run: bool = True, confirm: bool = False,
                     timeout_s: float = 300.0) -> dict:
        """Run AutoLISP code in the active drawing and return the value of the last expression as JSON.

        Opt-in: needs ZWCAD_MCP_ENABLE_LISP=1. dry_run=true only parses; dry_run=false with confirm=true runs it,
        wrapped in one undo mark when undo=true. Use for fast bulk work (ssget/entget/entmod run inside ZWCAD).
        Returned values: assoc lists with string keys -> objects, lists -> arrays, ENAME -> handle.
        Interactive functions (getpoint, entsel, ...) wait for the user and should not be used.
        """
        return invoke(service.execute_lisp, code, undo, dry_run, confirm, timeout_s)

    @mcp.tool()
    def offset_entities(request: OffsetRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Offset curves (lines, polylines, arcs, circles) by a distance, optionally to both sides and onto a layer."""
        return invoke(service.offset_entities, request, dry_run, confirm)

    @mcp.tool()
    def insert_area_fields(request: AreaFieldRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Put a live area field (m²) of a polyline / hatch into room or zone labels (WklejPole / WklejPoleHatch).

        The label text before the old value is kept ("POKÓJ 1 12.5 m²"); with_layer=true writes
        "<layer> - <area>". The format follows the drawing units (cm² → m², 1 decimal by default).
        dry_run=true checks the sources and shows the areas.
        """
        return invoke(service.insert_area_fields, request, dry_run, confirm)

    @mcp.tool()
    def draw_sun_ruler(request: SunRulerRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Equinox sun ruler (linijka nasłonecznienia, LinijkaSlonca) at an evaluated point, e.g. a window.

        Lines point towards the sun every 30 min 7:00–17:00; their length is the distance at which an obstruction
        H = obstruction_height_m - terrain_height_m above the point just reaches the sun ray. Height lines 1–5 m,
        the exposure sector between two directions (or times) and groups it into a block; optional result
        description and room label. Layers a_naslonecznienie_*. dry_run=true returns the exposure window.
        """
        return invoke(service.draw_sun_ruler, request, dry_run, confirm)

    @mcp.tool()
    def vehicle_swept_path(request: SweptPathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Swept path of a car (obwiednia ruchu, CAYENNE.LSP) along a GIVEN path: body with mirrors and tyre tracks.

        It does not plan or search for a route. To find a route from a start vector to an end vector inside a
        boundary use plan_vehicle_route + draw_vehicle_route (they call this tool for verification).
        Forward: the path is the front axle centre; reverse: the rear axle centre. Sharp polyline corners are
        rounded with the minimum radius first (auto_fillet; use false for planned, already smooth paths).
        Reports whether the path is feasible and marks places that need more steering on SP_PRZEKROCZENIE.
        Existing SP_* layers that are off are switched on (show_layers). dry_run=true returns the vehicle limits.
        """
        return invoke(service.vehicle_swept_path, request, dry_run, confirm)

    @mcp.tool()
    def plan_vehicle_route(request: VehicleRouteRequest) -> dict:
        """Find a collision-free car route inside a closed boundary from a start vector to an end vector.

        USE THIS for requests like "draw the vehicle route from start to end avoiding the boundary". Read-only:
        reads the boundary (closed LWPOLYLINE) and two vectors (2-point LWPOLYLINE or LINE, tail -> head = the
        direction the car faces; by default the vector midpoint is the body centre), then plans in the server
        (hybrid A* + exact Dubins tail, whole body with mirrors kept margin_mm from the boundary, Porsche Cayenne
        defaults, overridable). allow_reverse=false checks forward-only driving and answers within seconds when
        it is impossible. Planning runs in the background: if status is 'running', poll get_vehicle_route_plan.
        Then draw_vehicle_route(plan_id) draws one polyline per gear and verifies it with the swept path.
        Do not write your own planner or call vehicle_swept_path on the vectors.
        """
        return invoke(service.plan_vehicle_route, request)

    @mcp.tool()
    def get_vehicle_route_plan(plan_id: str, wait_s: float = 45.0) -> dict:
        """Status / result of a plan_vehicle_route job (waits up to wait_s, max 55 s, while it is running)."""
        return invoke(service.get_vehicle_route_plan, plan_id, wait_s)

    @mcp.tool()
    def draw_vehicle_route(request: DrawVehicleRouteRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Draw a planned route (one LWPOLYLINE with arcs per gear: forward = front axle path, green; reverse = rear
        axle path, red) and, with verify=true, run vehicle_swept_path on every segment and check that the
        envelopes do not intersect the boundary. Returns handles, steering, intersections and clearance."""
        return invoke(service.draw_vehicle_route, request, dry_run, confirm)

    @mcp.tool()
    def round_path_corners(request: RoundPathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Round the corners of a straight-segment route with the vehicle's minimum radius (SCIEZKA).

        Creates a new polyline on SP_SCIEZKA (the original stays); problem places get circles on SP_PRZEKROCZENIE.
        """
        return invoke(service.round_path_corners, request, dry_run, confirm)
