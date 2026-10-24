from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_document_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def get_current_document() -> dict:
        """Return the active drawing's name, path, saved state, read-only state and active layout."""
        return invoke(service.get_current_document)

    @mcp.tool()
    def list_documents() -> dict:
        """List all drawings open in the ZWCAD process."""
        return invoke(service.list_documents)

    @mcp.tool()
    def activate_document(name: str) -> dict:
        """Switch to an open drawing by name."""
        return invoke(service.activate_document, name)

    @mcp.tool()
    def save_document(file_path: str | None = None, confirm: bool = False) -> dict:
        """Save the active drawing (save-as when file_path is given). Refused unless confirm=true after the user confirmed the path."""
        return invoke(service.save_document, file_path, confirm)
