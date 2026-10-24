from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import LayerSpec
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
