from __future__ import annotations

import json
import os
from pathlib import Path

from zwcad_standard_mcp import settings_file


def test_settings_file_fills_missing_env_only(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"execute_lisp": True, "confirm_mode": "off", "ZWCAD_MCP_CODEPAGE": "cp1250"}))
    saved = {k: os.environ.get(k) for k in ("ZWCAD_MCP_SETTINGS", "ZWCAD_MCP_ENABLE_LISP", "ZWCAD_MCP_CONFIRM_MODE", "ZWCAD_MCP_CODEPAGE")}
    try:
        os.environ["ZWCAD_MCP_SETTINGS"] = str(path)
        os.environ.pop("ZWCAD_MCP_ENABLE_LISP", None)
        os.environ.pop("ZWCAD_MCP_CODEPAGE", None)
        os.environ["ZWCAD_MCP_CONFIRM_MODE"] = "strict"
        report = settings_file.apply_settings_file()
        assert report["path"] == str(path)
        assert os.environ["ZWCAD_MCP_ENABLE_LISP"] == "1" and os.environ["ZWCAD_MCP_CODEPAGE"] == "cp1250"
        assert os.environ["ZWCAD_MCP_CONFIRM_MODE"] == "strict" and report["overridden_by_env"] == ["ZWCAD_MCP_CONFIRM_MODE"]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
