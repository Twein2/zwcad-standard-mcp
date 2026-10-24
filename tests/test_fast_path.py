from __future__ import annotations

from zwcad_standard_mcp.adapters.dialogs import safe_answer_for
from zwcad_standard_mcp.adapters.fake import FakeCadAdapter
from zwcad_standard_mcp.adapters.selection import build_filter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.models import EntityFilter, FilteredPropertyPatch
from zwcad_standard_mcp.services import CadService


def _service(max_batch_size: int = 10000) -> tuple[CadService, FakeCadAdapter]:
    adapter = FakeCadAdapter()
    return CadService(adapter, Settings(adapter="fake", max_batch_size=max_batch_size)), adapter


def test_build_filter_group_codes() -> None:
    codes, values = build_filter(
        EntityFilter(entity_types=["point", "text"], layers=["0", "MO_*"], color=256, space="model")
    )
    assert codes == [0, 8, 62, 410]
    assert values == ["POINT,TEXT", "0,MO_*", 256, "Model"]


def test_build_filter_paper_space() -> None:
    codes, values = build_filter(EntityFilter(space="paper"))
    assert codes == [410] and values == ["~Model"]


def test_count_entities_groups() -> None:
    service, _ = _service()
    result = service.count_entities(EntityFilter(space="all"), ["layer", "type"])
    assert result["count"] == 3
    assert result["by_type"]["INSERT"] == 1


def test_update_by_filter_dry_run_does_not_mutate() -> None:
    service, adapter = _service()
    patch = FilteredPropertyPatch(filter=EntityFilter(entity_types=["LINE"]), layer="OUTLINE")
    result = service.update_entities_by_filter(patch, dry_run=True)
    assert result["matched"] == 1
    assert adapter.entities["10"]["layer"] == "0"


def test_update_by_filter_requires_confirm_and_applies() -> None:
    service, adapter = _service()
    patch = FilteredPropertyPatch(filter=EntityFilter(layers=["0"]), layer="OUTLINE", color=256)
    rejected = service.update_entities_by_filter(patch, dry_run=False, confirm=False)
    assert rejected.get("requires_confirmation") is True
    result = service.update_entities_by_filter(patch, dry_run=False, confirm=True)
    assert result["changed"] == 1
    assert adapter.entities["10"]["layer"] == "OUTLINE"


def test_update_by_filter_rejects_missing_layer_and_limit() -> None:
    service, _ = _service(max_batch_size=1)
    import pytest

    from zwcad_standard_mcp.errors import BatchLimitError, ValidationError

    with pytest.raises(ValidationError):
        service.update_entities_by_filter(
            FilteredPropertyPatch(filter=EntityFilter(), layer="MISSING"), dry_run=True
        )
    with pytest.raises(BatchLimitError):
        service.update_entities_by_filter(
            FilteredPropertyPatch(filter=EntityFilter(space="all"), color=1), dry_run=True
        )


def test_safe_dialog_answers() -> None:
    unresolved = {
        "title": "References - Unresolved Reference Files",
        "buttons": ["Update the location of the referenced files", "Ignore unresolved references and continue"],
    }
    assert safe_answer_for(unresolved) == "Ignore unresolved references and continue"
    licence = {"title": "Authorization Message", "buttons": ["OK"]}
    assert safe_answer_for(licence) is None
