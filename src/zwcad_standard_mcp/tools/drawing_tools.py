from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import EntityCreateSpec
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_drawing_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def create_entities_batch(
        entities: list[EntityCreateSpec],
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Create lines, circles, arcs, polylines, text, MText, block references and common dimensions in bulk. dry_run=true previews; dry_run=false with confirm=true writes.

        lwpolyline params: vertices [[x, y], ...] or [[x, y, bulge], ...] (bulge = tan(arc angle / 4), + = CCW,
        for the segment that starts at that vertex), optional "bulges" list instead, "closed".
        """
        return invoke(service.create_entities, entities, dry_run, confirm)
