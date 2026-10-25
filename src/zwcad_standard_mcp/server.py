from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from zwcad_standard_mcp.adapters import build_adapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.services import CadService
from zwcad_standard_mcp.tools import register_all_tools


SERVER_INSTRUCTIONS = """
Operate the locally running ZWCAD Standard application through its public COM API.
The server is designed around common user pain points: diagnosing connection issues,
understanding unfamiliar drawings, normalizing imported entities, batch drafting,
multi-layout plotting, and block/title-block attribute maintenance.

Safety rules:
- Mutating tools default to dry_run=true (preview).
- In confirm mode "strict" (default) writes need confirm=true, deletes also second_confirm=true,
  and saves need an explicit path. With ZWCAD_MCP_CONFIRM_MODE=off these flags are not required.
  diagnose_cad reports the active mode.
- Batch edits use a ZWCAD undo mark where the COM API supports it.
- Prefer querying or reading selected objects before modifying them.
""".strip()


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    adapter = build_adapter(settings)
    service = CadService(adapter, settings)
    mcp = FastMCP(
        name="ZWCAD Standard MCP",
        instructions=SERVER_INSTRUCTIONS,
        json_response=True,
    )
    register_all_tools(mcp, service)
    return mcp


def main() -> None:
    from zwcad_standard_mcp.settings_file import apply_settings_file

    file_report = apply_settings_file()
    settings = Settings.from_env()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, settings.log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    from zwcad_standard_mcp.permissions_bridge import confirm_mode

    mode = confirm_mode()
    if file_report.get("path"):
        logging.getLogger("zwcad_standard_mcp").info("Settings file: %s %s", file_report["path"], file_report.get("applied"))
    logging.getLogger("zwcad_standard_mcp").info(
        "Confirmation mode: %s%s", mode,
        " (set ZWCAD_MCP_CONFIRM_MODE=off to run writes, deletes and saves without confirm flags)"
        if mode == "strict" else " (writes, deletes and saves run without confirmation; set ZWCAD_MCP_CONFIRM_MODE=strict to restore)",
    )
    mcp = create_server(settings)
    if settings.transport in {"streamable-http", "streamable_http", "http"}:
        mcp.run(transport="streamable-http")
    elif settings.transport == "stdio":
        mcp.run()
    else:
        raise ValueError(
            "ZWCAD_MCP_TRANSPORT must be 'stdio' or 'streamable-http'."
        )


if __name__ == "__main__":
    main()
