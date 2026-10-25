from __future__ import annotations

from zwcad_standard_mcp import geometry
from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import BoundaryDistanceRequest
from zwcad_standard_mcp.services import CadService

# cm drawing: plot 20 x 35 m, building 10 x 15 m placed 4 m from the west and 3.5 m from the south boundary
PLOT = [[0, 0], [2000, 0], [2000, 3500], [0, 3500]]
BUILDING = [[400, 350], [1400, 350], [1400, 1850], [400, 1850]]


def _service() -> tuple[CadService, FakeCadAdapter]:
    adapter = FakeCadAdapter()
    adapter.insunits = 5
    adapter.entities["P"] = {"handle": "P", "object_name": "AcDbPolyline", "layer": "0", "layout": "Model", "points": PLOT, "closed": True}
    adapter.entities["B"] = {"handle": "B", "object_name": "AcDbPolyline", "layer": "0", "layout": "Model", "points": BUILDING, "closed": True}
    return CadService(adapter, Settings(adapter="fake")), adapter


def test_segment_distance_parallel_and_crossing() -> None:
    assert geometry.segment_distance((0, 0), (10, 0), (0, 5), (10, 5))[0] == 5
    assert geometry.segment_distance((0, 0), (10, 10), (0, 10), (10, 0))[0] == 0


def test_distances_and_limits() -> None:
    service, _ = _service()
    result = service.dimension_to_boundaries(BoundaryDistanceRequest(
        building_handle="B", plot_handle="P", min_distance_m=4.0, boundary_min_m={0: 3.0}))
    by_index = {r["boundary_index"]: r for r in result["boundaries"]}
    assert result["units"] == "cm" and result["building_inside_plot"] is True
    assert by_index[0]["distance_m"] == 3.5 and by_index[0]["ok"] is True      # south, 3 m required
    assert by_index[3]["distance_m"] == 4.0 and by_index[3]["ok"] is True      # west, 4 m required
    assert by_index[1]["distance_m"] == 6.0                                     # east
    assert result["closest_m"] == 3.5 and result["all_ok"] is True


def test_violation_and_dimensions() -> None:
    service, adapter = _service()
    request = BoundaryDistanceRequest(building_handle="B", plot_handle="P", min_distance_m=4.0,
                                      report_within_m=5.0, create_dimensions=True)
    preview = service.dimension_to_boundaries(request, dry_run=True)
    assert preview["violations"] == [0] and preview["dimensions_to_create"] == 2
    done = service.dimension_to_boundaries(request, dry_run=False, confirm=True)
    assert len(done["dimensions"]) == 2
    assert adapter.entities[done["dimensions"][0]]["entity_type"] == "dimension_aligned"
