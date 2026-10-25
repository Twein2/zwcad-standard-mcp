"""Register this server in Claude Desktop's config file (Windows).

Usage (from the repository folder, after install.bat):

    .venv\\Scripts\\python.exe -m zwcad_standard_mcp.configure [--name zwcad-standard]
        [--confirm-mode strict|off] [--enable-lisp | --disable-lisp] [--config PATH] [--dry-run]

The existing config is backed up next to the original (claude_desktop_config.json.bak-YYYYMMDD-HHMMSS)
and only the entry for this server is added or replaced; other servers are left unchanged. Settings that are
not given on the command line (confirm mode, execute_lisp switch, extra env variables) keep their previous
values; a new install starts with confirm mode strict and execute_lisp off. Confirm mode and the execute_lisp
switch are stored in %USERPROFILE%\\zwcad-mcp\\settings.json (read by the server at start-up), because Claude
Desktop rewrites its own config file while it runs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from zwcad_standard_mcp.settings_file import FRIENDLY_KEYS, user_settings_path


def candidate_config_paths() -> list[Path]:
    """Claude Desktop config locations, Microsoft Store (MSIX) copies first.

    The Store version reads its virtualised copy (Packages\\Claude_*\\LocalCache\\Roaming\\Claude) and ignores
    %APPDATA%\\Claude when that copy exists (FINDINGS #43).
    """
    paths: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        packages = Path(local) / "Packages"
        if packages.is_dir():
            for package in sorted(packages.glob("Claude_*")):
                paths.append(package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    return paths


def find_config_paths() -> list[Path]:
    """Every existing config file (all are updated); else the first location whose folder exists."""
    candidates = candidate_config_paths()
    existing = [path for path in candidates if path.is_file()]
    if existing:
        return existing
    for path in candidates:
        if path.parent.is_dir():
            return [path]
    if not candidates:
        raise SystemExit("APPDATA is not set; pass --config with the path to claude_desktop_config.json.")
    return [candidates[0]]


def find_config_path() -> Path:
    return find_config_paths()[0]


MANAGED_ENV = {FRIENDLY_KEYS["confirm_mode"], FRIENDLY_KEYS["execute_lisp"]}


def build_entry(python_exe: Path, previous_env: dict | None = None) -> dict:
    """Client entry; confirm mode and execute_lisp live in the settings file, not in the client env."""
    env = {k: v for k, v in (previous_env or {}).items() if k not in MANAGED_ENV}
    env.setdefault("ZWCAD_MCP_PROG_ID", "ZWCAD.Application")
    env["ZWCAD_MCP_TRANSPORT"] = "stdio"
    return {"command": str(python_exe), "args": ["-m", "zwcad_standard_mcp.server"], "env": env}


def write_settings(path: Path, confirm_mode: str | None, enable_lisp: bool | None, dry_run: bool,
                   legacy_env: dict) -> dict:
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except json.JSONDecodeError:
            data = {}
    # first run: take over values that older versions kept in the client env
    if "confirm_mode" not in data and legacy_env.get(FRIENDLY_KEYS["confirm_mode"]):
        data["confirm_mode"] = legacy_env[FRIENDLY_KEYS["confirm_mode"]]
    if "execute_lisp" not in data and FRIENDLY_KEYS["execute_lisp"] in legacy_env:
        data["execute_lisp"] = str(legacy_env[FRIENDLY_KEYS["execute_lisp"]]).lower() in {"1", "true", "yes", "on"}
    data.setdefault("confirm_mode", "strict")
    data.setdefault("execute_lisp", False)
    if confirm_mode:
        data["confirm_mode"] = confirm_mode
    if enable_lisp is not None:
        data["execute_lisp"] = enable_lisp
    rendered = json.dumps(data, indent=2)
    if dry_run:
        print(f"Settings:    {path}\n{rendered}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        print(f"Settings:    {path}")
        print(f"Confirm:     {data['confirm_mode']}")
        print(f"execute_lisp: {'on' if data['execute_lisp'] else 'off'}")
    return data


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
    parser.add_argument("--settings", type=Path, help="Settings file (default: %%USERPROFILE%%\\zwcad-mcp\\settings.json)")
    parser.add_argument("--dry-run", action="store_true", help="Print the result without writing")
    args = parser.parse_args(argv)

    python_exe = Path(sys.executable).absolute()
    paths = [args.config] if args.config else find_config_paths()
    status = 0
    legacy_env: dict = {}
    for config_path in paths:
        code, env = configure_file(config_path, args, python_exe)
        status |= code
        legacy_env = legacy_env or env
    write_settings(args.settings or user_settings_path(), args.confirm_mode, args.enable_lisp, args.dry_run, legacy_env)
    if status == 0 and not args.dry_run:
        print("Restart Claude Desktop (quit it from the system tray, then start it again).")
        print("Note: Claude Desktop rewrites its config while it runs; if the server entry disappears,")
        print("quit Claude Desktop first and run this command again. Settings above are kept in their own file.")
    return status


def configure_file(config_path: Path, args: argparse.Namespace, python_exe: Path) -> tuple[int, dict]:
    config: dict = {}
    if config_path.is_file():
        text = config_path.read_text(encoding="utf-8-sig").strip()
        if text:
            try:
                config = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"[ERROR] {config_path} is not valid JSON ({exc}). Fix or remove it, then run again.")
                return 1, {}
    servers = config.setdefault("mcpServers", {})
    previous = servers.get(args.name)
    previous_env = (previous or {}).get("env") or {}
    entry = build_entry(python_exe, previous_env)
    if previous == entry:
        print(f"Config file: {config_path} (server entry unchanged)")
        return 0, previous_env
    servers[args.name] = entry
    rendered = json.dumps(config, indent=2, ensure_ascii=False)
    if args.dry_run:
        print(f"Config file: {config_path}\n{rendered}")
        return 0, previous_env
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.is_file():
        backup = config_path.with_name(f"{config_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config_path, backup)
        print(f"Backup:      {backup}")
    config_path.write_text(rendered + "\n", encoding="utf-8")
    print(f"Config file: {config_path}")
    print(f"Server:      {args.name} ({'updated' if previous else 'added'})")
    print(f"Python:      {python_exe}")
    return 0, previous_env


if __name__ == "__main__":
    raise SystemExit(main())
