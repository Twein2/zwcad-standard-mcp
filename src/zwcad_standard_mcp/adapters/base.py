from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable

from zwcad_standard_mcp.errors import UnsupportedOperationError
from zwcad_standard_mcp.models import (
    DimStyleSpec,
    HatchRequest,
    TextStyleSpec,
    EntityCreateSpec,
    EntityFilter,
    EntityQuerySpec,
    LayerSpec,
    PlotScopeRequest,
)


class CadAdapter(ABC):
    """Boundary between MCP-facing business logic and a concrete CAD API."""

    @abstractmethod
    def diagnose(self) -> dict: ...

    @abstractmethod
    def get_app_info(self) -> dict: ...

    @abstractmethod
    def get_current_document(self) -> dict: ...

    @abstractmethod
    def list_documents(self) -> list[dict]: ...

    @abstractmethod
    def scan_cad_folder(self, path: str, recursive: bool = False) -> dict: ...

    @abstractmethod
    def open_document(self, path: str, read_only: bool = False) -> dict: ...

    @abstractmethod
    def close_document(self, name: str, save_changes: bool = False) -> dict: ...

    @abstractmethod
    def activate_document(self, name: str) -> dict: ...

    @abstractmethod
    def save_document(self, file_path: str | None = None) -> dict: ...

    @abstractmethod
    def list_layers(self, detail: bool = True) -> list[dict]: ...

    @abstractmethod
    def ensure_layers(self, layers: list[LayerSpec]) -> list[dict]: ...

    @abstractmethod
    def audit_drawing(self, sample_limit: int) -> dict: ...

    @abstractmethod
    def get_selected_entities(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def query_entities(self, query: EntityQuerySpec) -> list[dict]: ...

    @abstractmethod
    def get_entity_details(self, handles: list[str]) -> list[dict]: ...

    @abstractmethod
    def update_entity_properties(self, handles: list[str], properties: dict[str, Any]) -> list[dict]: ...

    @abstractmethod
    def transform_entities(self, action: str, handles: list[str], params: dict[str, Any]) -> list[dict]: ...

    @abstractmethod
    def delete_entities(self, handles: list[str]) -> list[dict]: ...

    @abstractmethod
    def create_entities(self, entities: list[EntityCreateSpec]) -> list[dict]: ...

    @abstractmethod
    def list_layouts(self) -> list[dict]: ...

    @abstractmethod
    def get_layout_plot_settings(self, layout_name: str | None) -> dict: ...

    @abstractmethod
    def get_plot_capabilities(self) -> dict: ...

    @abstractmethod
    def preview_plot_scope(self, request: PlotScopeRequest) -> dict: ...

    @abstractmethod
    def get_current_view(self) -> dict: ...

    @abstractmethod
    def get_drawing_extents(self) -> dict: ...

    @abstractmethod
    def activate_layout(self, name: str) -> dict: ...

    @abstractmethod
    def plot_layouts(
        self,
        layout_names: list[str],
        output_dir: str,
        plot_configuration: str | None,
        extension: str,
        scope_type: str | None = None,
        window_lower_left: list[float] | None = None,
        window_upper_right: list[float] | None = None,
        selected_handles: list[str] | None = None,
        center_plot: bool | None = None,
        fit_to_paper: bool | None = None,
        custom_scale: float | None = None,
        override_policy: str = "temporary",
    ) -> list[dict]: ...

    @abstractmethod
    def export_drawing(self, base_file_path: str, extension: str) -> dict: ...

    @abstractmethod
    def verify_export_files(
        self,
        file_paths: list[str],
        layout_names: list[str] | None = None,
        min_size_bytes: int = 1024,
        expected_plot_range: dict[str, list[float]] | None = None,
    ) -> dict: ...

    @abstractmethod
    def list_block_definitions(self, detail: bool) -> list[dict]: ...

    @abstractmethod
    def list_block_references(
        self,
        scope: str,
        block_name: str | None,
        has_attributes: bool | None,
        limit: int,
    ) -> list[dict]: ...

    @abstractmethod
    def get_block_attributes(self, handles: list[str]) -> list[dict]: ...

    @abstractmethod
    def update_block_attributes(self, updates: list[dict[str, Any]]) -> list[dict]: ...

    @abstractmethod
    def insert_blocks(self, blocks: list[dict[str, Any]]) -> list[dict]: ...

    @abstractmethod
    def begin_undo(self) -> None: ...

    @abstractmethod
    def end_undo(self) -> None: ...

    # --- CAD-side filtered selection (fast path for large drawings) ---------------

    @abstractmethod
    def count_entities(self, flt: EntityFilter, group_by: list[str] | None = None) -> dict:
        """Count entities matching ``flt``; optional grouping by "type" and/or "layer"."""

    @abstractmethod
    def select_handles(self, flt: EntityFilter, limit: int) -> dict:
        """Return ``{"count", "handles", "truncated"}`` for entities matching ``flt``."""

    @abstractmethod
    def update_entities_by_filter(self, flt: EntityFilter, properties: dict[str, Any]) -> dict:
        """Apply ``properties`` to every matching entity without per-entity snapshots."""

    # --- Layer mapping and text editing ----------------------------------------------

    def rename_layer(self, old: str, new: str) -> dict:
        raise NotImplementedError

    def move_layer_entities(self, source: str, target: str, time_budget_s: float) -> dict:
        """Move every entity on ``source`` (all spaces) to ``target``.

        Returns ``{"matched", "moved", "failed", "remaining", "elapsed_s"}``; stops when the budget is spent.
        """
        raise NotImplementedError

    def delete_layer(self, name: str) -> dict:
        raise NotImplementedError

    def read_texts(self, flt: EntityFilter, limit: int) -> dict:
        """Return ``{"count", "items": [{"handle", "type", "text"}], "truncated"}`` for TEXT/MTEXT."""
        raise NotImplementedError

    def write_texts(self, changes: list[tuple[str, str, float | None]], time_budget_s: float) -> dict:
        """Set TextString (and Height when not None) by handle.

        Returns ``{"changed", "failed", "failures_sample", "remaining"}``.
        """
        raise NotImplementedError

    # --- Annotation styles, hatching, areas ---------------------------------------

    def ensure_text_style(self, spec: "TextStyleSpec") -> dict:
        raise NotImplementedError

    def ensure_dim_style(self, spec: "DimStyleSpec") -> dict:
        raise NotImplementedError

    def style_exists(self, kind: str, name: str) -> bool:
        """kind: 'text' or 'dim'."""
        raise NotImplementedError

    def create_hatch(self, request: "HatchRequest") -> dict:
        raise NotImplementedError

    def curve_areas(self, handles: list[str]) -> dict[str, dict]:
        """``{handle: {"success", "type", "closed", "area", "layer"}}`` in drawing units."""
        raise NotImplementedError

    def polyline_points(self, handle: str) -> dict:
        """``{"type", "closed", "points": [[x, y], ...]}`` for LWPOLYLINE / 2D POLYLINE."""
        raise NotImplementedError

    def drawing_units(self) -> int:
        """INSUNITS value (4 = mm, 5 = cm, 6 = m, 0 = unitless)."""
        raise NotImplementedError

    # --- AutoLISP channel, screenshot, offset ----------------------------------------

    def run_lisp(self, expression: str, undo: bool = False, timeout_s: float = 600.0, name: str = "lisp") -> dict:
        """Evaluate one LISP expression in the active document; ``{"ok", "value" | "error"}``."""
        raise UnsupportedOperationError("The LISP channel is not available with this adapter.")

    def lisp_call_expr(self, expression: str, undo: bool = False, name: str = "lisp", timeout_s: float = 600.0) -> Any:
        envelope = self.run_lisp(expression, undo=undo, timeout_s=timeout_s, name=name)
        if not envelope.get("ok"):
            from zwcad_standard_mcp.errors import LispChannelError

            raise LispChannelError(f"LISP error: {envelope.get('error')}")
        return envelope.get("value")

    def lisp_codepage(self) -> str:
        return "utf-8"

    def lisp_info(self) -> dict:
        return {}

    def screenshot(self, region: str = "drawing", max_width: int = 1280) -> dict:
        raise UnsupportedOperationError("Screenshots are not available with this adapter.")

    def offset_entities(self, handles: list[str], distance: float, both_sides: bool, layer: str | None) -> list[dict]:
        raise UnsupportedOperationError("Offset is not available with this adapter.")

    # --- Modal dialog handling ------------------------------------------------------

    def list_dialogs(self) -> list[dict]:
        """Visible modal dialogs owned by the CAD process (empty when unsupported)."""
        return []

    def job_status(self, job_id: str) -> dict:
        return {"success": False, "job_id": job_id, "error": "Background jobs are not supported by this adapter."}

    def dismiss_dialog(self, title_contains: str, button: str) -> dict:
        return {"success": False, "error": "Dialog handling is not supported by this adapter."}

