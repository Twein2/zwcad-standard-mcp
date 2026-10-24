from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.models import BlockAttributePatch
from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_block_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def list_block_definitions(detail: bool = False) -> dict:
        """List block definitions (standard parts, drawing frames, title blocks, external references)."""
        return invoke(service.list_block_definitions, detail)

    @mcp.tool()
    def list_block_references(
        scope: str = "all_layouts",
        block_name: str | None = None,
        has_attributes: bool | None = None,
        limit: int = 200,
    ) -> dict:
        """List block references in model space or layouts; filter by block name and by whether they carry attributes."""
        return invoke(
            service.list_block_references,
            scope,
            block_name,
            has_attributes,
            limit,
        )

    @mcp.tool()
    def get_block_attributes(handles: list[str]) -> dict:
        """Read attribute TAGs and values of block references by handle (title block checks)."""
        return invoke(service.get_block_attributes, handles)

    @mcp.tool()
    def update_block_attributes(
        updates: list[BlockAttributePatch],
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Update block attributes by handle. dry_run=true previews and reports unknown TAGs; dry_run=false with confirm=true applies and reads back."""
        return invoke(service.update_block_attributes, updates, dry_run, confirm)

    @mcp.tool()
    def insert_blocks_batch(
        blocks: list[dict],
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Insert existing block definitions in bulk, optionally filling attributes. dry_run=true previews; dry_run=false with confirm=true executes."""
        return invoke(service.insert_blocks, blocks, dry_run, confirm)
