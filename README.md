# ZWCAD Standard MCP

A local [Model Context Protocol](https://modelcontextprotocol.io) server for **ZWCAD Standard**. Instead of
exposing every COM method to a language model, it offers a set of medium-grained tools built around common CAD
workflows. The tools work in batches, preview changes before applying them, and verify the results.

The server talks to a running ZWCAD instance through its public ActiveX/COM API (pywin32) and uses the official
MCP Python SDK.

> **About this fork (Twein2/zwcad-standard-mcp)**
> This fork builds on [qwtao321/zwcad-standard-mcp](https://github.com/qwtao321/zwcad-standard-mcp) (MIT).
> It has been tested on ZWCAD Standard 2024 (Polish locale) in architectural practice: site plans, survey
> maps, QGIS exports and batch PDF plotting. Changes compared with upstream:
>
> - **CAD-side filtered selection.** `count_entities`, `select_entities` and `update_entities_by_filter`
>   filter by DXF type, layer wildcards, ACI color, block name and space. Drawings with tens of thousands of
>   entities can be counted and changed in bulk within client timeouts.
> - **Dialog guard.** `open_document`, `plot_layouts` and batch jobs handle ZWCAD dialogs as follows:
>   - They refuse to start while a modal dialog is open (`MODAL_DIALOG_OPEN`).
>   - They set `PROXYNOTICE`, `XREFNOTIFY` and `BACKGROUNDPLOT` to 0 for the duration of the call and restore
>     them afterwards.
>   - They answer known-safe dialogs (unresolved references → "Ignore…", proxy notice → OK).
>   - They report every dialog seen during the call.
>
>   `list_cad_dialogs` and `dismiss_cad_dialog` work even while another COM call is blocked.
> - **Plot verification.** `plot_layouts` checks the result of `PlotToFile` and the output file, so a missing
>   or empty PDF is reported as a failure.
> - **Higher default limits.** Batch and query limits are now 10,000.
> - **Fix.** `activate_layout` is implemented in the COM adapter, so the server starts.
> - **English-only documentation and tool descriptions.**

## Problems it solves

| Problem | Tools |
|---|---|
| The AI cannot reach CAD; version or active drawing unknown | `diagnose_cad`, `get_cad_app_info`, `get_current_document` |
| An unknown DWG arrives: what layers, entities and blocks does it contain? | `count_entities`, `audit_drawing`, `list_layers`, `query_entities`, `list_block_references` |
| Imported entities sit on layer 0 with fixed colors and linetypes | `update_entities_by_filter`, `normalize_selected_entities`, `ensure_layers`, `update_entity_properties` |
| Read, change or transform a set of entities | `select_entities`, `get_selected_entities`, `get_entity_details`, `transform_entities` |
| Repetitive simple geometry, text and dimensions | `create_entities_batch` |
| Plot many layouts or a whole folder to PDF, check plot scope and devices first | `list_layouts`, `plot_layouts`, `preview_plot_scope`, `get_plot_capabilities`, `create_batch_job`, `scan_cad_folder` |
| Title blocks are attribute blocks that need checking and filling | `list_block_references`, `get_block_attributes`, `update_block_attributes` |
| Fear of accidental changes or deletions | dry-run by default, per-call `confirm`, second confirmation for delete/save, undo marks |
| ZWCAD dialogs block automation | `list_cad_dialogs`, `dismiss_cad_dialog`, dialog guard |

## Tools (44)

**System**: `diagnose_cad`, `get_cad_app_info`, `audit_drawing`, `list_cad_dialogs`, `dismiss_cad_dialog`

**Documents**: `get_current_document`, `list_documents`, `activate_document`, `save_document`

**Layers**: `list_layers`, `ensure_layers`, `map_layers` (apply an office layer standard from JSON: rename,
merge, purge; see `examples/layer_standard_geodetic_map.json`)

**Entities**:
- Read and select: `count_entities`, `select_entities`, `get_selected_entities`, `query_entities`,
  `get_entity_details`
- Change: `update_entity_properties`, `update_entities_by_filter`, `normalize_selected_entities`,
  `transform_entities`, `delete_entities`
- Text: `update_text_entities` (set, find/replace, strip MText formatting, repair UTF-8 read as cp1250)

**Drawing**: `create_entities_batch`. Supports lines, circles, arcs, lightweight polylines, text, MText, block
references, and aligned, rotated, radial and diametric dimensions.

**Layouts, plotting, extents**: `list_layouts`, `activate_layout`, `get_layout_plot_settings`,
`get_plot_capabilities`, `preview_plot_scope`, `get_current_view`, `get_drawing_extents`, `plot_layouts`,
`export_drawing`, `verify_export_files`

**Blocks**: `list_block_definitions`, `list_block_references`, `get_block_attributes`,
`update_block_attributes`, `insert_blocks_batch`

**Files**: `scan_cad_folder`, `open_document`, `close_document`, `create_batch_job`, `get_batch_job_status`

### Filtered selection

```json
{
  "filter": {
    "entity_types": ["POINT", "TEXT"],
    "layers": ["0", "MO_*"],
    "color": 256,
    "block_names": null,
    "space": "model"
  },
  "group_by": ["layer", "type"]
}
```

The filter fields work as follows:

- `entity_types` take DXF names: LINE, LWPOLYLINE, TEXT, MTEXT, INSERT, POINT, HATCH, DIMENSION and so on.
- `layers` accept CAD wildcards: `*`, `?`, `#` and `~`.
- `color` is an ACI number; 256 means ByLayer.
- `space` is `model`, `paper` or `all`.

ZWCAD evaluates the filter itself, so the server does not iterate entities over COM.

## Safety defaults

- **Per-call permission.** Read tools are always available. Every write tool defaults to `dry_run=true` and
  returns a preview. To execute, pass `dry_run=false, confirm=true` in the same call.
- **Second confirmations.** Deletion also requires `second_confirm=true`. Saving requires `confirm=true` and a
  `file_path`.
- **Batch limits.** By default a call writes at most 10,000 entities and returns at most 10,000 query results.
  Both limits can be configured.
- **Undo.** Bulk changes are wrapped in one undo mark, so they can be undone with a single Undo in ZWCAD.
- **Dialogs.** Only dialogs in a small safe list are answered automatically. Licence, recovery, save and
  unknown dialogs are never answered; they are reported instead.

### Turning confirmations off

If you work only on copies and do not want confirmation flags, set `ZWCAD_MCP_CONFIRM_MODE=off`:

- at install time: `install.bat off`;
- later: `.venv\Scripts\python.exe -m zwcad_standard_mcp.configure --confirm-mode off` (or edit the value in
  `claude_desktop_config.json`), then restart Claude Desktop.

`dry_run` previews stay available in both modes. Set the value back to `strict` to restore the checks.
The active mode is printed to the server log at start-up and returned by `diagnose_cad`.

## Requirements

- Windows 10 or 11
- ZWCAD Standard (tested with 2024) — the COM/ActiveX interface used here is part of the Standard edition
- Python 3.10 or newer (3.11 recommended)
- Claude Desktop, or another MCP client
- Git (optional; a ZIP download works too)

The default COM ProgID is `ZWCAD.Application`. Use `ZWCAD_MCP_PROG_ID` to change it, for example to
`ZWCAD.Application.2024`.

## Installation (Windows, about 5 minutes)

### 1. Install Python

Download Python 3.11 from <https://www.python.org/downloads/windows/> and run the installer.
On the first screen tick **"Add python.exe to PATH"**, then click **Install Now**.
Check it in a new Command Prompt (`Win+R`, type `cmd`, Enter):

```bat
py --version
```

It should print `Python 3.11.x` (or newer).

### 2. Download the server

Pick a short folder without spaces, for example `C:\MCP`.

With Git:

```bat
mkdir C:\MCP
cd /d C:\MCP
git clone https://github.com/Twein2/zwcad-standard-mcp.git
```

Without Git: on the GitHub page click **Code → Download ZIP**, unpack it to `C:\MCP` and rename the folder
to `zwcad-standard-mcp`.

### 3. Install and register in Claude Desktop

Double-click `C:\MCP\zwcad-standard-mcp\install.bat`. It:

1. creates a private Python environment in `.venv` (nothing is installed system-wide);
2. installs the server and its dependencies (pywin32, mcp, pydantic);
3. adds the server to Claude Desktop's `claude_desktop_config.json` (a timestamped backup of the old file is
   kept next to it; other servers in the file are not touched).

To start without confirmation prompts (see [Safety defaults](#safety-defaults)), run it from a Command
Prompt as `install.bat off`.

### 4. Restart Claude Desktop

Quit Claude Desktop completely (right-click the tray icon → **Quit**), then start it again.
The tools appear under the connectors/tools menu as **zwcad-standard**.

### 5. Check the connection

Start ZWCAD and open a drawing, then ask Claude: *"Run diagnose_cad."* All checks should be `ok: true`, and
`server_policy.confirm_mode` shows whether confirmations are on.

### Updating

```bat
cd /d C:\MCP\zwcad-standard-mcp
git pull
install.bat
```

(ZIP users: download the new ZIP, unpack over the old folder, run `install.bat`.) Then restart Claude Desktop.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `py` is not recognised | Reinstall Python with "Add python.exe to PATH", or open a new Command Prompt |
| Tools do not appear in Claude | Quit Claude Desktop from the tray (closing the window is not enough) and start it again; check the log `%APPDATA%\Claude\logs\mcp-server-zwcad-standard.log` |
| `diagnose_cad`: COM connection fails | Start ZWCAD first and open a drawing; if several ZWCAD versions are installed set `ZWCAD_MCP_PROG_ID` (e.g. `ZWCAD.Application.2024`) |
| Calls hang until a timeout | A modal dialog is open in ZWCAD — ask Claude to run `list_cad_dialogs`, or close it by hand |
| A call times out on a very large drawing | Use the filtered tools (`count_entities`, `update_entities_by_filter`, `map_layers`); long jobs stop before the client timeout and report what is left — run them again to continue |

### Manual configuration (other MCP clients)

`install.bat` writes the config for Claude Desktop. For other clients add this entry yourself (adjust the
path), or run `.venv\Scripts\python.exe -m zwcad_standard_mcp.configure --config <path-to-config.json>`:

```json
{
  "mcpServers": {
    "zwcad-standard": {
      "command": "C:\\MCP\\zwcad-standard-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "zwcad_standard_mcp.server"],
      "env": {
        "ZWCAD_MCP_PROG_ID": "ZWCAD.Application",
        "ZWCAD_MCP_TRANSPORT": "stdio",
        "ZWCAD_MCP_CONFIRM_MODE": "strict"
      }
    }
  }
}
```

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `ZWCAD_MCP_PROG_ID` | `ZWCAD.Application` | COM ProgID |
| `ZWCAD_MCP_CONFIRM_MODE` | `strict` | `strict`: writes need `confirm=true`, deletes also `second_confirm=true`, saves an explicit path. `off`: no confirmation flags needed |
| `ZWCAD_MCP_AUTO_START` | `false` | Start ZWCAD when no running instance is found |
| `ZWCAD_MCP_MAX_QUERY_RESULTS` | `10000` | Maximum results returned by a query |
| `ZWCAD_MCP_MAX_BATCH_SIZE` | `10000` | Maximum entities written in one call |
| `ZWCAD_MCP_AUTO_DISMISS_DIALOGS` | `true` | Answer known-safe ZWCAD dialogs during open/plot |
| `ZWCAD_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `ZWCAD_MCP_ADAPTER` | `com` | `com`; `fake` for tests |
| `ZWCAD_MCP_LOG_LEVEL` | `INFO` | Log level |

## Recommended validation order

1. **Read-only:** `diagnose_cad`, `get_current_document`, `count_entities`, `list_layers`,
   `get_selected_entities`.
2. **Low-risk edits on a copy:**
   - `ensure_layers`, then `update_entities_by_filter` with `dry_run=true`.
   - Execute with `confirm=true`.
   - Test Undo in ZWCAD.
3. **Title blocks:**
   - `list_block_references(has_attributes=true)`, then `get_block_attributes`.
   - `update_block_attributes` with `dry_run=true`, then execute.
   - Read back to verify.
4. **Plotting:**
   - `get_plot_capabilities`, `preview_plot_scope`, then `plot_layouts`.
   - For a whole folder: `scan_cad_folder`, then `create_batch_job(confirm=true)`, `get_batch_job_status` and
     `verify_export_files`.

## Examples

Bulk-move points from layer 0 (preview):

```json
{
  "patch": {
    "filter": {"entity_types": ["POINT"], "layers": ["0"]},
    "layer": "MO_GEO_POINTS",
    "color": 256
  },
  "dry_run": true
}
```

Draw a rectangle and a hole:

```json
{
  "entities": [
    {"entity_type": "lwpolyline", "layer": "OUTLINE",
     "params": {"vertices": [[0, 0], [300, 0], [300, 200], [0, 200]], "closed": true}},
    {"entity_type": "circle", "layer": "HOLE", "params": {"center": [25, 25, 0], "radius": 5}}
  ],
  "dry_run": true
}
```

Update title block attributes:

```json
{
  "updates": [{"handle": "3AF", "attributes": {"DESIGNER": "Jan Kowalski", "DATE": "2026-07-21"}}],
  "dry_run": true
}
```

More examples are in `examples/USAGE.md`.

## Tests

The repository includes a fake adapter that needs neither Windows nor ZWCAD:

```bat
pip install -e ".[dev]"
pytest
```

To inspect the tool schemas locally, set `ZWCAD_MCP_ADAPTER=fake` and run `python -m zwcad_standard_mcp.server`.

## Known limitations

- Mechanical-edition objects (frames, BOM, balloons) are not supported.
- Dynamic blocks, nested blocks, attributes inside XREFs, fields and complex proxy objects need dedicated work.
- `plot_layouts` relies on a usable plot configuration in each layout. Company PC3/PC5, PMP and paper setups
  must be checked on the target machine.
- `query_entities` and `audit_drawing` still iterate entities over COM. On large drawings use
  `count_entities` and `select_entities` instead.
- Dialogs not in the safe list still block a COM call until a user, or `dismiss_cad_dialog`, answers them.
- Undo marks make changes easy to undo, but they are not database transactions.

## Project structure

```
zwcad-standard-mcp/
├── README.md
├── pyproject.toml
├── install.bat / start.bat
├── mcp-config.example.json
├── docs/            design notes and acceptance tests
├── examples/        agent usage examples
├── src/zwcad_standard_mcp/
│   ├── adapters/    com.py, fake.py, selection.py, dialogs.py
│   ├── services/
│   ├── tools/
│   ├── config.py
│   ├── models.py
│   └── server.py
└── tests/
```

## Credits and licence

- Original project: [qwtao321/zwcad-standard-mcp](https://github.com/qwtao321/zwcad-standard-mcp), MIT licence.
  The copyright notice in `LICENSE` is kept.
- Planned additions from [dalingo81/ZWCAD-MCP](https://github.com/dalingo81/ZWCAD-MCP) (MIT) will keep its
  notice as well.

## References

- ZWCAD application development support: https://www.zwsoft.com/support/zwcad-devdoc
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- ZWCAD Mechanical MCP reference implementation: https://github.com/john0909/ZWCAD-Mechanical-MCP
