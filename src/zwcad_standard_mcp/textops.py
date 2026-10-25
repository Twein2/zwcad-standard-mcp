"""Pure text transformations used by update_text_entities (no COM imports)."""

from __future__ import annotations

import re

# Placeholders for escaped characters that must survive format stripping.
_ESC = {"\\\\": "\x00B", "\\{": "\x00L", "\\}": "\x00R"}

_STACK = re.compile(r"\\S([^;]*?)[\^/#]([^;]*);")
_WITH_ARG = re.compile(r"\\[ACcFfHhQTWp][^;\\]*;")
_TOGGLES = re.compile(r"\\[LlOoKkX]")


def strip_mtext_formatting(text: str) -> str:
    """Remove MText inline formatting codes, keeping paragraph breaks (\\P) and escaped characters.

    Removed: font (\\f), height (\\H), colour (\\C, \\c), tracking (\\T), oblique (\\Q), width (\\W),
    alignment (\\A), paragraph settings (\\p), underline/overline/strike toggles and grouping braces.
    Stacked fractions (\\S1^2;) become 1/2 and non-breaking spaces (\\~) become spaces.
    """
    out = text
    for raw, placeholder in _ESC.items():
        out = out.replace(raw, placeholder)
    out = _STACK.sub(lambda m: f"{m.group(1)}/{m.group(2)}", out)
    out = _WITH_ARG.sub("", out)
    out = _TOGGLES.sub("", out)
    out = out.replace("\\~", " ")
    out = out.replace("{", "").replace("}", "")
    for raw, placeholder in _ESC.items():
        out = out.replace(placeholder, raw)
    return out


_LEADING_HEIGHT = re.compile(r"^\{?(?:\\[fF][^;]*;)?\\H([0-9]*\.?[0-9]+);")


def mtext_leading_height(text: str) -> float | None:
    """Absolute height set by a leading \\H code (e.g. "\\fArial;\\H2.5;text" → 2.5).

    Relative heights (\\H0.8x;) and heights set later in the text return None.
    """
    match = _LEADING_HEIGHT.match(text)
    return float(match.group(1)) if match else None


def repair_mojibake(text: str, wrong_codepage: str = "cp1250") -> str:
    """Undo UTF-8 bytes that were decoded with an ANSI codepage (e.g. "Ĺ‚" → "ł").

    Returns the input unchanged when it is plain ASCII, cannot be re-encoded, or the bytes are not
    valid UTF-8 — so correctly encoded Polish text is never altered.
    """
    if text.isascii():
        return text
    try:
        repaired = text.encode(wrong_codepage).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired


def replace_text(text: str, find: str, replacement: str, *, regex: bool = False, case_sensitive: bool = True) -> str:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = find if regex else re.escape(find)
    if not regex:
        replacement = replacement.replace("\\", "\\\\")
    return re.sub(pattern, replacement, text, flags=flags)
