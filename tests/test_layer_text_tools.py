from __future__ import annotations

import json

from zwcad_standard_mcp import textops
from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import (
    EntityFilter,
    LayerMapRequest,
    LayerMapRule,
    LayerSpec,
    LayerStandard,
    TextUpdateRequest,
)
from zwcad_standard_mcp.services import CadService


def _service() -> tuple[CadService, FakeCadAdapter]:
    adapter = FakeCadAdapter()
    adapter.layers["EGB_A"] = {"name": "EGB_A", "color": 7, "linetype": "Continuous", "on": True, "locked": False, "frozen": False}
    adapter.layers["EGB_B"] = {"name": "EGB_B", "color": 7, "linetype": "Continuous", "on": True, "locked": False, "frozen": False}
    adapter.layers["EMPTY"] = {"name": "EMPTY", "color": 7, "linetype": "Continuous", "on": True, "locked": False, "frozen": False}
    adapter.entities["20"] = {"handle": "20", "object_name": "AcDbLine", "entity_type": "line", "layer": "EGB_A", "layout": "Model"}
    adapter.entities["21"] = {"handle": "21", "object_name": "AcDbLine", "entity_type": "line", "layer": "EGB_A", "layout": "Model"}
    adapter.entities["22"] = {"handle": "22", "object_name": "AcDbLine", "entity_type": "line", "layer": "EGB_B", "layout": "Model"}
    adapter.entities["30"] = {"handle": "30", "object_name": "AcDbMText", "entity_type": "mtext", "layer": "0", "layout": "Model",
                              "text": "{\\fArial|b0|i0;\\H2.5;DziaĹ‚ka}\\P1\\S1^2;"}
    adapter.entities["31"] = {"handle": "31", "object_name": "AcDbText", "entity_type": "text", "layer": "0", "layout": "Model",
                              "text": "ul. Sojowa"}
    return CadService(adapter, Settings(adapter="fake")), adapter


def _standard() -> LayerStandard:
    return LayerStandard(
        layers=[LayerSpec(name="A_GEO_EGIB", color=8)],
        rules=[LayerMapRule(source="EGB_*", target="A_GEO_EGIB")],
    )


def test_strip_mtext_formatting() -> None:
    assert textops.strip_mtext_formatting("{\\fArial|b0;\\H2.5x;Text}\\Pline\\~2 \\S1^2; \\\\ \\{") == "Text\\Pline 2 1/2 \\\\ \\{"


def test_repair_mojibake_only_changes_broken_text() -> None:
    broken = "Działka".encode("utf-8").decode("cp1250")
    assert textops.repair_mojibake(broken) == "Działka"
    assert textops.repair_mojibake("Działka") == "Działka"
    assert textops.repair_mojibake("plain") == "plain"


def test_replace_text_literal_and_regex() -> None:
    assert textops.replace_text("a.b.c", ".", "-") == "a-b-c"
    assert textops.replace_text("dz. 12/3", r"(\d+)/(\d+)", r"\2:\1", regex=True) == "dz. 3:12"
    assert textops.replace_text("ABC", "b", "x", case_sensitive=False) == "AxC"


def test_map_layers_plan_rename_largest_then_merge() -> None:
    service, adapter = _service()
    plan = service.map_layers(LayerMapRequest(standard=_standard(), verbose=True), dry_run=True)
    steps = {s["source"]: s for s in plan["steps"]}
    assert steps["EGB_A"]["action"] == "rename" and steps["EGB_A"]["entities"] == 2
    assert steps["EGB_B"]["action"] == "merge"
    assert plan["create_layers"] == []
    assert "EGB_A" in adapter.layers  # dry run does not mutate


def test_map_layers_execute_and_purge() -> None:
    service, adapter = _service()
    result = service.map_layers(LayerMapRequest(standard=_standard(), purge_unused=True, verbose=True), dry_run=False, confirm=True)
    assert result["complete"] is True
    assert "EGB_A" not in adapter.layers and "EGB_B" not in adapter.layers
    assert adapter.layers["A_GEO_EGIB"]["color"] == 8
    assert all(adapter.entities[h]["layer"] == "A_GEO_EGIB" for h in ("20", "21", "22"))
    assert "EMPTY" in result["deleted"]
    assert "0" in adapter.layers


def test_map_layers_from_json_file(tmp_path) -> None:
    service, _ = _service()
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(_standard().model_dump()), encoding="utf-8")
    plan = service.map_layers(LayerMapRequest(standard_path=str(path)), dry_run=True)
    assert plan["renames"] == 1 and plan["merges"] == 1


def test_update_text_entities_strip_and_repair() -> None:
    service, adapter = _service()
    preview = service.update_text_entities(TextUpdateRequest(operation="strip_formatting"), dry_run=True)
    assert preview["matched"] == 2 and preview["would_change"] == 1
    service.update_text_entities(TextUpdateRequest(operation="strip_formatting"), dry_run=False, confirm=True)
    service.update_text_entities(TextUpdateRequest(operation="repair_encoding"), dry_run=False, confirm=True)
    assert adapter.entities["30"]["text"] == "Działka\\P11/2"
    assert adapter.entities["31"]["text"] == "ul. Sojowa"


def test_update_text_entities_rejects_other_types() -> None:
    service, _ = _service()
    try:
        service.update_text_entities(
            TextUpdateRequest(operation="set", value="x", filter=EntityFilter(entity_types=["LINE"])), dry_run=True
        )
    except Exception as exc:
        assert "TEXT and MTEXT" in str(exc)
    else:
        raise AssertionError("expected a validation error")


def test_map_layers_summary_is_compact() -> None:
    service, _ = _service()
    plan = service.map_layers(LayerMapRequest(standard=_standard()), dry_run=True)
    assert "steps" not in plan and "empty_layers" not in plan
    assert plan["targets"]["A_GEO_EGIB"] == {"rename_from": "EGB_A", "merge": {"EGB_B": 1}, "empty_sources": 0}


def test_strip_formatting_keeps_leading_height() -> None:
    service, adapter = _service()
    service.update_text_entities(TextUpdateRequest(operation="strip_formatting"), dry_run=False, confirm=True)
    assert adapter.entities["30"]["height"] == 2.5
    assert "height" not in adapter.entities["31"]


def test_purge_unused_keeps_standard_layers() -> None:
    service, adapter = _service()
    standard = _standard()
    standard.layers.append(LayerSpec(name="A_EMPTY_STD"))
    adapter.layers["A_EMPTY_STD"] = {"name": "A_EMPTY_STD", "color": 7, "linetype": "Continuous", "on": True, "locked": False, "frozen": False}
    service.map_layers(LayerMapRequest(standard=standard, purge_unused=True), dry_run=False, confirm=True)
    service.map_layers(LayerMapRequest(standard=standard, purge_unused=True), dry_run=False, confirm=True)
    assert "A_EMPTY_STD" in adapter.layers and "A_GEO_EGIB" in adapter.layers
    assert "EMPTY" not in adapter.layers


def test_confirm_mode_off_skips_confirmation(monkeypatch=None) -> None:
    import os
    from zwcad_standard_mcp.permissions.policy import PermissionLevel
    from zwcad_standard_mcp.permissions_bridge import require_permission

    old = os.environ.get("ZWCAD_MCP_CONFIRM_MODE")
    try:
        os.environ["ZWCAD_MCP_CONFIRM_MODE"] = "strict"
        assert require_permission(PermissionLevel.DELETE, confirm=True) is not None
        os.environ["ZWCAD_MCP_CONFIRM_MODE"] = "off"
        assert require_permission(PermissionLevel.DELETE) is None
        assert require_permission(PermissionLevel.SAVE) is None
        service, adapter = _service()
        result = service.update_text_entities(TextUpdateRequest(operation="set", value="X"), dry_run=False)
        assert result["changed"] == 2
    finally:
        if old is None:
            os.environ.pop("ZWCAD_MCP_CONFIRM_MODE", None)
        else:
            os.environ["ZWCAD_MCP_CONFIRM_MODE"] = old
