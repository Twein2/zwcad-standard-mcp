from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import (
    EntityFilter,
    EntityPropertyPatch,
    EntityQuerySpec,
    FilteredPropertyPatch,
    TransformRequest,
)
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_entity_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def get_selected_entities(limit: int = 200) -> dict:
        """Read the entities the user pre-selected in ZWCAD: entity list, handles, combined bounding box, space and whether the selection is empty."""
        return invoke(service.get_selected_entities, limit)

    @mcp.tool()
    def query_entities(query: EntityQuerySpec) -> dict:
        """Query entities by space, entity type, layer, text content or block name (iterates entities; on large drawings prefer count_entities/select_entities)."""
        return invoke(service.query_entities, query)

    @mcp.tool()
    def get_entity_details(handles: list[str]) -> dict:
        """Read geometry, common properties, bounding box and block attributes of entities by handle."""
        return invoke(service.get_entity_details, handles)

    @mcp.tool()
    def update_entity_properties(
        patch: EntityPropertyPatch,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Change layer, color, linetype, lineweight or visibility of entities by handle. dry_run=true previews; dry_run=false with confirm=true executes."""
        return invoke(service.update_entity_properties, patch, dry_run, confirm)

    @mcp.tool()
    def normalize_selected_entities(
        target_layer: str | None = None,
        set_color_bylayer: bool = True,
        set_linetype_bylayer: bool = True,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Move the current selection to a target layer and optionally set color/linetype to ByLayer (clean up imported drawings). dry_run=true previews; dry_run=false with confirm=true executes."""
        return invoke(
            service.normalize_selected_entities,
            target_layer,
            set_color_bylayer,
            set_linetype_bylayer,
            dry_run,
            confirm,
        )

    @mcp.tool()
    def transform_entities(request: TransformRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        """Move, copy, rotate, scale or mirror entities in bulk (angles in radians). dry_run=true previews; dry_run=false with confirm=true executes."""
        return invoke(service.transform_entities, request, dry_run, confirm)

    @mcp.tool()
    def delete_entities(
        handles: list[str],
        dry_run: bool = True,
        confirm: bool = False,
        second_confirm: bool = False,
    ) -> dict:
        """Delete entities in bulk. Preview with dry_run first, then execute with dry_run=false, confirm=true and second_confirm=true."""
        return invoke(service.delete_entities, handles, dry_run, confirm, second_confirm)

    @mcp.tool()
    def count_entities(filter: EntityFilter, group_by: list[str] | None = None) -> dict:
        """Count entities with a CAD-side selection filter (fast on large drawings).

        filter: DXF entity names (POINT, TEXT, LWPOLYLINE ...), layer names/wildcards,
        ACI color, block names, space (model/paper/all). group_by: ["layer"] and/or ["type"].
        """
        return invoke(service.count_entities, filter, group_by)

    @mcp.tool()
    def select_entities(filter: EntityFilter, limit: int = 200) -> dict:
        """Return the total count and up to `limit` handles of entities matching a CAD-side filter."""
        return invoke(service.select_entities, filter, limit)

    @mcp.tool()
    def update_entities_by_filter(
        patch: FilteredPropertyPatch,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Bulk change layer/color/linetype/linetype scale/lineweight of every entity matching a filter.

        dry_run=true returns the match count per layer; dry_run=false and confirm=true applies it in one
        undo group without per-entity snapshots (thousands of entities per call).
        """
        return invoke(service.update_entities_by_filter, patch, dry_run, confirm)

