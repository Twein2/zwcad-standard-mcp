from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import PlotScopeRequest
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_layout_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def list_layouts() -> dict:
        """List model space and all layouts with the active layout and a plot-settings summary."""
        return invoke(service.list_layouts)

    @mcp.tool()
    def activate_layout(name: str) -> dict:
        """Switch to a model or paper layout by name."""
        return invoke(service.activate_layout, name)

    @mcp.tool()
    def get_layout_plot_settings(layout_name: str | None = None) -> dict:
        """Read a layout's plot settings: paper size, plot area, window, device, scale, rotation, plot style. Without layout_name reads the active layout."""
        return invoke(service.get_layout_plot_settings, layout_name)

    @mcp.tool()
    def get_plot_capabilities() -> dict:
        """List plot devices, paper sizes and output formats available in this ZWCAD installation."""
        return invoke(service.get_plot_capabilities)

    @mcp.tool()
    def preview_plot_scope(request: PlotScopeRequest) -> dict:
        """Resolve and preview the final plot area: bounding box, clipping risk and related plot settings."""
        return invoke(service.preview_plot_scope, request)

    @mcp.tool()
    def get_current_view() -> dict:
        """Return the current viewport center, size and bounding box (for 'display' plots)."""
        return invoke(service.get_current_view)

    @mcp.tool()
    def get_drawing_extents() -> dict:
        """Return the bounding box of all entities in the drawing (for 'extents' plots)."""
        return invoke(service.get_drawing_extents)

    @mcp.tool()
    def plot_layouts(
        layout_names: list[str],
        output_dir: str,
        plot_configuration: str | None = None,
        extension: str = "pdf",
        scope_type: str | None = None,
        window_lower_left: list[float] | None = None,
        window_upper_right: list[float] | None = None,
        selected_handles: list[str] | None = None,
        center_plot: bool | None = None,
        fit_to_paper: bool | None = None,
        custom_scale: float | None = None,
        override_policy: str = "temporary",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Plot layouts to files. Supports plot area (display/extents/limits/view/window/layout), window corners, centering, fit to paper and custom scale; verifies each output file. dry_run=true previews; dry_run=false with confirm=true plots."""
        return invoke(
            service.plot_layouts,
            layout_names,
            output_dir,
            plot_configuration,
            extension,
            scope_type,
            window_lower_left,
            window_upper_right,
            selected_handles,
            center_plot,
            fit_to_paper,
            custom_scale,
            override_policy,
            dry_run,
            confirm,
        )

    @mcp.tool()
    def export_drawing(
        base_file_path: str,
        extension: str = "DXF",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Export all entities of the active drawing with ZWCAD Export. dry_run=true previews; dry_run=false with confirm=true exports."""
        return invoke(service.export_drawing, base_file_path, extension, dry_run, confirm)

    @mcp.tool()
    def verify_export_files(
        file_paths: list[str],
        layout_names: list[str] | None = None,
        min_size_bytes: int = 1024,
        expected_plot_range: dict[str, list[float]] | None = None,
    ) -> dict:
        """Check exported files: existence, minimum size, layout and plot-range plausibility and common format signatures."""
        return invoke(service.verify_export_files, file_paths, layout_names, min_size_bytes, expected_plot_range)
