from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import AreaTakeoffRequest, DimStyleSpec, HatchRequest, TextStyleSpec
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_annotation_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def ensure_text_styles(styles: list[TextStyleSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        """Create or update text styles with a TrueType font (Polish characters, en dash and multiplication
        sign display correctly). dry_run=true previews; dry_run=false with confirm=true executes."""
        return invoke(service.ensure_text_styles, styles, dry_run, confirm)

    @mcp.tool()
    def ensure_dim_styles(styles: list[DimStyleSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        """Create or update dimension styles from DIM* variables.

        Typical site plan (PZT) drawn in cm at 1:500 showing metres with 2 decimals: linear_scale=0.01,
        decimals=2, decimal_separator=',', text_height=125 (2.5 mm on paper), arrow_block='_ARCHTICK',
        overall_scale=1 (always set it: drawings often carry a large DIMSCALE that multiplies text and arrows).
        set_current=true makes it the active style. Updating an existing style refreshes its dimensions.
        """
        return invoke(service.ensure_dim_styles, styles, dry_run, confirm)

    @mcp.tool()
    def create_hatch(request: HatchRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Hatch a closed boundary with optional islands (inner_handles, e.g. buildings) on a given layer.

        dry_run=true checks the boundaries and returns their areas and the expected hatch area;
        the executed result returns the hatch handle and its area (drawing units).
        """
        return invoke(service.create_hatch, request, dry_run, confirm)

    @mcp.tool()
    def area_takeoff(request: AreaTakeoffRequest) -> dict:
        """Site balance (bilans terenu) from closed outlines: plot, buildings, paved and other groups.

        Returns m² and % of plot, biologically active area (plot minus all listed areas), optional intensity
        (gross_floor_area_m2 / plot) and checks against MPZP limits. Units come from INSUNITS unless given.
        Read-only.
        """
        return invoke(service.area_takeoff, request)
