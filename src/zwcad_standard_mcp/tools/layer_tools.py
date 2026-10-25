from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import LayerMapRequest, LayerSpec
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_layer_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def list_layers(detail: bool = True) -> dict:
        """List layers (find messy layers, inconsistent colors or linetypes in external drawings)."""
        return invoke(service.list_layers, detail)

    @mcp.tool()
    def ensure_layers(layers: list[LayerSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        """Create or update layers in bulk. dry_run=true previews; dry_run=false with confirm=true executes."""
        return invoke(service.ensure_layers, layers, dry_run, confirm)

    @mcp.tool()
    def map_layers(request: LayerMapRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Map source layers to an office layer standard (rename, merge, purge).

        The standard (inline or JSON file) lists target layers with properties and rules
        {source: wildcard, target: layer}; the first matching rule wins. A missing target is created by
        renaming its largest source layer (instant); other sources are merged by moving their entities with
        CAD-side selections, then deleted when empty. dry_run=true returns the plan, unmatched layers and an
        time estimate. Long merges stop at time_budget_s and report incomplete_sources: call again to continue.
        """
        return invoke(service.map_layers, request, dry_run, confirm)
