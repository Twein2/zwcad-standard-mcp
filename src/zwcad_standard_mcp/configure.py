"""Register this server in Claude Desktop's config file (Windows).

Usage (from the repository folder, after install.bat):

    .venv\\Scripts\\python.exe -m zwcad_standard_mcp.configure [--name zwcad-standard]
        [--confirm-mode strict|off] [--enable-lisp | --disable-lisp] [--config PATH] [--dry-run]

The existing config is backed up next to the original (claude_desktop_config.json.bak-YYYYMMDD-HHMMSS)
and only the entry for this server is added or replaced; other servers are left unchanged. Settings that are
not given on the command line (confirm mode, execute_lisp switch, extra env variables) keep their previous
values; a new entry starts with confirm mode strict and execute_lisp off.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def candidate_config_paths() -> list[Path]:
    paths: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # Microsoft Store (MSIX) installs keep a virtualised copy of %APPDATA%.
        packages = Path(local) / "Packages"
        if packages.is_dir():
            for package in sorted(packages.glob("Claude_*")):
                paths.append(package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    return paths


def find_config_path() -> Path:
    candidates = candidate_config_paths()
    for path in candidates:
        if path.is_file():
            return path
    for path in candidates:
        if path.parent.is_dir():
            return path
    if not candidates:
        raise SystemExit("APPDATA is not set; pass --config with the path to claude_desktop_config.json.")
    return candidates[0]


def build_entry(python_exe: Path, confirm_mode: str, enable_lisp: bool, previous_env: dict | None = None) -> dict:
    env = dict(previous_env or {})
    env.update(
        {
            "ZWCAD_MCP_PROG_ID": env.get("ZWCAD_MCP_PROG_ID", "ZWCAD.Application"),
            "ZWCAD_MCP_TRANSPORT": "stdio",
            "ZWCAD_MCP_CONFIRM_MODE": confirm_mode,
            "ZWCAD_MCP_ENABLE_LISP": "1" if enable_lisp else "0",
        }
    )
    return {"command": str(python_exe), "args": ["-m", "zwcad_standard_mcp.server"], "env": env}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="zwcad-standard", help="Server name shown in Claude (default: zwcad-standard)")
    parser.add_argument("--confirm-mode", choices=["strict", "off"], default=None,
                        help="Default: keep the current value (strict for a new entry)")
    lisp = parser.add_mutually_exclusive_group()
    lisp.add_argument("--enable-lisp", dest="enable_lisp", action="store_true", default=None,
                      help="Allow the execute_lisp tool (arbitrary AutoLISP, still behind confirm)")
    lisp.add_argument("--disable-lisp", dest="enable_lisp", action="store_false")
    parser.add_argument("--config", type=Path, help="Path to claude_desktop_config.json (auto-detected by default)")
    parser.add_argument("--dry-run", action="store_true", help="Print the result without writing")
    args = parser.parse_args(argv)

    python_exe = Path(sys.executable).absolute()
    config_path = args.config or find_config_path()
    config: dict = {}
    if config_path.is_file():
        text = config_path.read_text(encoding="utf-8-sig").strip()
        if text:
            try:
                config = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"[ERROR] {config_path} is not valid JSON ({exc}). Fix or remove it, then run again.")
                return 1
    servers = config.setdefault("mcpServers", {})
    previous = servers.get(args.name)
    previous_env = (previous or {}).get("env") or {}
    confirm_mode = args.confirm_mode or previous_env.get("ZWCAD_MCP_CONFIRM_MODE") or "strict"
    if args.enable_lisp is None:
        enable_lisp = str(previous_env.get("ZWCAD_MCP_ENABLE_LISP", "0")).strip().lower() in {"1", "true", "yes", "on"}
    else:
        enable_lisp = args.enable_lisp
    servers[args.name] = build_entry(python_exe, confirm_mode, enable_lisp, previous_env)
    rendered = json.dumps(config, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(f"Config file: {config_path}\n{rendered}")
        return 0
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.is_file():
        backup = config_path.with_name(f"{config_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config_path, backup)
        print(f"Backup:      {backup}")
    config_path.write_text(rendered + "\n", encoding="utf-8")
    print(f"Config file: {config_path}")
    print(f"Server:      {args.name} ({'updated' if previous else 'added'})")
    print(f"Python:      {python_exe}")
    print(f"Confirm:     {confirm_mode}")
    print(f"execute_lisp: {'on' if enable_lisp else 'off'}")
    print("Restart Claude Desktop (quit it from the system tray, then start it again).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
