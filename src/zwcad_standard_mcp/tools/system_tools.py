from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_system_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def diagnose_cad() -> dict:
        """Diagnose the ZWCAD COM connection, the active drawing and the server's write policy."""
        return invoke(service.diagnose_cad)

    @mcp.tool()
    def get_cad_app_info() -> dict:
        """Return the ZWCAD version, installation path and visibility."""
        return invoke(service.get_app_info)

    @mcp.tool()
    def audit_drawing(sample_limit: int = 30) -> dict:
        """Quick drawing inventory: entity types, layer distribution, block references, layouts and a few samples (iterates entities; slow on very large drawings, use count_entities there)."""
        return invoke(service.audit_drawing, sample_limit)

    @mcp.tool()
    def list_cad_dialogs() -> dict:
        """List modal dialogs currently open in ZWCAD (they block COM calls). Works while another call is blocked."""
        return invoke(service.list_cad_dialogs)

    @mcp.tool()
    def dismiss_cad_dialog(title_contains: str, button: str, confirm: bool = False) -> dict:
        """Press a button (by caption) in an open ZWCAD dialog whose title contains `title_contains`. Requires confirm=true."""
        return invoke(service.dismiss_cad_dialog, title_contains, button, confirm)

