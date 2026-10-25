"""Regression tests for FINDINGS #49-#52 (found while planning a vehicle route, 2026-10-25)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from zwcad_standard_mcp.adapters.com import ComCadAdapter
from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.adapters.lisp_channel import BUNDLE_FILES, LispChannel, LispChannelError
from zwcad_standard_mcp.models import EntityCreateSpec, EntityQuerySpec, SweptPathRequest

LISP_DIR = Path(__file__).resolve().parents[1] / "src" / "zwcad_standard_mcp" / "lisp"


# #49 -------------------------------------------------------------------------------------------

def test_query_spec_rejects_unknown_keys() -> None:
    with pytest.raises(PydanticValidationError):
        EntityQuerySpec.model_validate({"layerz": ["A"]})


def test_query_spec_accepts_layer_and_type_lists_with_wildcards() -> None:
    adapter = FakeCadAdapter()
    layers = {e["layer"] for e in adapter.entities.values()}
    some = sorted(layers)[0]
    spec = EntityQuerySpec(layers=[some[:1] + "*"], scope="all_layouts")
    found = adapter.query_entities(spec)
    assert found and all(e["layer"].lower().startswith(some[:1].lower()) for e in found)
    assert EntityQuerySpec(layer="A", layers=["B"]).layer_patterns() == ["a", "b"]
    assert EntityQuerySpec(entity_type="line", entity_types=["circle"]).type_names() == ["line", "circle"]
    assert not EntityQuerySpec(layers=["X*"]).matches_layer("Y")


# #51 -------------------------------------------------------------------------------------------

class _Poly:
    def __init__(self) -> None:
        self.bulges: dict[int, float] = {}
        self.Closed = False
        self.Layer = "0"

    def SetBulge(self, index: int, bulge: float) -> None:
        self.bulges[index] = bulge


class _Space:
    def __init__(self) -> None:
        self.flat = None
        self.poly = _Poly()

    def AddLightWeightPolyline(self, flat):
        self.flat = list(flat)
        return self.poly


def _adapter() -> ComCadAdapter:
    adapter = object.__new__(ComCadAdapter)
    adapter._double_array = lambda values: list(values)       # type: ignore[method-assign]
    return adapter


def test_lwpolyline_vertex_bulges() -> None:
    space = _Space()
    spec = EntityCreateSpec(entity_type="lwpolyline", params={"vertices": [[0, 0, 0.5], [10, 0], [10, 10, -0.2]]})
    _adapter()._create_entity(space, spec)
    assert space.flat == [0.0, 0.0, 10.0, 0.0, 10.0, 10.0]
    assert space.poly.bulges == {0: 0.5, 2: -0.2}


def test_lwpolyline_bulges_list() -> None:
    space = _Space()
    spec = EntityCreateSpec(entity_type="lwpolyline", params={"vertices": [[0, 0], [1, 0], [1, 1]], "bulges": [0, 1]})
    _adapter()._create_entity(space, spec)
    assert space.poly.bulges == {1: 1.0}


# #50 -------------------------------------------------------------------------------------------

def test_runner_locals_are_prefixed() -> None:
    text = (LISP_DIR / "zwmcp_core.lsp").read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith("(defun zwmcp:run "))
    names = header[header.index("(", 1) + 1: header.rindex(")")].replace("/", " ").split()
    assert names and all(name.startswith("zwmcp:%") for name in names), names


def test_request_that_ran_without_result_is_reported(tmp_path: Path) -> None:
    channel = LispChannel(ipc_dir=tmp_path, codepage="cp1250", poll_s=0.01)

    def zwcad(command: str) -> None:        # runs, deletes request + ack, never writes the result
        import re
        rid = re.search(r'"([0-9a-f]+)" (T|nil)\)', command).group(1)

        def crash() -> None:
            (tmp_path / f"req_{rid}.lsp").unlink()
        threading.Timer(0.05, crash).start()

    with pytest.raises(LispChannelError) as info:
        channel.run(zwcad, "(setq res 1)", timeout_s=5, ack_timeout_s=3)
    assert "ran but wrote no result" in str(info.value)


# #52 -------------------------------------------------------------------------------------------

def test_swept_path_shows_layers_by_default() -> None:
    assert SweptPathRequest(path_handle="1A").show_layers is True
    text = (LISP_DIR / "mo_tools.lsp").read_text(encoding="utf-8")
    assert '(zwmcp:opt "show_layers" opts T)' in text
    assert text.count("(mosp:layers-begin opts)") == 2 and text.count('(cons "layers" (mosp:layers-end))') == 2


def test_bundle_files_still_listed() -> None:
    assert set(BUNDLE_FILES) == {"zwmcp_core.lsp", "mo_tools.lsp"}


# #55 -------------------------------------------------------------------------------------------

class _Ent:
    def __init__(self, object_name: str) -> None:
        self.ObjectName = object_name


def test_type_filter_maps_acdbpolyline_and_dxf_names() -> None:
    adapter = object.__new__(ComCadAdapter)
    adapter._safe_get = lambda obj, name, default=None: getattr(obj, name, default)  # type: ignore[method-assign]
    poly, hatch, ins = _Ent("AcDbPolyline"), _Ent("AcDbHatch"), _Ent("AcDbBlockReference")
    assert adapter._entity_type(poly) == "lwpolyline"
    assert adapter._match_type(poly, "LWPOLYLINE") and adapter._match_type(poly, "lwpolyline")
    assert adapter._match_type(hatch, "HATCH") and not adapter._match_type(hatch, "LWPOLYLINE")
    assert adapter._match_type(ins, "INSERT") and adapter._match_type(ins, "block_reference")
