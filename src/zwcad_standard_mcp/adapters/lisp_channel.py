"""AutoLISP channel: run LISP inside ZWCAD from the COM server (FORK_DESIGN §5.1).

Why: AutoLISP runs in the CAD process, so ``ssget`` / ``entget`` / ``vlax-curve-*`` work on thousands of
entities in milliseconds, where per-entity COM calls take ~17 ms each. The channel replaces the second
(dalingo81) server, which typed a command into the ZWCAD window and decoded its result files with the wrong
codepage (FINDINGS #2, #21, #24).

Protocol (one request):
1. Python writes ``req_<id>.lsp`` (one LISP expression) into the IPC folder, encoded in the ANSI codepage that
   ZWCAD's LISP file functions use (cp1250 on Polish Windows).
2. Python sends ``(progn <bootstrap> (zwmcp:run "<dir>/" "<id>" <undo>) (princ))`` with COM
   ``Document.SendCommand``. The bootstrap reads the bundled LISP library (shipped in this package,
   comment-free copy in the IPC folder) with ``read`` + ``eval`` — no ``load``, so SECURELOAD and trusted
   paths do not matter — and only when the document does not have this bundle version yet (LISP namespaces
   are per document, FINDINGS #24).
3. ``zwmcp:run`` evaluates the request inside ``vl-catch-all-apply`` (optionally inside one undo mark) and
   writes ``res_<id>.json`` (tmp file + rename). Strings are JSON-escaped in LISP; characters above 126 are
   written as \\uXXXX when ZWCAD's LISP is Unicode, otherwise raw in the ANSI codepage.
4. Python polls for the result file and decodes it (UTF-8 if valid, else the ANSI codepage).

This module has no COM dependency: ``send`` is a callable, so the protocol is unit-tested with a fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zwcad_standard_mcp.errors import LispChannelError

LISP_SOURCE_DIR = Path(__file__).resolve().parent.parent / "lisp"
BUNDLE_FILES = ("zwmcp_core.lsp", "mo_tools.lsp")
FORM_SEPARATOR = "#FORM"


def default_ipc_dir() -> Path:
    override = os.getenv("ZWCAD_MCP_IPC_DIR")
    if override:
        return Path(override)
    # Not under %LOCALAPPDATA%: Claude Desktop from the Microsoft Store (MSIX) virtualises AppData for the
    # server process, so ZWCAD would not see files written there (FINDINGS #41).
    return Path(os.path.expanduser("~")) / "zwcad-mcp" / "ipc"


def ansi_codepage() -> str:
    """Codepage used by ZWCAD's LISP file functions (system ANSI codepage); ZWCAD_MCP_CODEPAGE overrides."""
    override = os.getenv("ZWCAD_MCP_CODEPAGE")
    if override:
        return override
    if sys.platform == "win32":
        try:
            import ctypes

            acp = int(ctypes.windll.kernel32.GetACP())
            return "utf-8" if acp == 65001 else f"cp{acp}"
        except Exception:  # pragma: no cover - Windows only
            return "mbcs"
    return "utf-8"


# --------------------------------------------------------------------------- LISP source handling


def strip_lisp_comments(source: str) -> list[str]:
    """Split LISP source into top-level forms with comments removed.

    Handles ``;`` line comments, ``;| ... |;`` block comments and string literals with backslash escapes.
    Newlines are kept inside forms (``read`` accepts them); each returned item is one top-level form.
    """
    forms: list[str] = []
    current: list[str] = []
    depth = 0
    i, n = 0, len(source)
    in_string = False
    while i < n:
        ch = source[i]
        if in_string:
            current.append(ch)
            if ch == "\\" and i + 1 < n:
                current.append(source[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == ";":
            if source.startswith(";|", i):
                end = source.find("|;", i + 2)
                i = n if end < 0 else end + 2
                continue
            end = source.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        current.append(ch)
        i += 1
        if depth == 0 and ch == ")":
            text = "".join(current).strip()
            if text:
                forms.append(text)
            current = []
    tail = "".join(current).strip()
    if tail:
        forms.append(tail)
    if in_string:
        raise LispChannelError("Unterminated string literal in LISP source.")
    if depth != 0:
        raise LispChannelError(f"Unbalanced parentheses in LISP source (depth {depth} at end).")
    return forms


def _form_lines(forms: list[str]) -> str:
    out: list[str] = []
    for form in forms:
        out.append(FORM_SEPARATOR)
        out.extend(line.rstrip() for line in form.splitlines() if line.strip())
    return "\n".join(out) + "\n"


def lisp_string(value: str, codepage: str) -> str:
    """LISP expression producing ``value``; characters the codepage cannot hold become ``(chr N)``."""
    parts: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            parts.append('"' + "".join(buf) + '"')
            buf.clear()

    for ch in value:
        if ch == "\\":
            buf.append("\\\\")
        elif ch == '"':
            buf.append('\\"')
        elif ch == "\n":
            buf.append("\\n")
        elif ch == "\r":
            buf.append("\\r")
        elif ch == "\t":
            buf.append("\\t")
        elif ord(ch) < 32:
            flush()
            parts.append(f"(chr {ord(ch)})")
        else:
            try:
                ch.encode(codepage)
                buf.append(ch)
            except (UnicodeEncodeError, LookupError):
                flush()
                parts.append(f"(chr {ord(ch)})")
    flush()
    if not parts:
        return '""'
    return parts[0] if len(parts) == 1 else "(strcat " + " ".join(parts) + ")"


def to_lisp(value: Any, codepage: str) -> str:
    """Python value → LISP expression. dict → assoc list of (key . value); list → (list ...)."""
    if value is None or value is False:
        return "nil"
    if value is True:
        return "T"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(float(value))
        if "e" in text or "E" in text:
            text = f"{value:.12f}"
        return text if "." in text else text + ".0"
    if isinstance(value, str):
        return lisp_string(value, codepage)
    if isinstance(value, dict):
        items = " ".join(f"(cons {lisp_string(str(k), codepage)} {to_lisp(v, codepage)})" for k, v in value.items())
        return f"(list {items})" if items else "nil"
    if isinstance(value, (list, tuple)):
        items = " ".join(to_lisp(v, codepage) for v in value)
        return f"(list {items})" if items else "nil"
    raise LispChannelError(f"Cannot pass {type(value).__name__} to LISP.")


def lisp_call(function: str, *args: Any, codepage: str = "utf-8") -> str:
    return "(" + " ".join([function, *(to_lisp(a, codepage) for a in args)]) + ")"


def _lisp_path(path: Path | str) -> str:
    return str(path).replace("\\", "/")


# --------------------------------------------------------------------------- channel


class LispChannel:
    def __init__(
        self,
        ipc_dir: Path | str | None = None,
        codepage: str | None = None,
        source_dir: Path | None = None,
        poll_s: float = 0.05,
        terminator: str | None = None,
    ) -> None:
        self.ipc_dir = Path(ipc_dir) if ipc_dir else default_ipc_dir()
        self.codepage = codepage or ansi_codepage()
        self.source_dir = source_dir or LISP_SOURCE_DIR
        self.poll_s = poll_s
        # Text that ends the command line for SendCommand (Enter). Overridable for odd builds.
        self.terminator = terminator if terminator is not None else os.getenv("ZWCAD_MCP_LISP_TERMINATOR", "\n")
        self._bundle_version: str | None = None
        self._bundle_path: Path | None = None
        self.last_unicode: bool | None = None

    # -- bundle -------------------------------------------------------------------------

    def bundle(self) -> tuple[Path, str]:
        """Write the comment-free LISP library into the IPC folder (once per content version)."""
        sources = []
        for name in BUNDLE_FILES:
            path = self.source_dir / name
            if not path.exists():
                raise LispChannelError(f"LISP library file missing: {path}")
            sources.append(path.read_text(encoding="utf-8"))
        digest = hashlib.sha1("\n".join(sources).encode("utf-8")).hexdigest()[:12]
        if self._bundle_version == digest and self._bundle_path and self._bundle_path.exists():
            return self._bundle_path, digest
        forms: list[str] = []
        for text in sources:
            forms.extend(strip_lisp_comments(text))
        forms.append(f'(setq zwmcp:*version* "{digest}")')
        self.ipc_dir.mkdir(parents=True, exist_ok=True)
        path = self.ipc_dir / f"zwmcp_bundle_{digest}.lsp"
        try:
            content = _form_lines(forms).encode(self.codepage)
        except UnicodeEncodeError as exc:
            raise LispChannelError(f"LISP library is not encodable in {self.codepage}: {exc}") from exc
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        for old in self.ipc_dir.glob("zwmcp_bundle_*.lsp"):
            if old != path:
                try:
                    old.unlink()
                except OSError:
                    pass
        self._bundle_version, self._bundle_path = digest, path
        return path, digest

    def command_line(self, request_id: str, undo: bool) -> str:
        """The single expression sent with SendCommand."""
        bundle, version = self.bundle()
        boot = (
            f'(if (/= zwmcp:*version* "{version}") '
            "((lambda (/ zf zl zs zr) "
            f'(if (setq zf (open "{_lisp_path(bundle)}" "r")) (progn (setq zs "") '
            f'(while (setq zl (read-line zf)) (if (= (vl-string-right-trim "\\r " zl) "{FORM_SEPARATOR}") '
            '(progn (if (/= zs "") (setq zr (cons zs zr))) (setq zs "")) (setq zs (strcat zs zl "\\n")))) '
            '(close zf) (if (/= zs "") (setq zr (cons zs zr))) '
            "(foreach zs (reverse zr) (eval (read zs))))))))"
        )
        run = f'(zwmcp:run "{_lisp_path(self.ipc_dir)}/" "{request_id}" {"T" if undo else "nil"})'
        return f"(progn {boot} {run} (princ)){self.terminator}"

    # -- request ------------------------------------------------------------------------

    def write_request(self, expression: str) -> str:
        request_id = uuid.uuid4().hex[:12]
        self.ipc_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_stale()
        try:
            data = expression.encode(self.codepage)
        except UnicodeEncodeError as exc:
            raise LispChannelError(
                f"LISP code contains characters outside {self.codepage}; build them with (chr N): {exc}"
            ) from exc
        path = self.ipc_dir / f"req_{request_id}.lsp"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return request_id

    def result_path(self, request_id: str) -> Path:
        return self.ipc_dir / f"res_{request_id}.json"

    def decode(self, raw: bytes) -> dict:
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode(self.codepage, errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LispChannelError(f"LISP result is not valid JSON ({exc}): {text[:300]!r}") from exc
        if isinstance(data, dict) and "unicode" in data:
            self.last_unicode = bool(data["unicode"])
        return data

    def ack_path(self, request_id: str) -> Path:
        return self.ipc_dir / f"ack_{request_id}.txt"

    def wait_result(self, request_id: str, timeout_s: float, ack_timeout_s: float = 10.0) -> dict:
        """Poll for the result. Fails after ``ack_timeout_s`` when the request never started."""
        path = self.result_path(request_id)
        started = time.monotonic()
        deadline = started + timeout_s
        acknowledged = False
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    raw = path.read_bytes()
                except OSError:
                    time.sleep(self.poll_s)
                    continue
                try:
                    return self.decode(raw)
                finally:
                    self._remove(request_id)
            if not acknowledged:
                acknowledged = self.ack_path(request_id).exists()
                if not acknowledged and time.monotonic() - started > ack_timeout_s:
                    self._remove(request_id)
                    raise LispChannelError(
                        f"The LISP request did not start within {ack_timeout_s:.0f} s (request {request_id}). "
                        "ZWCAD did not run the command line sent with SendCommand: check the ZWCAD command line "
                        "for an error, a running command or a prompt."
                    )
            time.sleep(self.poll_s)
        raise LispChannelError(
            f"No LISP result after {timeout_s:.0f} s (request {request_id}); the LISP code is still running "
            "or stopped without writing a result. Check the ZWCAD command line."
        )

    def _remove(self, request_id: str) -> None:
        for path in (self.ipc_dir / f"req_{request_id}.lsp", self.result_path(request_id), self.ack_path(request_id)):
            try:
                path.unlink()
            except OSError:
                pass

    def cleanup_stale(self, older_than_s: float = 3600.0) -> None:
        now = time.time()
        for pattern in ("req_*.lsp", "res_*.json", "ack_*.txt", "*.tmp"):
            for path in self.ipc_dir.glob(pattern):
                try:
                    if now - path.stat().st_mtime > older_than_s:
                        path.unlink()
                except OSError:
                    pass

    def run(
        self,
        send: Callable[[str], None],
        expression: str,
        undo: bool = False,
        timeout_s: float = 40.0,
        ack_timeout_s: float = 10.0,
    ) -> dict:
        """Evaluate ``expression`` in the active document; return the parsed result envelope.

        Envelope: ``{"id", "ok", "value" | "error", "unicode"}``.
        """
        request_id = self.write_request(expression)
        try:
            send(self.command_line(request_id, undo))
        except Exception:
            self._remove(request_id)
            raise
        return self.wait_result(request_id, timeout_s, ack_timeout_s)

    def call(self, send: Callable[[str], None], function: str, *args: Any, undo: bool = False,
             timeout_s: float = 40.0) -> Any:
        """Call a bundled LISP function with Python arguments; return its value or raise LispChannelError."""
        envelope = self.run(send, lisp_call(function, *args, codepage=self.codepage), undo=undo, timeout_s=timeout_s)
        if not envelope.get("ok"):
            raise LispChannelError(f"LISP error in {function}: {envelope.get('error')}")
        return envelope.get("value")
