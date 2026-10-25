"""Detection of modal dialogs owned by the ZWCAD process (Windows only, no COM).

COM calls such as Documents.Open or Plot.PlotToFile block while ZWCAD shows a modal
dialog (proxy object notice, unresolved references, licence messages, missing fonts).
This module lets the server report such dialogs instead of hanging silently.
"""

from __future__ import annotations

import sys
from typing import Any

# Window class of standard Win32 dialog boxes.
_DIALOG_CLASS = "#32770"


def _win32() -> tuple[Any, Any] | None:
    if sys.platform != "win32":
        return None
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        return None
    return win32gui, win32process


def _is_push_button(win32gui: Any, hwnd: int) -> bool:
    """True for visible push buttons only (not checkboxes, radio buttons or group boxes)."""
    if win32gui.GetClassName(hwnd) != "Button" or not win32gui.IsWindowVisible(hwnd):
        return False
    style = win32gui.GetWindowLong(hwnd, -16)  # GWL_STYLE
    return (style & 0x0F) in (0x00, 0x01)  # BS_PUSHBUTTON, BS_DEFPUSHBUTTON


def find_process_dialogs(main_hwnd: int | None = None, pid: int | None = None) -> list[dict]:
    """List visible top-level dialog windows that belong to the ZWCAD process.

    ``main_hwnd`` is ``Application.HWND``; the process id is derived from it when ``pid``
    is not given. Returns ``[{"hwnd", "title", "class", "buttons"}]``.
    """
    mods = _win32()
    if mods is None:
        return []
    win32gui, win32process = mods
    if pid is None:
        if not main_hwnd:
            return []
        try:
            _, pid = win32process.GetWindowThreadProcessId(int(main_hwnd))
        except Exception:
            return []

    found: list[dict] = []

    def _children_text(hwnd: int) -> list[str]:
        texts: list[str] = []

        def _child(child: int, _: Any) -> bool:
            try:
                if _is_push_button(win32gui, child):
                    text = win32gui.GetWindowText(child)
                    if text and text not in texts:
                        texts.append(text)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(hwnd, _child, None)
        except Exception:
            pass
        return texts

    def _top(hwnd: int, _: Any) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
            if owner_pid != pid or hwnd == main_hwnd:
                return True
            cls = win32gui.GetClassName(hwnd)
            if cls != _DIALOG_CLASS:
                return True
            found.append(
                {
                    "hwnd": int(hwnd),
                    "title": win32gui.GetWindowText(hwnd),
                    "class": cls,
                    "buttons": _children_text(hwnd),
                }
            )
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_top, None)
    except Exception:
        return found
    return found


# Dialogs that may be answered automatically, with the button that keeps the drawing
# unchanged and lets the operation continue. Matching is case-insensitive on a title
# fragment; the button is matched as a prefix (ignoring "&" accelerators).
# Licence, recovery, save and any unknown dialogs are never answered automatically.
SAFE_DIALOG_ANSWERS: list[tuple[str, tuple[str, ...]]] = [
    ("unresolved reference", ("Ignore unresolved references", "Continue")),
    ("proxy information", ("OK", "Close")),
    ("missing shx", ("Ignore", "Skip")),
    ("specify font", ("Cancel",)),
    # Dense hatches on open: keep the drawing as is (does not change HPMAXLINES).
    ("too many hatch lines", ("Keep degenerated display",)),
]


def _clean(text: str) -> str:
    return text.replace("&", "").strip().lower()


def safe_answer_for(dialog: dict) -> str | None:
    """Return the button text to press for a known-safe dialog, else None."""
    title = _clean(str(dialog.get("title", "")))
    buttons = [str(b) for b in dialog.get("buttons", [])]
    for fragment, wanted in SAFE_DIALOG_ANSWERS:
        if fragment in title:
            for want in wanted:
                for button in buttons:
                    if _clean(button).startswith(want.lower()):
                        return button
    return None


def click_dialog_button(hwnd: int, button_text: str) -> bool:
    """Press a button of a dialog window by its caption (BM_CLICK). Windows only."""
    mods = _win32()
    if mods is None:
        return False
    win32gui, _ = mods
    target: list[int] = []

    def _child(child: int, _: Any) -> bool:
        try:
            if _is_push_button(win32gui, child) and _clean(win32gui.GetWindowText(child)) == _clean(button_text):
                target.append(child)
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(int(hwnd), _child, None)
    except Exception:
        pass  # EnumChildWindows raises when the callback stops enumeration early
    if not target:
        return False
    try:
        import win32con  # type: ignore

        win32gui.PostMessage(target[0], win32con.BM_CLICK, 0, 0)
        return True
    except Exception:
        return False


class DialogWatchdog:
    """Background poller used around blocking COM calls (Documents.Open, PlotToFile).

    It records every dialog seen and, when ``auto_dismiss`` is true, answers dialogs
    listed in SAFE_DIALOG_ANSWERS after they stayed open for ``grace_seconds``.
    """

    def __init__(self, main_hwnd: int | None, auto_dismiss: bool = True,
                 interval: float = 0.5, grace_seconds: float = 1.0) -> None:
        import threading

        self.main_hwnd = main_hwnd
        self.auto_dismiss = auto_dismiss
        self.interval = interval
        self.grace_seconds = grace_seconds
        self.seen: dict[int, dict] = {}
        self.answered: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="zwcad-dialog-watchdog", daemon=True)

    def _run(self) -> None:
        import time

        first_seen: dict[int, float] = {}
        while not self._stop.wait(self.interval):
            for dialog in find_process_dialogs(self.main_hwnd):
                hwnd = dialog["hwnd"]
                self.seen.setdefault(hwnd, dialog)
                first_seen.setdefault(hwnd, time.monotonic())
                if not self.auto_dismiss or any(a["hwnd"] == hwnd for a in self.answered):
                    continue
                if time.monotonic() - first_seen[hwnd] < self.grace_seconds:
                    continue
                button = safe_answer_for(dialog)
                if button and click_dialog_button(hwnd, button):
                    self.answered.append({"hwnd": hwnd, "title": dialog.get("title"), "button": button})

    def __enter__(self) -> "DialogWatchdog":
        if self.main_hwnd:
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def report(self) -> dict:
        return {
            "dialogs_seen": [
                {"title": d.get("title"), "buttons": d.get("buttons")} for d in self.seen.values()
            ],
            "dialogs_answered": [{"title": a["title"], "button": a["button"]} for a in self.answered],
        }
