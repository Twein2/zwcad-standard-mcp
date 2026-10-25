from __future__ import annotations

import contextlib
import math
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

from zwcad_standard_mcp.errors import (
    CadBusyError,
    CadConnectionError,
    LispChannelError,
    EntityNotFoundError,
    ModalDialogError,
    NoActiveDocumentError,
    UnsupportedOperationError,
    ValidationError,
)
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

from .base import CadAdapter
from .detached import DetachedRunner
from .lisp_channel import LispChannel, lisp_string
from .dialogs import DialogWatchdog, click_dialog_button, find_process_dialogs, safe_answer_for
from .selection import COMMON_DXF_TYPES, _wild, build_filter, dxf_name, escape_layer_name


_OBJECT_ALIASES = {
    "line": {"acdbline", "line"},
    "circle": {"acdbcircle", "circle"},
    "arc": {"acdbarc", "arc"},
    "lwpolyline": {"acdbpolyline", "acdblwpolyline", "lwpolyline"},
    "polyline": {"acdb2dpolyline", "acdb3dpolyline", "polyline"},
    "text": {"acdbtext", "text"},
    "mtext": {"acdbmtext", "mtext"},
    "block_reference": {"acdbblockreference", "blockreference", "insert"},
    "dimension": {
        "acdbaligneddimension",
        "acdbrotateddimension",
        "acdbdiametricdimension",
        "acdbradialdimension",
        "acdb3pointangulardimension",
    },
}


class ComCadAdapter(CadAdapter):
    """ZWCAD Standard adapter using the public ActiveX/COM automation interface."""

    # System variables set while opening or plotting so that ZWCAD does not stop on
    # informational dialogs (proxy notice, xref notification, background plotting).
    GUARD_SYSVARS: tuple[tuple[str, Any], ...] = (
        ("PROXYNOTICE", 0),
        ("XREFNOTIFY", 0),
        ("BACKGROUNDPLOT", 0),
    )

    def __init__(
        self,
        prog_id: str = "ZWCAD.Application",
        auto_start: bool = False,
        auto_dismiss_dialogs: bool = True,
    ) -> None:
        self.prog_id = prog_id
        self.auto_start = auto_start
        self.auto_dismiss_dialogs = auto_dismiss_dialogs
        self.last_dialog_report: dict | None = None
        self._main_hwnd: int | None = None
        self._app: Any | None = None
        self._pythoncom: Any | None = None
        self._win32_client: Any | None = None
        self._lisp_channel: LispChannel | None = None
        self._lisp_count_error: str | None = None
        self.use_lisp_counts = os.getenv("ZWCAD_MCP_LISP_COUNTS", "1").strip().lower() not in {"0", "false", "off", "no"}
        self._runner = DetachedRunner(
            find_dialogs=lambda: find_process_dialogs(self._main_hwnd) if self._main_hwnd else [],
            is_safe_dialog=lambda d: self.auto_dismiss_dialogs and safe_answer_for(d) is not None,
        )

    def _load_win32(self) -> None:
        if sys.platform != "win32":
            raise CadConnectionError("ZWCAD COM adapter can only run on Windows.")
        if self._pythoncom is not None:
            return
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError as exc:
            raise CadConnectionError(
                "pywin32 is not installed. Run: pip install pywin32"
            ) from exc
        self._pythoncom = pythoncom
        self._win32_client = win32com.client

    def _initialize_com(self) -> None:
        self._load_win32()
        try:
            self._pythoncom.CoInitialize()
        except Exception:
            pass

    def _connect(self, force: bool = False) -> Any:
        # While a detached call waits in ZWCAD every COM call would hang: fail fast instead.
        self._runner.ensure_idle()
        self._initialize_com()
        if self._app is not None and not force:
            try:
                _ = self._app.Version
                return self._app
            except Exception:
                self._app = None

        try:
            self._app = self._win32_client.GetActiveObject(self.prog_id)
        except Exception as first_error:
            if not self.auto_start:
                raise CadConnectionError(
                    f"Cannot connect to a running ZWCAD instance via '{self.prog_id}'. "
                    "Start ZWCAD and open a DWG first, or set ZWCAD_MCP_AUTO_START=true."
                ) from first_error
            try:
                self._app = self._win32_client.Dispatch(self.prog_id)
                self._app.Visible = True
            except Exception as second_error:
                raise CadConnectionError(
                    f"Cannot start ZWCAD via COM ProgID '{self.prog_id}'."
                ) from second_error
        try:
            self._main_hwnd = int(self._app.HWND)
        except Exception:
            pass
        return self._app

    def _doc(self) -> Any:
        app = self._connect()
        try:
            doc = app.ActiveDocument
        except Exception as exc:
            raise NoActiveDocumentError("ZWCAD has no active document.") from exc
        if doc is None:
            raise NoActiveDocumentError("ZWCAD has no active document.")
        return doc

    @staticmethod
    def _safe_get(obj: Any, name: str, default: Any = None) -> Any:
        try:
            return getattr(obj, name)
        except Exception:
            return default

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        if isinstance(value, dict):
            return {str(k): cls._json_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_value(v) for v in value]
        try:
            return [cls._json_value(v) for v in value]
        except Exception:
            return str(value)

    def _point(self, values: Iterable[float]) -> Any:
        coords = list(values)
        while len(coords) < 3:
            coords.append(0.0)
        return self._win32_client.VARIANT(
            self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8,
            tuple(float(v) for v in coords[:3]),
        )

    def _double_array(self, values: Iterable[float]) -> Any:
        return self._win32_client.VARIANT(
            self._pythoncom.VT_ARRAY | self._pythoncom.VT_R8,
            tuple(float(v) for v in values),
        )

    @staticmethod
    def _iter_collection(collection: Any) -> Iterator[Any]:
        try:
            for item in collection:
                yield item
            return
        except Exception:
            pass
        count = int(getattr(collection, "Count", 0))
        for index in range(count):
            try:
                yield collection.Item(index)
            except Exception:
                yield collection.Item(index + 1)

    def _entity_type(self, entity: Any) -> str:
        object_name = str(self._safe_get(entity, "ObjectName", type(entity).__name__))
        lowered = object_name.lower()
        for alias, candidates in _OBJECT_ALIASES.items():
            if lowered in candidates:
                return alias
        return object_name

    def _block_name(self, entity: Any) -> str | None:
        for name in ("EffectiveName", "Name"):
            value = self._safe_get(entity, name)
            if value:
                return str(value)
        return None

    def _entity_summary(self, entity: Any, layout: str | None = None) -> dict:
        data = {
            "handle": str(self._safe_get(entity, "Handle", "")),
            "object_name": str(self._safe_get(entity, "ObjectName", "")),
            "entity_type": self._entity_type(entity),
            "layer": str(self._safe_get(entity, "Layer", "")),
        }
        if layout is not None:
            data["layout"] = layout
        if data["entity_type"] == "block_reference":
            data["block_name"] = self._block_name(entity)
            data["has_attributes"] = bool(self._safe_get(entity, "HasAttributes", False))
        for prop, key in (("TextString", "text"), ("Visible", "visible")):
            value = self._safe_get(entity, prop)
            if value is not None:
                data[key] = self._json_value(value)
        return data

    def _entity_details(self, entity: Any, layout: str | None = None) -> dict:
        data = self._entity_summary(entity, layout)
        for prop in (
            "Color",
            "Linetype",
            "LinetypeScale",
            "Lineweight",
            "StartPoint",
            "EndPoint",
            "Center",
            "Radius",
            "Diameter",
            "StartAngle",
            "EndAngle",
            "Area",
            "Length",
            "Height",
            "Rotation",
            "InsertionPoint",
            "Closed",
            "Coordinates",
            "Measurement",
            "StyleName",
            "TextHeight",
            "ScaleFactor",
            "LinearScaleFactor",
            "TextOverride",
            "TextStyle",
        ):
            value = self._safe_get(entity, prop)
            if value is not None:
                data[prop] = self._json_value(value)
        try:
            min_point, max_point = entity.GetBoundingBox()
            data["bounding_box"] = {
                "min": self._json_value(min_point),
                "max": self._json_value(max_point),
            }
        except Exception:
            pass
        if data.get("entity_type") == "block_reference" and data.get("has_attributes"):
            data["attributes"] = self._read_attributes(entity)
        return data

    def _spaces(self, scope: str) -> Iterator[tuple[str, Any]]:
        doc = self._doc()
        if scope == "model":
            yield "Model", doc.ModelSpace
            return
        if scope == "paper":
            active = self._safe_get(doc, "ActiveLayout")
            name = str(self._safe_get(active, "Name", "Paper"))
            yield name, doc.PaperSpace
            return
        if scope != "all_layouts":
            raise ValidationError(f"Unsupported scope: {scope}")
        yield "Model", doc.ModelSpace
        for layout in self._iter_collection(doc.Layouts):
            name = str(self._safe_get(layout, "Name", ""))
            if name.lower() == "model":
                continue
            block = self._safe_get(layout, "Block")
            if block is not None:
                yield name, block

    def _handle_to_object(self, handle: str) -> Any:
        try:
            return self._doc().HandleToObject(handle)
        except Exception as exc:
            raise EntityNotFoundError(f"Entity handle not found: {handle}") from exc

    def _match_type(self, entity: Any, expected: str | None) -> bool:
        if not expected:
            return True
        actual = self._entity_type(entity).lower()
        expected_lower = expected.lower().strip()
        if expected_lower in _OBJECT_ALIASES:
            return actual == expected_lower
        object_name = str(self._safe_get(entity, "ObjectName", ""))
        # also accept the DXF name (LWPOLYLINE, INSERT, HATCH ...), as count_entities / select_entities do
        return expected_lower in {actual, object_name.lower(), dxf_name(object_name).lower()}

    def _read_attributes(self, block_ref: Any) -> list[dict]:
        try:
            attrs = block_ref.GetAttributes()
        except Exception:
            return []
        results: list[dict] = []
        for attr in attrs or []:
            results.append(
                {
                    "tag": str(self._safe_get(attr, "TagString", "")),
                    "text": str(self._safe_get(attr, "TextString", "")),
                    "handle": str(self._safe_get(attr, "Handle", "")),
                    "constant": bool(self._safe_get(attr, "Constant", False)),
                }
            )
        return results

    def _bounds_from_corners(self, min_point: Any, max_point: Any) -> dict:
        return {
            "min": self._json_value(min_point),
            "max": self._json_value(max_point),
        }

    def _try_get_bounding_box(self, entity: Any) -> dict | None:
        try:
            min_point, max_point = entity.GetBoundingBox()
            return self._bounds_from_corners(min_point, max_point)
        except Exception:
            return None

    def _merge_bounds(self, bounds: list[dict]) -> dict:
        if not bounds:
            return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
        min_vals = [float("inf"), float("inf"), float("inf")]
        max_vals = [float("-inf"), float("-inf"), float("-inf")]
        for box in bounds:
            mn = box.get("min", [0, 0, 0])
            mx = box.get("max", [0, 0, 0])
            for i in range(3):
                min_vals[i] = min(min_vals[i], float(mn[i]) if i < len(mn) else 0.0)
                max_vals[i] = max(max_vals[i], float(mx[i]) if i < len(mx) else 0.0)
        return {"min": min_vals, "max": max_vals}

    def _get_space(self, entity: Any) -> str:
        owner = self._safe_get(entity, "OwnerID")
        if owner is None:
            return "unknown"
        try:
            doc = self._doc()
            owner_obj = doc.ObjectIdToObject(owner)
            owner_name = str(self._safe_get(owner_obj, "ObjectName", "")).lower()
            if "paperspace" in owner_name:
                return "paper"
            if "modelspace" in owner_name:
                return "model"
        except Exception:
            pass
        return "unknown"

    def _scan_pc5_files(self) -> list[str]:
        candidates = []
        for base in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path.home() / "AppData" / "Roaming",
            Path.home() / "AppData" / "Local",
        ):
            if not base.exists():
                continue
            for root in base.rglob("*.pc5"):
                try:
                    candidates.append(str(root))
                except Exception:
                    pass
        return candidates[:50]

    def _list_plot_devices(self, app: Any) -> list[str]:
        try:
            return [str(d) for d in app.GetPlotDevices()]
        except Exception:
            pass
        try:
            return [str(d) for d in app.GetPlotDeviceNames()]
        except Exception:
            pass
        return []

    def _list_media_names(self, plot: Any, config_name: str) -> list[str]:
        try:
            return [str(m) for m in plot.GetCanonicalMediaNames(config_name)]
        except Exception:
            pass
        try:
            return [str(m) for m in plot.GetCanonicalMediaNameList(config_name)]
        except Exception:
            pass
        return []

    @contextlib.contextmanager
    def _undo_group(self) -> Iterator[None]:
        self.begin_undo()
        try:
            yield
        finally:
            self.end_undo()

    # ------------------------------------------------------------------ dialogs

    def list_dialogs(self) -> list[dict]:
        # Uses only Win32 window enumeration, so it also works while a COM call is blocked.
        if self._main_hwnd is None:
            try:
                self._connect()
            except Exception:
                return []
        return find_process_dialogs(self._main_hwnd)

    def dismiss_dialog(self, title_contains: str, button: str) -> dict:
        for dialog in self.list_dialogs():
            if title_contains.lower() in str(dialog.get("title", "")).lower():
                ok = click_dialog_button(dialog["hwnd"], button)
                return {"success": ok, "title": dialog.get("title"), "button": button,
                        "buttons": dialog.get("buttons")}
        return {"success": False, "error": f"No open dialog with title containing '{title_contains}'."}

    @contextlib.contextmanager
    def _dialog_guard(self, doc: Any | None = None) -> Iterator[DialogWatchdog]:
        """Refuse to start when a dialog is already open, silence known prompts, watch for new ones."""
        self._connect()
        pending = find_process_dialogs(self._main_hwnd)
        if pending:
            titles = ", ".join(repr(d.get("title")) for d in pending)
            raise ModalDialogError(f"ZWCAD shows a modal dialog: {titles}", pending)
        target = doc
        if target is None:
            try:
                target = self._app.ActiveDocument
            except Exception:
                target = None
        saved: dict[str, Any] = {}
        if target is not None:
            for name, value in self.GUARD_SYSVARS:
                try:
                    old = target.GetVariable(name)
                    if old != value:
                        target.SetVariable(name, value)
                        saved[name] = old
                except Exception:
                    pass  # variable unknown in this ZWCAD build
        watchdog = DialogWatchdog(self._main_hwnd, auto_dismiss=self.auto_dismiss_dialogs)
        try:
            with watchdog:
                yield watchdog
        finally:
            for name, old in saved.items():
                try:
                    target.SetVariable(name, old)
                except Exception:
                    pass
            self.last_dialog_report = {**watchdog.report(), "sysvars_overridden": sorted(saved)}

    # ---------------------------------------------------------- filtered selection

    @contextlib.contextmanager
    def _selection(self, doc: Any) -> Iterator[Any]:
        selection = doc.SelectionSets.Add(f"ZWCAD_MCP_{uuid.uuid4().hex[:10]}")
        try:
            yield selection
        finally:
            try:
                selection.Delete()
            except Exception:
                pass

    def _select(self, selection: Any, flt: EntityFilter) -> int:
        codes, values = build_filter(flt)
        with contextlib.suppress(Exception):
            selection.Clear()
        origin = self._point([0, 0, 0])
        if codes:
            filter_type = self._win32_client.VARIANT(
                self._pythoncom.VT_ARRAY | self._pythoncom.VT_I2, tuple(int(c) for c in codes)
            )
            filter_data = self._win32_client.VARIANT(
                self._pythoncom.VT_ARRAY | self._pythoncom.VT_VARIANT, tuple(values)
            )
            selection.Select(5, origin, origin, filter_type, filter_data)
        else:
            selection.Select(5, origin, origin)
        return int(selection.Count)

    def _layer_names(self, doc: Any) -> list[str]:
        return [str(self._safe_get(layer, "Name", "")) for layer in self._iter_collection(doc.Layers)]

    def _lisp_filter(self, flt: EntityFilter) -> str:
        codes, values = build_filter(flt)
        pairs = []
        for code, value in zip(codes, values):
            literal = str(int(value)) if isinstance(value, int) else lisp_string(str(value), self._lisp().codepage)
            pairs.append(f"(cons {int(code)} {literal})")
        return "(list " + " ".join(pairs) + ")" if pairs else "nil"

    def _count_lisp(self, flt: EntityFilter, groups: set[str]) -> dict:
        """One ssget per grouping inside ZWCAD (FINDINGS #29, map_layers planning 16-26 s via COM)."""
        started = time.monotonic()
        result: dict[str, Any] = {"engine": "lisp"}
        expr_filter = self._lisp_filter(flt)
        for name, code in (("layer", 8), ("type", 0)):
            if name not in groups:
                continue
            value = self.lisp_call_expr(f"(zwmcp:count-by {code} {expr_filter})", name="count_entities")
            result["count"] = int(value.get("total") or 0)
            grouped = value.get("groups") or {}
            result["by_" + name] = dict(sorted(((str(k), int(v)) for k, v in grouped.items()), key=lambda kv: -kv[1]))
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        return result

    def count_entities(self, flt: EntityFilter, group_by: list[str] | None = None) -> dict:
        groups = set(group_by or [])
        lisp_note = None
        if groups & {"layer", "type"} and self.use_lisp_counts and not self._lisp_count_error:
            try:
                return self._count_lisp(flt, groups)
            except LispChannelError as exc:
                # Do not retry LISP on every call; COM selections below give the same numbers, slower.
                self._lisp_count_error = lisp_note = str(exc)
        doc = self._doc()
        started = time.monotonic()
        result: dict[str, Any] = {"engine": "com"}
        if lisp_note or self._lisp_count_error:
            result["lisp_fallback_reason"] = lisp_note or self._lisp_count_error
        with self._selection(doc) as selection:
            result["count"] = self._select(selection, flt)
            groups = set(group_by or [])
            if "layer" in groups and result["count"]:
                names = [n for n in self._layer_names(doc) if not flt.layers or _wild(n, flt.layers)]
                by_layer = {}
                for name in names:
                    pattern = escape_layer_name(name)
                    count = self._select(selection, flt.model_copy(update={"layers": [pattern]}))
                    if count:
                        by_layer[name] = count
                result["by_layer"] = dict(sorted(by_layer.items(), key=lambda kv: -kv[1]))
            if "type" in groups and result["count"]:
                # One CAD-side selection per type; never iterate entities over COM (FINDINGS #29).
                by_type: dict[str, int] = {}
                for dxf_type in flt.entity_types or COMMON_DXF_TYPES:
                    count = self._select(selection, flt.model_copy(update={"entity_types": [dxf_type]}))
                    if count:
                        by_type[dxf_type] = count
                other = result["count"] - sum(by_type.values())
                if other > 0:
                    by_type["OTHER"] = other
                result["by_type"] = dict(sorted(by_type.items(), key=lambda kv: -kv[1]))
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        return result

    def select_handles(self, flt: EntityFilter, limit: int) -> dict:
        doc = self._doc()
        with self._selection(doc) as selection:
            count = self._select(selection, flt)
            handles = []
            for index, entity in enumerate(self._iter_collection(selection)):
                if index >= limit:
                    break
                handles.append(str(self._safe_get(entity, "Handle", "")))
        return {"count": count, "handles": handles, "truncated": count > len(handles)}

    def update_entities_by_filter(self, flt: EntityFilter, properties: dict[str, Any]) -> dict:
        mapping = {
            "layer": "Layer",
            "color": "Color",
            "linetype": "Linetype",
            "linetype_scale": "LinetypeScale",
            "lineweight": "Lineweight",
        }
        doc = self._doc()
        started = time.monotonic()
        changed = 0
        failures: list[dict] = []
        with self._undo_group(), self._selection(doc) as selection:
            matched = self._select(selection, flt)
            for entity in self._iter_collection(selection):
                try:
                    for key, value in properties.items():
                        if value is not None:
                            setattr(entity, mapping[key], value)
                    changed += 1
                except Exception as exc:
                    if len(failures) < 20:
                        failures.append({"handle": str(self._safe_get(entity, "Handle", "")), "error": str(exc)})
        return {
            "matched": matched,
            "changed": changed,
            "failed": matched - changed,
            "failures_sample": failures,
            "elapsed_s": round(time.monotonic() - started, 3),
        }

    # ---------------------------------------------------------- layer mapping / texts

    def rename_layer(self, old: str, new: str) -> dict:
        doc = self._doc()
        with self._undo_group():
            try:
                doc.Layers.Item(old).Name = new
                return {"success": True, "source": old, "target": new}
            except Exception as exc:
                return {"success": False, "source": old, "target": new, "error": str(exc)}

    def move_layer_entities(self, source: str, target: str, time_budget_s: float) -> dict:
        doc = self._doc()
        started = time.monotonic()
        moved = 0
        failed = 0
        failures: list[dict] = []
        flt = EntityFilter(layers=[escape_layer_name(source)], space="all")
        with self._undo_group(), self._selection(doc) as selection:
            matched = self._select(selection, flt)
            for entity in self._iter_collection(selection):
                if time.monotonic() - started > time_budget_s:
                    break
                try:
                    entity.Layer = target
                    moved += 1
                except Exception as exc:
                    failed += 1
                    if len(failures) < 10:
                        failures.append({"handle": str(self._safe_get(entity, "Handle", "")), "error": str(exc)})
        return {
            "matched": matched,
            "moved": moved,
            "failed": failed,
            "failures_sample": failures,
            "remaining": matched - moved - failed,
            "elapsed_s": round(time.monotonic() - started, 3),
        }

    def delete_layer(self, name: str) -> dict:
        doc = self._doc()
        try:
            doc.Layers.Item(name).Delete()
            return {"name": name, "success": True}
        except Exception as exc:
            return {"name": name, "success": False, "error": str(exc)}

    def read_texts(self, flt: EntityFilter, limit: int) -> dict:
        doc = self._doc()
        items: list[dict] = []
        with self._selection(doc) as selection:
            count = self._select(selection, flt)
            for index, entity in enumerate(self._iter_collection(selection)):
                if index >= limit:
                    break
                items.append(
                    {
                        "handle": str(self._safe_get(entity, "Handle", "")),
                        "type": dxf_name(str(self._safe_get(entity, "ObjectName", ""))),
                        "text": str(self._safe_get(entity, "TextString", "")),
                    }
                )
        return {"count": count, "items": items, "truncated": count > len(items)}

    def write_texts(self, changes: list[tuple[str, str, float | None]], time_budget_s: float) -> dict:
        doc = self._doc()
        started = time.monotonic()
        changed = 0
        failures: list[dict] = []
        done = 0
        with self._undo_group():
            for handle, text, height in changes:
                if time.monotonic() - started > time_budget_s:
                    break
                done += 1
                try:
                    entity = doc.HandleToObject(handle)
                    entity.TextString = text
                    if height is not None:
                        entity.Height = height
                    changed += 1
                except Exception as exc:
                    if len(failures) < 10:
                        failures.append({"handle": handle, "error": str(exc)})
        return {
            "changed": changed,
            "failed": done - changed,
            "failures_sample": failures,
            "remaining": len(changes) - done,
            "elapsed_s": round(time.monotonic() - started, 3),
        }

    # ---------------------------------------------------------- styles / hatch / areas

    def _sysvar_value(self, value: Any) -> Any:
        # AutoCAD-compatible SetVariable expects VT_I2 for integer variables.
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            return self._win32_client.VARIANT(self._pythoncom.VT_I2, value)
        return value

    def style_exists(self, kind: str, name: str) -> bool:
        doc = self._doc()
        collection = doc.TextStyles if kind == "text" else doc.DimStyles
        try:
            collection.Item(name)
            return True
        except Exception:
            return False

    def ensure_text_style(self, spec: TextStyleSpec) -> dict:
        doc = self._doc()
        created = not self.style_exists("text", spec.name)
        notes: list[str] = []
        with self._undo_group():
            style = doc.TextStyles.Add(spec.name) if created else doc.TextStyles.Item(spec.name)
            try:
                # 238 = EASTEUROPE_CHARSET (Polish), 34 = VARIABLE_PITCH | FF_SWISS
                style.SetFont(spec.font, spec.bold, spec.italic, 238, 34)
            except Exception as exc:
                notes.append(f"SetFont failed ({exc}); using FontFile")
                style.FontFile = spec.font if spec.font.lower().endswith((".ttf", ".shx")) else f"{spec.font}.ttf"
            style.Height = spec.height
            style.Width = spec.width_factor
            style.ObliqueAngle = math.radians(spec.oblique_deg)
        return {"name": spec.name, "success": True, "created": created, "font": spec.font, "notes": notes}

    def ensure_dim_style(self, spec: DimStyleSpec) -> dict:
        doc = self._doc()
        wanted: dict[str, Any] = {
            "DIMTXSTY": spec.text_style,
            "DIMTXT": spec.text_height,
            "DIMASZ": spec.arrow_size,
            "DIMBLK": spec.arrow_block,
            "DIMSCALE": spec.overall_scale,
            "DIMLFAC": spec.linear_scale,
            "DIMDEC": spec.decimals,
            "DIMDSEP": spec.decimal_separator,
            "DIMGAP": spec.text_gap,
            "DIMEXO": spec.ext_line_offset,
            "DIMEXE": spec.ext_line_extension,
        }
        wanted.update({k.upper(): v for k, v in spec.extra.items()})
        wanted = {k: v for k, v in wanted.items() if v is not None}
        if spec.arrow_block == "":
            wanted["DIMBLK"] = "."
        created = not self.style_exists("dim", spec.name)
        saved: dict[str, Any] = {}
        applied: list[str] = []
        failed: dict[str, str] = {}
        with self._undo_group():
            for name, value in wanted.items():
                try:
                    saved[name] = doc.GetVariable(name)
                    doc.SetVariable(name, self._sysvar_value(value))
                    applied.append(name)
                except Exception as exc:
                    failed[name] = str(exc)
            style = doc.DimStyles.Add(spec.name) if created else doc.DimStyles.Item(spec.name)
            style.CopyFrom(doc)
            refreshed = 0 if created else self._refresh_dimensions(doc, spec.name)
            if spec.set_current:
                doc.ActiveDimStyle = style
            else:
                for name, old in saved.items():
                    try:
                        doc.SetVariable(name, self._sysvar_value(old) if isinstance(old, int) else old)
                    except Exception:
                        pass
        return {
            "name": spec.name,
            "success": not failed,
            "created": created,
            "variables_set": applied,
            "variables_failed": failed,
            "current": spec.set_current,
            "dimensions_refreshed": refreshed,
        }

    def _refresh_dimensions(self, doc: Any, style_name: str) -> int:
        """Re-apply an updated dimension style to existing dimensions (they keep old geometry otherwise)."""
        refreshed = 0
        # Re-assigning the same StyleName is a no-op (verified live, FINDINGS #39): switch to another style and back.
        other = next(
            (str(s.Name) for s in self._iter_collection(doc.DimStyles) if str(s.Name).lower() != style_name.lower()),
            None,
        )
        if other is None:
            other = "_ZWCAD_MCP_TMP"
            doc.DimStyles.Add(other)
        with self._selection(doc) as selection:
            filter_type = self._win32_client.VARIANT(self._pythoncom.VT_ARRAY | self._pythoncom.VT_I2, (0, 3))
            filter_data = self._win32_client.VARIANT(
                self._pythoncom.VT_ARRAY | self._pythoncom.VT_VARIANT, ("DIMENSION", style_name)
            )
            origin = self._point([0, 0, 0])
            selection.Select(5, origin, origin, filter_type, filter_data)
            for dim in self._iter_collection(selection):
                with contextlib.suppress(Exception):
                    dim.StyleName = other
                    dim.StyleName = style_name
                    dim.Update()
                    refreshed += 1
        return refreshed

    def create_hatch(self, request: HatchRequest) -> dict:
        doc = self._doc()
        outer = self._handle_to_object(request.outer_handle)
        inners = [self._handle_to_object(h) for h in request.inner_handles]
        objects = lambda items: self._win32_client.VARIANT(  # noqa: E731
            self._pythoncom.VT_ARRAY | self._pythoncom.VT_DISPATCH, tuple(items)
        )
        with self._undo_group():
            hatch = doc.ModelSpace.AddHatch(1, request.pattern, request.associative)  # 1 = predefined pattern
            try:
                if request.pattern.upper() != "SOLID":
                    hatch.PatternScale = request.scale
                    hatch.PatternAngle = math.radians(request.angle_deg)
                hatch.AppendOuterLoop(objects([outer]))
                for inner in inners:
                    hatch.AppendInnerLoop(objects([inner]))
                if request.layer:
                    hatch.Layer = request.layer
                if request.color is not None:
                    hatch.Color = request.color
                hatch.Evaluate()
            except Exception:
                with contextlib.suppress(Exception):
                    hatch.Delete()
                raise
        return {
            "success": True,
            "handle": str(hatch.Handle),
            "layer": str(self._safe_get(hatch, "Layer", "")),
            "pattern": request.pattern,
            "area": float(self._safe_get(hatch, "Area", 0.0)),
            "islands": len(inners),
        }

    def curve_areas(self, handles: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for handle in handles:
            try:
                obj = self._doc().HandleToObject(handle)
                out[handle] = {
                    "success": True,
                    "type": dxf_name(str(self._safe_get(obj, "ObjectName", ""))),
                    "closed": bool(self._safe_get(obj, "Closed", True)),
                    "area": float(obj.Area),
                    "layer": str(self._safe_get(obj, "Layer", "")),
                }
            except Exception as exc:
                out[handle] = {"success": False, "error": str(exc)}
        return out

    def polyline_points(self, handle: str) -> dict:
        obj = self._handle_to_object(handle)
        kind = dxf_name(str(self._safe_get(obj, "ObjectName", "")))
        if kind not in {"LWPOLYLINE", "POLYLINE"}:
            raise ValidationError(f"Entity {handle} is {kind}, expected a polyline.")
        coords = list(obj.Coordinates)
        step = 2 if kind == "LWPOLYLINE" else 3
        points = [[float(coords[i]), float(coords[i + 1])] for i in range(0, len(coords) - step + 1, step)]
        return {"type": kind, "closed": bool(self._safe_get(obj, "Closed", False)), "points": points}

    def drawing_units(self) -> int:
        try:
            return int(self._doc().GetVariable("INSUNITS"))
        except Exception:
            return 0

    def diagnose(self) -> dict:
        checks: list[dict] = []
        try:
            app = self._connect(force=True)
            checks.append({"name": "com_connection", "ok": True, "prog_id": self.prog_id})
        except Exception as exc:
            return {
                "success": False,
                "connected": False,
                "checks": [{"name": "com_connection", "ok": False, "error": str(exc)}],
            }
        try:
            version = str(self._safe_get(app, "Version", "unknown"))
            full_name = str(self._safe_get(app, "FullName", ""))
            checks.append(
                {"name": "application", "ok": True, "version": version, "path": full_name}
            )
        except Exception as exc:
            checks.append({"name": "application", "ok": False, "error": str(exc)})
        try:
            doc_info = self.get_current_document()
            checks.append({"name": "active_document", "ok": True, "document": doc_info})
        except Exception as exc:
            checks.append({"name": "active_document", "ok": False, "error": str(exc)})
        return {
            "success": all(check["ok"] for check in checks),
            "connected": True,
            "checks": checks,
        }

    def get_app_info(self) -> dict:
        app = self._connect()
        return {
            "prog_id": self.prog_id,
            "version": str(self._safe_get(app, "Version", "unknown")),
            "path": str(self._safe_get(app, "FullName", "")),
            "visible": bool(self._safe_get(app, "Visible", True)),
            "caption": str(self._safe_get(app, "Caption", "ZWCAD")),
        }

    def get_current_document(self) -> dict:
        doc = self._doc()
        return {
            "name": str(self._safe_get(doc, "Name", "")),
            "full_name": str(self._safe_get(doc, "FullName", "")),
            "path": str(self._safe_get(doc, "Path", "")),
            "saved": bool(self._safe_get(doc, "Saved", False)),
            "readonly": bool(self._safe_get(doc, "ReadOnly", False)),
            "active_layout": str(self._safe_get(self._safe_get(doc, "ActiveLayout"), "Name", "")),
        }

    def list_documents(self) -> list[dict]:
        app = self._connect()
        results = []
        for doc in self._iter_collection(app.Documents):
            results.append(
                {
                    "name": str(self._safe_get(doc, "Name", "")),
                    "full_name": str(self._safe_get(doc, "FullName", "")),
                    "saved": bool(self._safe_get(doc, "Saved", False)),
                    "readonly": bool(self._safe_get(doc, "ReadOnly", False)),
                }
            )
        return results

    def scan_cad_folder(self, path: str, recursive: bool = False) -> dict:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            raise ValidationError(f"Path does not exist: {path}")
        if not root.is_dir():
            raise ValidationError(f"Path is not a directory: {path}")

        cad_extensions = {".dwg", ".dxf", ".dwt"}
        files = []
        iterator = root.rglob("*") if recursive else root.iterdir()
        for item in iterator:
            if item.is_file() and item.suffix.lower() in cad_extensions:
                files.append({"name": item.name, "path": str(item)})
        files.sort(key=lambda f: f["path"])
        return {
            "folder": str(root),
            "recursive": recursive,
            "count": len(files),
            "files": files,
        }

    def open_document(self, path: str, read_only: bool = False) -> dict:
        self._connect()
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise ValidationError(f"File does not exist: {path}")
        pending = find_process_dialogs(self._main_hwnd)
        if pending:
            titles = ", ".join(repr(d.get("title")) for d in pending)
            raise ModalDialogError(f"ZWCAD shows a modal dialog: {titles}", pending)
        watchdog = DialogWatchdog(self._main_hwnd, auto_dismiss=self.auto_dismiss_dialogs)
        pythoncom, win32_client, prog_id = self._pythoncom, self._win32_client, self.prog_id
        guard_sysvars = self.GUARD_SYSVARS
        safe_get = self._safe_get

        def work() -> dict:
            # Worker thread: own COM apartment and own proxy to the running ZWCAD (FINDINGS #37).
            pythoncom.CoInitialize()
            try:
                app = win32_client.GetActiveObject(prog_id)
                active = safe_get(app, "ActiveDocument")
                saved: dict[str, Any] = {}
                for name, value in guard_sysvars:
                    try:
                        old = active.GetVariable(name)
                        if old != value:
                            active.SetVariable(name, value)
                            saved[name] = old
                    except Exception:
                        pass
                watchdog.__enter__()
                try:
                    try:
                        doc = app.Documents.Open(str(target), read_only)
                    except Exception as exc:
                        raise ValidationError(f"Cannot open document: {path} ({exc})") from exc
                    data = {
                        "name": str(safe_get(doc, "Name", "")),
                        "full_name": str(safe_get(doc, "FullName", "")),
                        "path": str(safe_get(doc, "Path", "")),
                        "saved": bool(safe_get(doc, "Saved", False)),
                        "readonly": bool(safe_get(doc, "ReadOnly", False)),
                    }
                finally:
                    watchdog.__exit__(None, None, None)
                    for name, old in saved.items():
                        try:
                            active.SetVariable(name, old)
                        except Exception:
                            pass
                data["dialogs"] = {**watchdog.report(), "sysvars_overridden": sorted(saved)}
                self.last_dialog_report = data["dialogs"]
                return data
            finally:
                pythoncom.CoUninitialize()

        return self._runner.run("open_document", work)

    def job_status(self, job_id: str) -> dict:
        return self._runner.status(job_id)

    def close_document(self, name: str, save_changes: bool = False) -> dict:
        app = self._connect()
        try:
            doc = app.Documents.Item(name)
        except Exception as exc:
            raise ValidationError(f"Document not found: {name}") from exc
        try:
            doc.Close(save_changes)
        except Exception as exc:
            raise ValidationError(f"Cannot close document: {name}") from exc
        return {"closed": True, "name": name, "save_changes": save_changes}

    def activate_document(self, name: str) -> dict:
        app = self._connect()
        try:
            doc = app.Documents.Item(name)
            doc.Activate()
        except Exception as exc:
            raise ValidationError(f"Document not found or cannot be activated: {name}") from exc
        return self.get_current_document()

    def save_document(self, file_path: str | None = None) -> dict:
        doc = self._doc()
        if bool(self._safe_get(doc, "ReadOnly", False)):
            raise ValidationError("Current document is read-only.")
        if file_path:
            path = str(Path(file_path).expanduser().resolve())
            doc.SaveAs(path)
        else:
            current = str(self._safe_get(doc, "FullName", ""))
            if not current:
                raise ValidationError("The document has no file path; provide file_path for SaveAs.")
            doc.Save()
            path = current
        return {"saved": True, "file_path": path}

    def list_layers(self, detail: bool = True) -> list[dict]:
        doc = self._doc()
        layers = []
        for layer in self._iter_collection(doc.Layers):
            item = {
                "name": str(self._safe_get(layer, "Name", "")),
                "color": int(self._safe_get(layer, "Color", 0)),
            }
            if detail:
                item.update(
                    {
                        "linetype": str(self._safe_get(layer, "Linetype", "")),
                        "on": bool(self._safe_get(layer, "LayerOn", True)),
                        "locked": bool(self._safe_get(layer, "Lock", False)),
                        "frozen": bool(self._safe_get(layer, "Freeze", False)),
                    }
                )
            layers.append(item)
        return layers

    def ensure_layers(self, layers: list[LayerSpec]) -> list[dict]:
        doc = self._doc()
        # Names only: reading 5 properties of every layer was the slowest part on 300-layer maps.
        existing = {item["name"].lower(): item for item in self.list_layers(detail=False)}
        results: list[dict] = []
        with self._undo_group():
            for spec in layers:
                key = spec.name.lower()
                created = key not in existing
                try:
                    layer = doc.Layers.Add(spec.name) if created else doc.Layers.Item(existing[key]["name"])
                    changes = []
                    if spec.color is not None:
                        layer.Color = spec.color
                        changes.append("color")
                    if spec.linetype is not None:
                        layer.Linetype = spec.linetype
                        changes.append("linetype")
                    if spec.on is not None:
                        layer.LayerOn = spec.on
                        changes.append("on")
                    if spec.locked is not None:
                        layer.Lock = spec.locked
                        changes.append("locked")
                    if spec.frozen is not None:
                        layer.Freeze = spec.frozen
                        changes.append("frozen")
                    results.append(
                        {
                            "name": spec.name,
                            "success": True,
                            "created": created,
                            "updated_properties": changes,
                        }
                    )
                except Exception as exc:
                    results.append({"name": spec.name, "success": False, "error": str(exc)})
        return results

    def audit_drawing(self, sample_limit: int) -> dict:
        doc = self._doc()
        type_counts: Counter[str] = Counter()
        layer_counts: Counter[str] = Counter()
        block_counts: Counter[str] = Counter()
        samples: list[dict] = []
        total = 0
        for layout_name, space in self._spaces("all_layouts"):
            for entity in self._iter_collection(space):
                total += 1
                entity_type = self._entity_type(entity)
                layer = str(self._safe_get(entity, "Layer", ""))
                type_counts[entity_type] += 1
                layer_counts[layer] += 1
                if entity_type == "block_reference":
                    block_counts[self._block_name(entity) or "<unknown>"] += 1
                if len(samples) < sample_limit:
                    samples.append(self._entity_summary(entity, layout_name))
        return {
            "document": self.get_current_document(),
            "entity_total": total,
            "entity_types": dict(type_counts.most_common()),
            "entities_by_layer": dict(layer_counts.most_common()),
            "block_references": dict(block_counts.most_common()),
            "layer_count": int(self._safe_get(doc.Layers, "Count", 0)),
            "layout_count": int(self._safe_get(doc.Layouts, "Count", 0)),
            "sample_entities": samples,
        }

    def get_selected_entities(self, limit: int) -> list[dict]:
        doc = self._doc()
        try:
            selection = doc.PickfirstSelectionSet
        except Exception:
            return []
        results = []
        for index, entity in enumerate(self._iter_collection(selection)):
            if index >= limit:
                break
            item = self._entity_summary(entity)
            item["space"] = self._get_space(entity)
            bounds = self._try_get_bounding_box(entity)
            if bounds is not None:
                item["bounding_box"] = bounds
            results.append(item)
        return results

    def query_entities(self, query: EntityQuerySpec) -> list[dict]:
        results: list[dict] = []
        target_block = query.block_name.lower() if query.block_name else None
        target_text = query.text_contains.lower() if query.text_contains else None
        for layout_name, space in self._spaces(query.scope):
            for entity in self._iter_collection(space):
                types = query.type_names()
                if types and not any(self._match_type(entity, t) for t in types):
                    continue
                if not query.matches_layer(str(self._safe_get(entity, "Layer", ""))):
                    continue
                if target_block and (self._block_name(entity) or "").lower() != target_block:
                    continue
                if target_text:
                    text = str(self._safe_get(entity, "TextString", "")).lower()
                    if target_text not in text:
                        continue
                results.append(self._entity_summary(entity, layout_name))
                if len(results) >= query.limit:
                    return results
        return results

    def get_entity_details(self, handles: list[str]) -> list[dict]:
        results = []
        for handle in handles:
            try:
                results.append({"success": True, **self._entity_details(self._handle_to_object(handle))})
            except Exception as exc:
                results.append({"success": False, "handle": handle, "error": str(exc)})
        return results

    def update_entity_properties(self, handles: list[str], properties: dict[str, Any]) -> list[dict]:
        mapping = {
            "layer": "Layer",
            "color": "Color",
            "linetype": "Linetype",
            "linetype_scale": "LinetypeScale",
            "lineweight": "Lineweight",
            "visible": "Visible",
        }
        results = []
        with self._undo_group():
            for handle in handles:
                try:
                    entity = self._handle_to_object(handle)
                    before = self._entity_summary(entity)
                    changed = []
                    for key, value in properties.items():
                        if value is None:
                            continue
                        setattr(entity, mapping[key], value)
                        changed.append(key)
                    update = self._safe_get(entity, "Update")
                    if callable(update):
                        update()
                    results.append(
                        {
                            "success": True,
                            "handle": handle,
                            "changed": changed,
                            "before": before,
                            "after": self._entity_summary(entity),
                        }
                    )
                except Exception as exc:
                    results.append({"success": False, "handle": handle, "error": str(exc)})
        return results

    def transform_entities(self, action: str, handles: list[str], params: dict[str, Any]) -> list[dict]:
        results = []
        with self._undo_group():
            for handle in handles:
                try:
                    entity = self._handle_to_object(handle)
                    new_entity = None
                    if action in {"move", "copy"}:
                        from_point = self._point(params["from"])
                        to_point = self._point(params["to"])
                        target = entity.Copy() if action == "copy" else entity
                        target.Move(from_point, to_point)
                        new_entity = target if action == "copy" else None
                    elif action == "rotate":
                        entity.Rotate(self._point(params["base_point"]), float(params["angle_radians"]))
                    elif action == "scale":
                        entity.ScaleEntity(self._point(params["base_point"]), float(params["factor"]))
                    elif action == "mirror":
                        new_entity = entity.Mirror(self._point(params["point1"]), self._point(params["point2"]))
                        if not bool(params.get("keep_original", True)):
                            entity.Delete()
                    else:
                        raise UnsupportedOperationError(f"Unsupported transform action: {action}")
                    item = {"success": True, "handle": handle, "action": action}
                    if new_entity is not None:
                        item["new_handle"] = str(self._safe_get(new_entity, "Handle", ""))
                    results.append(item)
                except Exception as exc:
                    results.append({"success": False, "handle": handle, "action": action, "error": str(exc)})
        return results

    def delete_entities(self, handles: list[str]) -> list[dict]:
        results = []
        with self._undo_group():
            for handle in handles:
                try:
                    entity = self._handle_to_object(handle)
                    summary = self._entity_summary(entity)
                    entity.Delete()
                    results.append({"success": True, "handle": handle, "deleted": summary})
                except Exception as exc:
                    results.append({"success": False, "handle": handle, "error": str(exc)})
        return results

    @staticmethod
    def _require(params: dict[str, Any], names: Iterable[str], entity_type: str) -> None:
        missing = [name for name in names if name not in params]
        if missing:
            raise ValidationError(
                f"{entity_type} is missing required parameters: {', '.join(missing)}"
            )

    def _apply_layer(self, entity: Any, layer: str) -> None:
        if layer:
            entity.Layer = layer

    def _create_entity(self, space: Any, spec: EntityCreateSpec) -> Any:
        p = spec.params
        et = spec.entity_type
        if et == "line":
            self._require(p, ("start", "end"), et)
            entity = space.AddLine(self._point(p["start"]), self._point(p["end"]))
        elif et == "circle":
            self._require(p, ("center", "radius"), et)
            entity = space.AddCircle(self._point(p["center"]), float(p["radius"]))
        elif et == "arc":
            self._require(p, ("center", "radius", "start_angle", "end_angle"), et)
            entity = space.AddArc(
                self._point(p["center"]),
                float(p["radius"]),
                float(p["start_angle"]),
                float(p["end_angle"]),
            )
        elif et == "lwpolyline":
            self._require(p, ("vertices",), et)
            flattened, bulges = [], []
            for point in p["vertices"]:
                flattened.extend([float(point[0]), float(point[1])])
                bulges.append(float(point[2]) if len(point) > 2 and point[2] is not None else 0.0)
            if "bulges" in p:                      # alternative: separate list, one value per vertex
                bulges = [float(b) for b in p["bulges"]] + [0.0] * max(0, len(bulges) - len(p["bulges"]))
            entity = space.AddLightWeightPolyline(self._double_array(flattened))
            for index, bulge in enumerate(bulges):
                if bulge:
                    entity.SetBulge(index, bulge)
            if "closed" in p:
                entity.Closed = bool(p["closed"])
        elif et == "text":
            self._require(p, ("text", "insertion_point", "height"), et)
            entity = space.AddText(
                str(p["text"]), self._point(p["insertion_point"]), float(p["height"])
            )
            if "rotation" in p:
                entity.Rotation = float(p["rotation"])
        elif et == "mtext":
            self._require(p, ("text", "insertion_point", "width"), et)
            entity = space.AddMText(
                self._point(p["insertion_point"]), float(p["width"]), str(p["text"])
            )
            if "height" in p and hasattr(entity, "Height"):
                entity.Height = float(p["height"])
        elif et == "block":
            self._require(p, ("name", "insertion_point"), et)
            scales = p.get("scale", [1.0, 1.0, 1.0])
            entity = space.InsertBlock(
                self._point(p["insertion_point"]),
                str(p["name"]),
                float(scales[0]),
                float(scales[1]),
                float(scales[2]),
                float(p.get("rotation", 0.0)),
            )
        elif et == "dimension_aligned":
            self._require(p, ("point1", "point2", "text_point"), et)
            entity = space.AddDimAligned(
                self._point(p["point1"]), self._point(p["point2"]), self._point(p["text_point"])
            )
        elif et == "dimension_rotated":
            self._require(p, ("point1", "point2", "text_point", "angle_radians"), et)
            entity = space.AddDimRotated(
                self._point(p["point1"]),
                self._point(p["point2"]),
                self._point(p["text_point"]),
                float(p["angle_radians"]),
            )
        elif et == "dimension_radial":
            self._require(p, ("center", "chord_point", "leader_length"), et)
            entity = space.AddDimRadial(
                self._point(p["center"]),
                self._point(p["chord_point"]),
                float(p["leader_length"]),
            )
        elif et == "dimension_diametric":
            self._require(p, ("chord_point", "far_chord_point", "leader_length"), et)
            entity = space.AddDimDiametric(
                self._point(p["chord_point"]),
                self._point(p["far_chord_point"]),
                float(p["leader_length"]),
            )
        else:
            raise UnsupportedOperationError(f"Unsupported entity type: {et}")
        self._apply_layer(entity, spec.layer)
        return entity

    def create_entities(self, entities: list[EntityCreateSpec]) -> list[dict]:
        space = self._doc().ModelSpace
        results = []
        with self._undo_group():
            for index, spec in enumerate(entities):
                try:
                    entity = self._create_entity(space, spec)
                    results.append(
                        {
                            "success": True,
                            "index": index,
                            "entity_type": spec.entity_type,
                            "handle": str(self._safe_get(entity, "Handle", "")),
                            "layer": spec.layer,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "success": False,
                            "index": index,
                            "entity_type": spec.entity_type,
                            "error": str(exc),
                        }
                    )
        return results

    def list_layouts(self) -> list[dict]:
        doc = self._doc()
        results = []
        active_name = str(self._safe_get(self._safe_get(doc, "ActiveLayout"), "Name", ""))
        for layout in self._iter_collection(doc.Layouts):
            name = str(self._safe_get(layout, "Name", ""))
            results.append(
                {
                    "name": name,
                    "tab_order": int(self._safe_get(layout, "TabOrder", 0)),
                    "model_space": name.lower() == "model",
                    "active": name == active_name,
                    "config_name": str(self._safe_get(layout, "ConfigName", "")),
                    "canonical_media_name": str(self._safe_get(layout, "CanonicalMediaName", "")),
                    "plot_type": self._safe_get(layout, "PlotType"),
                }
            )
        return sorted(results, key=lambda item: item["tab_order"])

    def get_layout_plot_settings(self, layout_name: str | None) -> dict:
        doc = self._doc()
        try:
            if layout_name:
                layout = doc.Layouts.Item(layout_name)
            else:
                layout = self._safe_get(doc, "ActiveLayout")
            if layout is None:
                raise ValidationError("No active layout or layout not found.")
        except Exception as exc:
            raise ValidationError(f"Layout not found: {layout_name}") from exc

        plot_type_raw = self._safe_get(layout, "PlotType")
        plot_type_map = {
            0: "display",
            1: "extents",
            2: "limits",
            3: "view",
            4: "window",
            5: "layout",
        }
        paper_units_raw = self._safe_get(layout, "PaperUnits")
        paper_units_map = {0: "inches", 1: "millimeters", 2: "pixels"}
        rotation_raw = self._safe_get(layout, "PlotRotation")
        rotation_map = {0: "0_degrees", 1: "90_degrees", 2: "180_degrees", 3: "270_degrees"}

        settings = {
            "name": str(self._safe_get(layout, "Name", "")),
            "config_name": str(self._safe_get(layout, "ConfigName", "")),
            "canonical_media_name": str(self._safe_get(layout, "CanonicalMediaName", "")),
            "plot_device": str(self._safe_get(layout, "ConfigName", "")),
            "plot_type": plot_type_map.get(plot_type_raw, plot_type_raw),
            "paper_units": paper_units_map.get(paper_units_raw, paper_units_raw),
            "plot_scale": self._json_value(self._safe_get(layout, "PlotScale")),
            "std_scale_name": str(self._safe_get(layout, "StdScaleName", "")),
            "std_scale": self._json_value(self._safe_get(layout, "StdScale")),
            "plot_rotation": rotation_map.get(rotation_raw, rotation_raw),
            "plot_origin": self._json_value(self._safe_get(layout, "PlotOrigin")),
            "center_plot": bool(self._safe_get(layout, "CenterPlot", False)),
            "plot_hidden": bool(self._safe_get(layout, "PlotHidden", False)),
            "plot_with_lineweights": bool(self._safe_get(layout, "PlotWithLineweights", False)),
            "plot_with_plot_styles": bool(self._safe_get(layout, "PlotWithPlotStyles", False)),
            "scale_lineweights": bool(self._safe_get(layout, "ScaleLineweights", False)),
            "use_standard_scale": bool(self._safe_get(layout, "UseStandardScale", False)),
            "plot_style_sheet": str(self._safe_get(layout, "PlotStyleSheet", "")),
            "style_sheet": str(self._safe_get(layout, "StyleSheet", "")),
        }
        try:
            lower_left = self._safe_get(layout, "PlotWindowLowerLeft")
            upper_right = self._safe_get(layout, "PlotWindowUpperRight")
            if lower_left is not None and upper_right is not None:
                settings["window_lower_left"] = self._json_value(lower_left)
                settings["window_upper_right"] = self._json_value(upper_right)
        except Exception:
            pass
        try:
            view_name = self._safe_get(layout, "ViewToPlot")
            if view_name is not None:
                settings["view_to_plot"] = str(view_name)
        except Exception:
            pass
        return settings

    def get_plot_capabilities(self) -> dict:
        doc = self._doc()
        app = self._connect()
        plot = self._safe_get(doc, "Plot")

        devices = self._list_plot_devices(app)
        if not devices:
            devices = self._scan_pc5_files()

        default_config = "DWG to PDF.pc5"
        media_names = []
        if plot is not None:
            media_names = self._list_media_names(plot, default_config)
        if not media_names:
            media_names = [
                "A0",
                "A1",
                "A2",
                "A3",
                "A4",
                "A0+",
                "ANSI_A",
                "ANSI_B",
                "ANSI_C",
                "ANSI_D",
                "ANSI_E",
                "ISO_A0",
                "ISO_A1",
                "ISO_A2",
                "ISO_A3",
                "ISO_A4",
            ]

        return {
            "devices": devices,
            "default_device": default_config,
            "paper_sizes": media_names,
            "supported_extensions": ["pdf", "dwf", "dwfx", "dxf", "png", "jpg", "jpeg"],
            "note": "Device list falls back to known PC5 files if COM does not expose GetPlotDevices.",
        }

    def activate_layout(self, name: str) -> dict:
        doc = self._doc()
        layout = self._resolve_layout(doc, name)
        doc.ActiveLayout = layout
        return {"active_layout": str(self._safe_get(doc.ActiveLayout, "Name", name))}

    def _resolve_layout(self, doc: Any, layout_name: str | None) -> Any:
        if layout_name:
            try:
                return doc.Layouts.Item(layout_name)
            except Exception as exc:
                raise ValidationError(f"Layout not found: {layout_name}") from exc
        return self._safe_get(doc, "ActiveLayout")

    def _bounds_for_scope(self, scope_type: str, layout: Any, request: PlotScopeRequest) -> dict:
        doc = self._doc()
        if scope_type in {"display", "view"}:
            return self._bounds_from_current_view(doc)
        if scope_type == "extents":
            return self.get_drawing_extents()
        if scope_type == "limits":
            try:
                return self._bounds_from_corners(doc.GetVariable("LIMMIN"), doc.GetVariable("LIMMAX"))
            except Exception:
                return self._merge_bounds([])
        if scope_type == "window":
            lower_left = request.window_lower_left or self._json_value(self._safe_get(layout, "PlotWindowLowerLeft"))
            upper_right = request.window_upper_right or self._json_value(self._safe_get(layout, "PlotWindowUpperRight"))
            if lower_left and upper_right:
                return {"min": lower_left, "max": upper_right}
            return self._merge_bounds([])
        if scope_type == "layout":
            try:
                min_point = self._safe_get(layout, "PaperMin")
                max_point = self._safe_get(layout, "PaperMax")
                if min_point and max_point:
                    return self._bounds_from_corners(min_point, max_point)
            except Exception:
                pass
            return self._merge_bounds([])
        return self._merge_bounds([])

    def _bounds_from_current_view(self, doc: Any) -> dict:
        try:
            viewport = self._safe_get(doc, "ActivePViewport")
            if viewport is not None:
                center = self._safe_get(viewport, "Center")
                height = float(self._safe_get(viewport, "Height", 1.0))
                width = float(self._safe_get(viewport, "Width", height))
                if center is not None:
                    cx, cy = float(center[0]), float(center[1])
                    return {"min": [cx - width / 2, cy - height / 2, 0.0], "max": [cx + width / 2, cy + height / 2, 0.0]}
        except Exception:
            pass
        try:
            viewport = self._safe_get(doc.Application, "ActiveViewport")
            center = self._safe_get(viewport, "Center")
            height = float(self._safe_get(viewport, "Height", 1.0))
            width = float(self._safe_get(viewport, "Width", height))
            if center is not None:
                cx, cy = float(center[0]), float(center[1])
                return {"min": [cx - width / 2, cy - height / 2, 0.0], "max": [cx + width / 2, cy + height / 2, 0.0]}
        except Exception:
            pass
        return {"min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 0.0]}

    def preview_plot_scope(self, request: PlotScopeRequest) -> dict:
        doc = self._doc()
        layout = self._resolve_layout(doc, request.layout_name)
        if layout is None:
            raise ValidationError("No active layout or layout not found.")

        layout_settings = self.get_layout_plot_settings(request.layout_name)
        scope_type = request.scope_type or layout_settings.get("plot_type", "layout")
        if isinstance(scope_type, int):
            type_map = {0: "display", 1: "extents", 2: "limits", 3: "view", 4: "window", 5: "layout"}
            scope_type = type_map.get(scope_type, "layout")

        bounds = self._bounds_for_scope(scope_type, layout, request)
        drawing_extents = self.get_drawing_extents()
        try:
            mn = bounds.get("min", [0, 0, 0])
            mx = bounds.get("max", [0, 0, 0])
            area = max(0.0, (mx[0] - mn[0]) * (mx[1] - mn[1])) if len(mn) >= 2 and len(mx) >= 2 else 0.0
            dx_mn = drawing_extents.get("min", [0, 0, 0])
            dx_mx = drawing_extents.get("max", [0, 0, 0])
            drawing_area = max(0.0, (dx_mx[0] - dx_mn[0]) * (dx_mx[1] - dx_mn[1])) if len(dx_mn) >= 2 and len(dx_mx) >= 2 else 0.0
            clipping_risk = (
                area > 0
                and drawing_area > 0
                and (area < drawing_area * 0.9)
                and scope_type in {"display", "window", "limits", "view"}
            )
        except Exception:
            clipping_risk = False

        return {
            "layout": layout_settings.get("name"),
            "scope_type": scope_type,
            "bounds": bounds,
            "drawing_extents": drawing_extents,
            "clipping_risk": clipping_risk,
            "layout_settings": layout_settings,
        }

    def get_current_view(self) -> dict:
        doc = self._doc()
        bounds = self._bounds_from_current_view(doc)
        try:
            viewport = self._safe_get(doc, "ActivePViewport")
            if viewport is not None:
                return {
                    "type": "paper_viewport",
                    "center": self._json_value(self._safe_get(viewport, "Center")),
                    "height": float(self._safe_get(viewport, "Height", 1.0)),
                    "width": float(self._safe_get(viewport, "Width", 1.0)),
                    "bounds": bounds,
                    "twist_angle": float(self._safe_get(viewport, "TwistAngle", 0.0)),
                }
        except Exception:
            pass
        try:
            viewport = self._safe_get(doc.Application, "ActiveViewport")
            if viewport is not None:
                return {
                    "type": "viewport",
                    "center": self._json_value(self._safe_get(viewport, "Center")),
                    "height": float(self._safe_get(viewport, "Height", 1.0)),
                    "width": float(self._safe_get(viewport, "Width", 1.0)),
                    "bounds": bounds,
                    "twist_angle": float(self._safe_get(viewport, "TwistAngle", 0.0)),
                }
        except Exception:
            pass
        return {"type": "unknown", "bounds": bounds}

    def get_drawing_extents(self) -> dict:
        doc = self._doc()
        try:
            extents = self._safe_get(doc, "Extents")
            if extents is not None:
                return self._bounds_from_corners(extents.minPoint, extents.maxPoint)
        except Exception:
            pass
        try:
            min_point = self._safe_get(doc, "MinPoint")
            max_point = self._safe_get(doc, "MaxPoint")
            if min_point and max_point:
                return self._bounds_from_corners(min_point, max_point)
        except Exception:
            pass

        bounds: list[dict] = []
        for layout_name, space in self._spaces("all_layouts"):
            for entity in self._iter_collection(space):
                box = self._try_get_bounding_box(entity)
                if box is not None:
                    bounds.append(box)
        if bounds:
            return self._merge_bounds(bounds)
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}

        doc = self._doc()
        try:
            layout = doc.Layouts.Item(name)
            doc.ActiveLayout = layout
            if name.lower() != "model":
                doc.ActiveSpace = 0
        except Exception as exc:
            raise ValidationError(f"Layout cannot be activated: {name}") from exc
        return {"active_layout": name}

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
    ) -> list[dict]:
        doc = self._doc()
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        original = str(self._safe_get(self._safe_get(doc, "ActiveLayout"), "Name", ""))
        results = []
        for name in layout_names:
            try:
                self.activate_layout(name)
                layout = self._safe_get(doc, "ActiveLayout")
                original_settings: dict[str, Any] = {}
                try:
                    original_settings = self.get_layout_plot_settings(name)
                except Exception:
                    pass
                self._apply_plot_scope_to_layout(
                    layout,
                    scope_type=scope_type,
                    window_lower_left=window_lower_left,
                    window_upper_right=window_upper_right,
                    selected_handles=selected_handles,
                    center_plot=center_plot,
                    fit_to_paper=fit_to_paper,
                    custom_scale=custom_scale,
                )
                safe_name = "".join(c if c not in '<>:"/\\|?*' else "_" for c in name)
                file_path = output / f"{Path(str(self._safe_get(doc, 'Name', 'drawing'))).stem}-{safe_name}.{extension.lstrip('.')}"
                config = plot_configuration or str(
                    self._safe_get(self._safe_get(doc, "ActiveLayout"), "ConfigName", "")
                )
                with self._dialog_guard(doc):
                    plotted = doc.Plot.PlotToFile(str(file_path), config)
                exists = file_path.exists() and file_path.stat().st_size > 0
                if plotted is False or not exists:
                    raise RuntimeError(
                        f"PlotToFile returned {plotted!r}; output file "
                        f"{'missing' if not file_path.exists() else 'empty'}: {file_path}"
                    )
                results.append({
                    "dialogs": self.last_dialog_report,
                    "success": True,
                    "layout": name,
                    "file_path": str(file_path),
                    "plot_configuration": config,
                    "scope_applied": scope_type is not None,
                })
                if override_policy == "temporary":
                    self._restore_plot_settings(layout, original_settings)
            except Exception as exc:
                results.append({"success": False, "layout": name, "error": str(exc)})
        if original:
            try:
                self.activate_layout(original)
            except Exception:
                pass
        return results

    def _apply_plot_scope_to_layout(
        self,
        layout: Any,
        scope_type: str | None,
        window_lower_left: list[float] | None,
        window_upper_right: list[float] | None,
        selected_handles: list[str] | None,
        center_plot: bool | None,
        fit_to_paper: bool | None,
        custom_scale: float | None,
    ) -> None:
        if layout is None:
            return
        plot_type_map = {
            "display": 0,
            "extents": 1,
            "limits": 2,
            "view": 3,
            "window": 4,
            "layout": 5,
        }
        if scope_type is not None and scope_type in plot_type_map:
            try:
                layout.PlotType = plot_type_map[scope_type]
            except Exception:
                pass
        if window_lower_left and window_upper_right:
            try:
                layout.PlotWindowLowerLeft = self._point(window_lower_left)
                layout.PlotWindowUpperRight = self._point(window_upper_right)
            except Exception:
                pass
        if selected_handles and scope_type in {None, "window"}:
            try:
                layout.PlotType = plot_type_map["window"]
                handles = selected_handles
                # ZWCAD does not expose a direct SetPlotWindowFromHandles API; leave the window as-is
                # and record the handles for the caller.
                layout.SetWindowToPlot = handles  # best-effort attempt
            except Exception:
                pass
        if center_plot is not None:
            try:
                layout.CenterPlot = bool(center_plot)
            except Exception:
                pass
        if fit_to_paper is not None:
            try:
                layout.UseStandardScale = True
                layout.StdScale = -1  # Fit to paper in many AutoCAD-compatible APIs
                layout.PlotScale = [0, 1]  # 0:1 is often "fit to paper"
            except Exception:
                pass
        if custom_scale is not None and custom_scale > 0:
            try:
                layout.UseStandardScale = False
                layout.PlotScale = [1, custom_scale]
            except Exception:
                pass

    def _restore_plot_settings(self, layout: Any, original: dict[str, Any]) -> None:
        if not layout or not original:
            return
        plot_type_map = {
            "display": 0,
            "extents": 1,
            "limits": 2,
            "view": 3,
            "window": 4,
            "layout": 5,
        }
        try:
            plot_type = original.get("plot_type")
            if isinstance(plot_type, str) and plot_type in plot_type_map:
                layout.PlotType = plot_type_map[plot_type]
            elif isinstance(plot_type, int):
                layout.PlotType = plot_type
        except Exception:
            pass
        for key, prop in (
            ("center_plot", "CenterPlot"),
            ("use_standard_scale", "UseStandardScale"),
            ("plot_scale", "PlotScale"),
            ("std_scale", "StdScale"),
        ):
            value = original.get(key)
            if value is not None:
                try:
                    setattr(layout, prop, value)
                except Exception:
                    pass

    def export_drawing(self, base_file_path: str, extension: str) -> dict:
        doc = self._doc()
        selection_name = f"ZWCAD_MCP_EXPORT_{uuid.uuid4().hex[:8]}"
        selection = None
        try:
            selection = doc.SelectionSets.Add(selection_name)
            selection.Select(5)
            doc.Export(str(Path(base_file_path).expanduser().resolve()), extension.upper(), selection)
            return {"success": True, "base_file_path": base_file_path, "extension": extension.upper()}
        finally:
            if selection is not None:
                try:
                    selection.Delete()
                except Exception:
                    pass

    def verify_export_files(
        self,
        file_paths: list[str],
        layout_names: list[str] | None = None,
        min_size_bytes: int = 1024,
        expected_plot_range: dict[str, list[float]] | None = None,
    ) -> dict:
        def _detect_signature(path: Path) -> dict:
            signatures = {
                ".pdf": (b"%PDF", "PDF"),
                ".dxf": (None, "DXF"),  # textual, checked separately
                ".dwg": (b"AC", "DWG"),
                ".step": (b"ISO-10303-21", "STEP"),
                ".stp": (b"ISO-10303-21", "STEP"),
                ".stl": (b"solid ", "STL (ASCII)"),
                ".3mf": (b"PK", "3MF (ZIP)"),
                ".png": (b"\x89PNG", "PNG"),
                ".jpg": (b"\xff\xd8", "JPEG"),
                ".jpeg": (b"\xff\xd8", "JPEG"),
            }
            ext = path.suffix.lower()
            marker, label = signatures.get(ext, (None, ext.upper().lstrip(".") or "unknown"))
            try:
                with open(path, "rb") as fh:
                    head = fh.read(16)
            except Exception as exc:
                return {"signature_ok": False, "detected_format": None, "error": str(exc)}
            if ext == ".dxf":
                is_ascii = head.startswith(b"  0\n") or head.startswith(b"0\n") or b"SECTION" in head[:64]
                return {"signature_ok": is_ascii, "detected_format": "DXF" if is_ascii else "unknown"}
            if marker is None:
                return {"signature_ok": True, "detected_format": label, "note": "no signature check for this extension"}
            ok = head.startswith(marker)
            return {"signature_ok": ok, "detected_format": label if ok else "unknown"}

        results = []
        for raw_path in file_paths:
            path = Path(raw_path).expanduser().resolve()
            item = {
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "extension": path.suffix.lower(),
                "layout_check": None,
                "size_check": None,
                "range_check": None,
            }
            if item["exists"]:
                item.update(_detect_signature(path))
                item["size_check"] = item["size_bytes"] >= min_size_bytes
                if layout_names is not None:
                    stem = path.stem
                    item["layout_check"] = any(layout in stem for layout in layout_names)
                if expected_plot_range is not None:
                    expected_min = expected_plot_range.get("min", [0, 0, 0])
                    expected_max = expected_plot_range.get("max", [0, 0, 0])
                    # Without reading the file content, we can only verify that a non-zero range was expected.
                    area = 0.0
                    if len(expected_min) >= 2 and len(expected_max) >= 2:
                        area = max(0.0, (expected_max[0] - expected_min[0]) * (expected_max[1] - expected_min[1]))
                    item["range_check"] = area > 0.0
            else:
                item.update({"signature_ok": False, "detected_format": None, "error": "file not found"})
                item["size_check"] = False
                if layout_names is not None:
                    item["layout_check"] = False
                item["range_check"] = False
            results.append(item)

        existing = [r for r in results if r["exists"]]
        verified = [r for r in results if r.get("signature_ok") and r.get("size_check") and r.get("layout_check") is not False and r.get("range_check") is not False]
        return {
            "total": len(results),
            "existing": len(existing),
            "verified": len(verified),
            "missing": [r["path"] for r in results if not r["exists"]],
            "failed": [r["path"] for r in results if r["exists"] and r not in verified],
            "results": results,
        }

    def list_block_definitions(self, detail: bool) -> list[dict]:
        doc = self._doc()
        results = []
        for block in self._iter_collection(doc.Blocks):
            item = {
                "name": str(self._safe_get(block, "Name", "")),
                "entity_count": int(self._safe_get(block, "Count", 0)),
                "is_layout": bool(self._safe_get(block, "IsLayout", False)),
                "is_xref": bool(self._safe_get(block, "IsXRef", False)),
            }
            if detail:
                item["origin"] = self._json_value(self._safe_get(block, "Origin"))
                entities = []
                for index, entity in enumerate(self._iter_collection(block)):
                    if index >= 50:
                        item["entities_truncated"] = True
                        break
                    entities.append(self._entity_summary(entity))
                item["entities"] = entities
            results.append(item)
        return results

    def list_block_references(
        self,
        scope: str,
        block_name: str | None,
        has_attributes: bool | None,
        limit: int,
    ) -> list[dict]:
        results = []
        target = block_name.lower() if block_name else None
        for layout_name, space in self._spaces(scope):
            for entity in self._iter_collection(space):
                if self._entity_type(entity) != "block_reference":
                    continue
                actual_name = self._block_name(entity) or ""
                actual_has_attrs = bool(self._safe_get(entity, "HasAttributes", False))
                if target and actual_name.lower() != target:
                    continue
                if has_attributes is not None and actual_has_attrs != has_attributes:
                    continue
                item = self._entity_summary(entity, layout_name)
                item["insertion_point"] = self._json_value(self._safe_get(entity, "InsertionPoint"))
                item["rotation"] = self._safe_get(entity, "Rotation")
                results.append(item)
                if len(results) >= limit:
                    return results
        return results

    def get_block_attributes(self, handles: list[str]) -> list[dict]:
        results = []
        for handle in handles:
            try:
                block_ref = self._handle_to_object(handle)
                if self._entity_type(block_ref) != "block_reference":
                    raise ValidationError(f"Handle {handle} is not a block reference.")
                results.append(
                    {
                        "success": True,
                        "handle": handle,
                        "block_name": self._block_name(block_ref),
                        "attributes": self._read_attributes(block_ref),
                    }
                )
            except Exception as exc:
                results.append({"success": False, "handle": handle, "error": str(exc)})
        return results

    def update_block_attributes(self, updates: list[dict[str, Any]]) -> list[dict]:
        results = []
        with self._undo_group():
            for update in updates:
                handle = str(update["handle"])
                requested = {str(k).upper(): str(v) for k, v in update["attributes"].items()}
                try:
                    block_ref = self._handle_to_object(handle)
                    if self._entity_type(block_ref) != "block_reference":
                        raise ValidationError(f"Handle {handle} is not a block reference.")
                    changed = []
                    unmatched = set(requested)
                    for attr in block_ref.GetAttributes() or []:
                        tag = str(self._safe_get(attr, "TagString", "")).upper()
                        if tag not in requested:
                            continue
                        before = str(self._safe_get(attr, "TextString", ""))
                        attr.TextString = requested[tag]
                        update_fn = self._safe_get(attr, "Update")
                        if callable(update_fn):
                            update_fn()
                        changed.append({"tag": tag, "old_value": before, "new_value": requested[tag]})
                        unmatched.discard(tag)
                    results.append(
                        {
                            "success": True,
                            "handle": handle,
                            "block_name": self._block_name(block_ref),
                            "changed": changed,
                            "unmatched_tags": sorted(unmatched),
                            "verified_attributes": self._read_attributes(block_ref),
                        }
                    )
                except Exception as exc:
                    results.append({"success": False, "handle": handle, "error": str(exc)})
        return results

    def insert_blocks(self, blocks: list[dict[str, Any]]) -> list[dict]:
        space = self._doc().ModelSpace
        results = []
        with self._undo_group():
            for index, item in enumerate(blocks):
                try:
                    scales = item.get("scale", [1.0, 1.0, 1.0])
                    ref = space.InsertBlock(
                        self._point(item["insertion_point"]),
                        str(item["name"]),
                        float(scales[0]),
                        float(scales[1]),
                        float(scales[2]),
                        float(item.get("rotation", 0.0)),
                    )
                    if item.get("layer"):
                        ref.Layer = item["layer"]
                    requested = {str(k).upper(): str(v) for k, v in item.get("attributes", {}).items()}
                    for attr in (ref.GetAttributes() or []) if requested else []:
                        tag = str(self._safe_get(attr, "TagString", "")).upper()
                        if tag in requested:
                            attr.TextString = requested[tag]
                            update_fn = self._safe_get(attr, "Update")
                            if callable(update_fn):
                                update_fn()
                    results.append(
                        {
                            "success": True,
                            "index": index,
                            "handle": str(self._safe_get(ref, "Handle", "")),
                            "block_name": self._block_name(ref),
                        }
                    )
                except Exception as exc:
                    results.append({"success": False, "index": index, "error": str(exc)})
        return results

    # ------------------------------------------------------------------ LISP channel

    def _lisp(self) -> LispChannel:
        if self._lisp_channel is None:
            self._lisp_channel = LispChannel()
        return self._lisp_channel

    def run_lisp(self, expression: str, undo: bool = False, timeout_s: float = 600.0, name: str = "lisp") -> dict:
        """Evaluate one LISP expression in the active document; returns the channel envelope.

        Runs detached (worker thread with its own COM apartment), so a long LISP routine or a dialog it
        opens returns STILL_RUNNING / MODAL_DIALOG_OPEN with a job id instead of blocking the server.
        """
        doc = self._doc()
        try:
            cmdactive = int(doc.GetVariable("CMDACTIVE"))
        except Exception:
            cmdactive = 0
        if cmdactive:
            raise CadBusyError(
                f"ZWCAD is in the middle of a command (CMDACTIVE={cmdactive}). Finish it or press Esc in ZWCAD, then retry."
            )
        pending = find_process_dialogs(self._main_hwnd)
        if pending:
            titles = ", ".join(repr(d.get("title")) for d in pending)
            raise ModalDialogError(f"ZWCAD shows a modal dialog: {titles}", pending)
        channel = self._lisp()
        pythoncom, win32_client, prog_id = self._pythoncom, self._win32_client, self.prog_id

        def work() -> dict:
            pythoncom.CoInitialize()
            try:
                app = win32_client.GetActiveObject(prog_id)
                active = app.ActiveDocument
                return channel.run(active.SendCommand, expression, undo=undo, timeout_s=timeout_s)
            finally:
                pythoncom.CoUninitialize()

        return self._runner.run(name, work)

    def lisp_call_expr(self, expression: str, undo: bool = False, name: str = "lisp", timeout_s: float = 600.0) -> Any:
        envelope = self.run_lisp(expression, undo=undo, timeout_s=timeout_s, name=name)
        if not envelope.get("ok"):
            raise LispChannelError(f"LISP error: {envelope.get('error')}")
        return envelope.get("value")

    def lisp_codepage(self) -> str:
        return self._lisp().codepage

    def lisp_info(self) -> dict:
        channel = self._lisp()
        return {"ipc_dir": str(channel.ipc_dir), "codepage": channel.codepage, "unicode_lisp": channel.last_unicode}

    # ------------------------------------------------------------------ screenshot / offset

    def screenshot(self, region: str = "drawing", max_width: int = 1280) -> dict:
        from .screenshot import capture_window

        if self._main_hwnd is None:
            self._connect()
        if self._main_hwnd is None:
            raise CadConnectionError("ZWCAD window handle is unknown.")
        return capture_window(self._main_hwnd, region=region, max_width=max_width)

    def zoom_view(self, mode: str, lower_left: list[float] | None, upper_right: list[float] | None,
                  handles: list[str] | None, margin: float) -> dict:
        app = self._connect()
        doc = self._doc()
        if mode == "extents":
            app.ZoomExtents()
        else:
            if mode == "entities":
                bounds = []
                for handle in handles or []:
                    try:
                        box = self._try_get_bounding_box(doc.HandleToObject(handle))
                    except Exception:
                        box = None
                    if box:
                        bounds.append(box)
                if not bounds:
                    raise ValidationError("None of the handles has a bounding box.")
                merged = self._merge_bounds(bounds)
                lower_left, upper_right = merged["min"], merged["max"]
            if not lower_left or not upper_right:
                raise ValidationError("window mode needs lower_left and upper_right.")
            dx = (upper_right[0] - lower_left[0]) * margin
            dy = (upper_right[1] - lower_left[1]) * margin
            app.ZoomWindow(
                self._point([lower_left[0] - dx, lower_left[1] - dy, 0.0]),
                self._point([upper_right[0] + dx, upper_right[1] + dy, 0.0]),
            )
        return {"success": True, "mode": mode, "view": self.get_current_view()}

    def offset_entities(self, handles: list[str], distance: float, both_sides: bool, layer: str | None) -> list[dict]:
        doc = self._doc()
        results: list[dict] = []
        distances = [distance, -distance] if both_sides else [distance]
        with self._undo_group():
            for handle in handles:
                try:
                    obj = doc.HandleToObject(handle)
                except Exception as exc:
                    results.append({"handle": handle, "success": False, "error": f"not found: {exc}"})
                    continue
                for dist in distances:
                    try:
                        created = obj.Offset(dist)
                        new_handles = []
                        for item in created if isinstance(created, (list, tuple)) else [created]:
                            if layer:
                                item.Layer = layer
                            new_handles.append(str(item.Handle))
                        results.append({"handle": handle, "distance": dist, "success": True, "new_handles": new_handles})
                    except Exception as exc:
                        results.append({"handle": handle, "distance": dist, "success": False, "error": str(exc)})
        return results

    def begin_undo(self) -> None:
        try:
            self._doc().StartUndoMark()
        except Exception:
            pass

    def end_undo(self) -> None:
        try:
            self._doc().EndUndoMark()
        except Exception:
            pass
