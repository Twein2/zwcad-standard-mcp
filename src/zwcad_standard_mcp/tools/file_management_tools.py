from __future__ import annotations

from typing import Any

from zwcad_standard_mcp.services import CadService

from .common import invoke


def register_file_management_tools(mcp: Any, service: CadService) -> None:
    @mcp.tool()
    def scan_cad_folder(path: str, recursive: bool = False) -> dict:
        """List CAD files (.dwg/.dxf/.dwt) in a local folder."""
        return invoke(service.scan_cad_folder, path, recursive)

    @mcp.tool()
    def open_document(
        path: str,
        read_only: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Open a DWG/DXF file in ZWCAD. dry_run=true previews; dry_run=false with confirm=true opens it (with the dialog guard)."""
        return invoke(service.open_document, path, read_only, dry_run, confirm)

    @mcp.tool()
    def close_document(
        name: str,
        save_changes: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Close an open drawing by name (does not save by default). Requires confirm=true."""
        return invoke(service.close_document, name, save_changes, dry_run, confirm)

    @mcp.tool()
    def create_batch_job(
        total: int,
        files: list[str] | None = None,
        operation: str = "plot_pdf",
        output_dir: str | None = None,
        config: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        """Create a batch job and return its id. With files given, runs it in the background (default operation=plot_pdf). Execution requires confirm=true."""
        return invoke(service.create_batch_job, total, files, operation, output_dir, config, dry_run, confirm)

    @mcp.tool()
    def get_batch_job_status(job_id: str) -> dict:
        """Return batch job progress (total/finished/failed/status)."""
        return invoke(service.get_batch_job_status, job_id)
