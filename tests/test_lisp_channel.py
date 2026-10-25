from __future__ import annotations

import json
import re
import threading
import zlib
from pathlib import Path

import pytest

from zwcad_standard_mcp import archtools
from zwcad_standard_mcp.adapters.lisp_channel import (
    FORM_SEPARATOR,
    LispChannel,
    lisp_call,
    lisp_string,
    strip_lisp_comments,
    to_lisp,
)
from zwcad_standard_mcp.adapters.screenshot import encode_png, target_size
from zwcad_standard_mcp.errors import LispChannelError

LISP_DIR = Path(__file__).resolve().parents[1] / "src" / "zwcad_standard_mcp" / "lisp"


# --------------------------------------------------------------------------- source handling


def test_strip_comments_keeps_strings_and_splits_forms() -> None:
    src = '; header\n(setq a "x;y") ; tail\n;| block\n (no) |;\n(defun f (/ b)\n  (setq b "q\\"; r") ; c\n)\n'
    forms = strip_lisp_comments(src)
    assert forms == ['(setq a "x;y")', '(defun f (/ b)\n  (setq b "q\\"; r") \n)']


def test_strip_comments_reports_unbalanced() -> None:
    with pytest.raises(LispChannelError):
        strip_lisp_comments("(setq a (list 1 2)")


def test_bundled_lisp_files_are_ascii_and_balanced() -> None:
    for name in ("zwmcp_core.lsp", "mo_tools.lsp"):
        text = (LISP_DIR / name).read_text(encoding="utf-8")
        text.encode("ascii")
        forms = strip_lisp_comments(text)
        assert forms, name
        for form in forms:
            assert form.startswith("("), (name, form[:40])


def test_bundled_functions_present() -> None:
    forms = strip_lisp_comments((LISP_DIR / "zwmcp_core.lsp").read_text() + (LISP_DIR / "mo_tools.lsp").read_text())
    names = {m.group(1).lower() for f in forms if (m := re.match(r"\(defun (\S+)", f))}
    for required in ("zwmcp:run", "zwmcp:json", "zwmcp:count-by", "zwmcp:ping", "mo:area-fields",
                     "mo:sun-ruler", "mo:swept-path", "mo:round-path", "mosp:fixpl", "mosp:proj"):
        assert required in names, required


# --------------------------------------------------------------------------- Python -> LISP values


def test_lisp_string_escapes_and_chr_fallback() -> None:
    assert lisp_string('a"b\\c', "cp1250") == '"a\\"b\\\\c"'
    assert lisp_string("Pokój ść", "cp1250") == '"Pokój ść"'
    assert lisp_string("x中y", "cp1250") == '(strcat "x" (chr 20013) "y")'
    assert lisp_string("", "cp1250") == '""'


def test_to_lisp_structures() -> None:
    assert to_lisp({"a": 1, "b": [1.5, 2], "c": None, "d": True}, "cp1250") == (
        '(list (cons "a" 1) (cons "b" (list 1.5 2)) (cons "c" nil) (cons "d" T))'
    )
    assert to_lisp([], "cp1250") == "nil"
    assert lisp_call("mo:x", "1A", {"k": 2.0}) == '(mo:x "1A" (list (cons "k" 2.0)))'


# --------------------------------------------------------------------------- protocol with a fake ZWCAD


class FakeLisp:
    """Simulates ZWCAD: parses the SendCommand line, reads the request, writes ack + result (cp1250)."""

    def __init__(self, channel: LispChannel, value_for, start: bool = True, encoding: str = "cp1250") -> None:
        self.channel = channel
        self.value_for = value_for
        self.start = start
        self.encoding = encoding
        self.commands: list[str] = []

    def __call__(self, command: str) -> None:
        self.commands.append(command)
        if not self.start:
            return
        match = re.search(r'\(zwmcp:run "([^"]+)" "([0-9a-f]+)" (T|nil)\)', command)
        assert match, command
        folder, rid, undo = Path(match.group(1)), match.group(2), match.group(3)
        expression = (folder / f"req_{rid}.lsp").read_bytes().decode(self.encoding)
        (folder / f"ack_{rid}.txt").write_text("1")

        def finish() -> None:
            body = {"id": rid, "unicode": False, "ok": True, "value": self.value_for(expression, undo == "T")}
            (folder / f"res_{rid}.json").write_bytes(json.dumps(body, ensure_ascii=False).encode(self.encoding))

        threading.Timer(0.05, finish).start()


def test_run_roundtrip_with_polish_text(tmp_path: Path) -> None:
    channel = LispChannel(ipc_dir=tmp_path, codepage="cp1250", poll_s=0.01)
    fake = FakeLisp(channel, lambda expr, undo: {"echo": expr, "undo": undo, "layer": "ściana zewnętrzna"})
    envelope = channel.run(fake, '(mo:x "Pokój")', undo=True, timeout_s=5)
    assert envelope["ok"] and envelope["value"]["layer"] == "ściana zewnętrzna"
    assert envelope["value"]["echo"] == '(mo:x "Pokój")' and envelope["value"]["undo"] is True
    command = fake.commands[0]
    assert command.endswith("\n") and command.count("(") == command.count(")")
    assert "zwmcp:*version*" in command and "read-line" in command
    assert not list(tmp_path.glob("req_*")) and not list(tmp_path.glob("res_*")) and not list(tmp_path.glob("ack_*"))


def test_bundle_written_once_with_form_separators(tmp_path: Path) -> None:
    channel = LispChannel(ipc_dir=tmp_path, codepage="cp1250")
    path, version = channel.bundle()
    text = path.read_text(encoding="cp1250")
    assert text.startswith(FORM_SEPARATOR + "\n") and f'(setq zwmcp:*version* "{version}")' in text
    assert ";;" not in text
    assert channel.bundle() == (path, version)
    assert f'zwmcp:*version* "{version}"' in channel.command_line("abc", False)


def test_request_that_never_starts_fails_fast(tmp_path: Path) -> None:
    channel = LispChannel(ipc_dir=tmp_path, codepage="cp1250", poll_s=0.01)
    fake = FakeLisp(channel, lambda e, u: None, start=False)
    with pytest.raises(LispChannelError) as info:
        channel.run(fake, "(+ 1 2)", timeout_s=5, ack_timeout_s=0.2)
    assert "did not start" in str(info.value)
    assert not list(tmp_path.glob("req_*"))


def test_decode_reports_invalid_json(tmp_path: Path) -> None:
    channel = LispChannel(ipc_dir=tmp_path, codepage="cp1250")
    with pytest.raises(LispChannelError):
        channel.decode(b'{"ok": true, "value": "a\\x"}')
    assert channel.decode('{"ok":true,"unicode":false,"value":"żółw"}'.encode("cp1250"))["value"] == "żółw"
    assert channel.decode('{"ok":true,"value":"\\u017c"}'.encode())["value"] == "ż"


def test_non_encodable_request_is_rejected(tmp_path: Path) -> None:
    channel = LispChannel(ipc_dir=tmp_path, codepage="cp1250")
    with pytest.raises(LispChannelError):
        channel.write_request('(princ "中")')


# --------------------------------------------------------------------------- tool maths


def test_sun_direction_and_time_are_inverse() -> None:
    for eq in ("W", "J"):
        for hour in (7.5, 9.0, 12.0, 15.25, 16.5):
            angle, length = archtools.shadow(hour, 51.0, 17.0, eq, 10.0)
            assert length > 0
            assert archtools.time_for_direction(angle, 51.0, 17.0, eq) == pytest.approx(hour, abs=1e-6)


def test_sun_noon_altitude_and_window() -> None:
    eq = archtools.EQUINOX["W"]
    noon = 12.0 - ((17.0 - 15.0 * eq["tz"]) / 15.0 + eq["eot"])
    az, alt = archtools.sun_position(noon, 51.0, 17.0, "W")
    assert alt == pytest.approx(39.0, abs=0.01)
    assert abs(az - 180.0) < 0.01
    window = archtools.exposure_window((0, 0), 51.0, 17.0, "W", start_time=8.0, end_time=12.5)
    assert window == {"start": "8:00", "end": "12:30", "duration_min": 270, "duration": "4:30"}
    assert archtools.exposure_window((0, 0), 51.0, 17.0, "W")["duration"] == "10:00"


def test_vehicle_min_radius_matches_cayenne_header() -> None:
    veh = archtools.vehicle()
    assert round(archtools.min_path_radius_mm(veh, reverse=False) / 10.0, 1) == 532.9
    assert round(archtools.min_path_radius_mm(veh, reverse=True) / 10.0, 1) == 447.4
    with pytest.raises(ValueError):
        archtools.vehicle({"XX": 1})


def test_area_field_format() -> None:
    assert archtools.area_field_format(5) == "%lu2%pr1%ct8[.0001]"
    assert archtools.area_field_format(4, 2, ",") == "%lu2%pr2%ds44%ct8[.000001]"
    assert archtools.area_field_format(6, 0) == "%lu2%pr0%ct8[1]"


# --------------------------------------------------------------------------- screenshot encoding


def test_png_encoding_roundtrip() -> None:
    width, height = 3, 2
    bgrx = bytes([0, 0, 255, 0] * width * height)  # red pixels
    png = encode_png(width, height, bgrx)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    idat = png[png.index(b"IDAT") + 4: png.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert raw[:1 + 3 * width] == b"\x00" + bytes([255, 0, 0] * width)
    assert target_size(2560, 1440, 1280) == (1280, 720)
    assert target_size(800, 600, 1280) == (800, 600)
