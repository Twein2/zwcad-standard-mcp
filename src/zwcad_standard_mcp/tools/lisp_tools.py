from __future__ import annotations

import json
from typing import Any

from zwcad_standard_mcp.models import (
    AreaFieldRequest,
    OffsetRequest,
    RoundPathRequest,
    SunRulerRequest,
    SweptPathRequest,
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
        """Equinox shadow ruler (linijka nasłonecznienia, LinijkaSlonca) at a building corner.

        Draws shadow lines every 30 min 7:00–17:00 for an obstruction of the given height, height lines 1–5 m,
        the exposure sector between two directions (or times) and groups it into a block; optional result
        description and room label. Layers a_naslonecznienie_*. dry_run=true returns the exposure window.
        """
        return invoke(service.draw_sun_ruler, request, dry_run, confirm)

    @mcp.tool()
    def vehicle_swept_path(request: SweptPathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Swept path of a car (obwiednia ruchu, CAYENNE.LSP): body with mirrors and outer tyre tracks.

        Forward: the path is the front axle centre; reverse: the rear axle centre. Sharp polyline corners are
        rounded with the minimum radius first (auto_fillet). Reports whether the path is feasible and marks
        places that need more steering on SP_PRZEKROCZENIE. dry_run=true returns the vehicle limits.
        """
        return invoke(service.vehicle_swept_path, request, dry_run, confirm)

    @mcp.tool()
    def round_path_corners(request: RoundPathRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Round the corners of a straight-segment route with the vehicle's minimum radius (SCIEZKA).

        Creates a new polyline on SP_SCIEZKA (the original stays); problem places get circles on SP_PRZEKROCZENIE.
        """
        return invoke(service.round_path_corners, request, dry_run, confirm)
