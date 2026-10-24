from __future__ import annotations

from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import AreaLimits, AreaTakeoffRequest, DimStyleSpec, HatchRequest, TextStyleSpec
from zwcad_standard_mcp.services import CadService


def _service() -> tuple[CadService, FakeCadAdapter]:
    adapter = FakeCadAdapter()
    adapter.text_styles, adapter.dim_styles = {}, {}
    # cm drawing: plot 20 x 35 m, building 15 x 10 m, paving 50 m2
    for handle, area in (("P1", 7_000_000.0), ("B1", 1_500_000.0), ("D1", 500_000.0)):
        adapter.entities[handle] = {"handle": handle, "object_name": "AcDbPolyline", "entity_type": "lwpolyline",
                                    "layer": "0", "layout": "Model", "area": area, "closed": True}
    return CadService(adapter, Settings(adapter="fake")), adapter


def test_area_takeoff_cm_with_limits() -> None:
    service, _ = _service()
    result = service.area_takeoff(AreaTakeoffRequest(
        plot_handle="P1", building_handles=["B1"], paved_handles=["D1"], gross_floor_area_m2=300,
        limits=AreaLimits(min_pbc_percent=70, max_building_percent=25, max_intensity=0.6),
    ))
    assert result["units"] == "cm" and result["plot_m2"] == 700.0
    assert result["areas_m2"] == {"buildings": 150.0, "paved": 50.0}
    assert result["biologically_active_m2"] == 500.0
    assert result["percent_of_plot"]["biologically_active"] == 71.43
    assert result["intensity"] == 0.429 and result["all_ok"] is True


def test_area_takeoff_rejects_open_outline() -> None:
    service, adapter = _service()
    adapter.entities["B1"]["closed"] = False
    try:
        service.area_takeoff(AreaTakeoffRequest(plot_handle="P1", building_handles=["B1"]))
    except Exception as exc:
        assert "not closed" in str(exc)
    else:
        raise AssertionError("expected a validation error")


def test_create_hatch_with_island() -> None:
    service, adapter = _service()
    preview = service.create_hatch(HatchRequest(outer_handle="P1", inner_handles=["B1"], layer="OUTLINE"), dry_run=True)
    assert preview["expected_area"] == 5_500_000.0
    result = service.create_hatch(HatchRequest(outer_handle="P1", inner_handles=["B1"], layer="OUTLINE"), dry_run=False, confirm=True)
    assert result["area"] == 5_500_000.0 and adapter.entities[result["handle"]]["layer"] == "OUTLINE"


def test_dim_style_requires_existing_text_style() -> None:
    service, _ = _service()
    spec = DimStyleSpec(name="PZT_500", text_style="PZT_TXT", linear_scale=0.01, decimals=2)
    try:
        service.ensure_dim_styles([spec], dry_run=True)
    except Exception as exc:
        assert "Text style not found" in str(exc)
    else:
        raise AssertionError("expected a validation error")
    service.ensure_text_styles([TextStyleSpec(name="PZT_TXT")], dry_run=False, confirm=True)
    result = service.ensure_dim_styles([spec], dry_run=False, confirm=True)
    assert result["results"][0]["created"] is True
