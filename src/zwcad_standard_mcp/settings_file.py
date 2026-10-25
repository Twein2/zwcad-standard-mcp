"""Server settings from a JSON file, so they survive Claude Desktop rewriting its config (FINDINGS #43).

Claude Desktop keeps claude_desktop_config.json in memory and rewrites the whole file while it runs, so
values edited there (or by configure.py) while the app is open are lost. The server therefore also reads a
settings file of its own. Environment variables from the client config still win.

Search order (first existing file is used):
1. the path in ZWCAD_MCP_SETTINGS
2. %USERPROFILE%\\zwcad-mcp\\settings.json (written by configure.py)
3. zwcad-mcp.settings.json in the repository folder (next to pyproject.toml; git-ignored)

Keys: friendly names ("confirm_mode": "strict"|"off", "execute_lisp": true|false, "prog_id", "lisp_counts",
"codepage", "ipc_dir", "max_batch_size", "max_query_results", "log_level") or raw ZWCAD_MCP_* names.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

FRIENDLY_KEYS = {
    "confirm_mode": "ZWCAD_MCP_CONFIRM_MODE",
    "execute_lisp": "ZWCAD_MCP_ENABLE_LISP",
    "prog_id": "ZWCAD_MCP_PROG_ID",
    "lisp_counts": "ZWCAD_MCP_LISP_COUNTS",
    "codepage": "ZWCAD_MCP_CODEPAGE",
    "ipc_dir": "ZWCAD_MCP_IPC_DIR",
    "max_batch_size": "ZWCAD_MCP_MAX_BATCH_SIZE",
    "max_query_results": "ZWCAD_MCP_MAX_QUERY_RESULTS",
    "auto_dismiss_dialogs": "ZWCAD_MCP_AUTO_DISMISS_DIALOGS",
    "log_level": "ZWCAD_MCP_LOG_LEVEL",
}

REPO_SETTINGS_NAME = "zwcad-mcp.settings.json"

_loaded: dict = {"path": None, "applied": {}, "overridden_by_env": []}


def user_settings_path() -> Path:
    return Path(os.path.expanduser("~")) / "zwcad-mcp" / "settings.json"


def repo_settings_path() -> Path:
    return Path(__file__).resolve().parents[2] / REPO_SETTINGS_NAME


def candidate_paths() -> list[Path]:
    paths = []
    if os.getenv("ZWCAD_MCP_SETTINGS"):
        paths.append(Path(os.environ["ZWCAD_MCP_SETTINGS"]))
    paths.extend([user_settings_path(), repo_settings_path()])
    return paths


def _as_env_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def apply_settings_file() -> dict:
    """Load the first settings file into os.environ (existing variables are kept). Returns a report."""
    for path in candidate_paths():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            logging.getLogger("zwcad_standard_mcp").warning("Settings file %s ignored: %s", path, exc)
            _loaded.update(path=str(path), error=str(exc))
            return dict(_loaded)
        applied, overridden = {}, []
        for key, value in data.items():
            env_name = FRIENDLY_KEYS.get(key, key if key.startswith("ZWCAD_MCP_") else None)
            if env_name is None or value is None:
                continue
            if env_name in os.environ:
                overridden.append(env_name)
                continue
            os.environ[env_name] = _as_env_value(value)
            applied[env_name] = os.environ[env_name]
        _loaded.update(path=str(path), applied=applied, overridden_by_env=overridden)
        return dict(_loaded)
    return dict(_loaded)


def settings_report() -> dict:
    return dict(_loaded)
