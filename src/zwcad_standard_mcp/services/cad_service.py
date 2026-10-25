from __future__ import annotations

import itertools
import json
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zwcad_standard_mcp.adapters.base import CadAdapter
from zwcad_standard_mcp.config import Settings
from zwcad_standard_mcp.errors import (
    BatchLimitError,
    ReadOnlyDocumentError,
    ValidationError,
    WriteDisabledError,
)
from zwcad_standard_mcp.models import (
    BlockAttributePatch,
    EntityCreateSpec,
    EntityFilter,
    EntityPropertyPatch,
    FilteredPropertyPatch,
    EntityQuerySpec,
    LayerMapRequest,
    AreaTakeoffRequest,
    BoundaryDistanceRequest,
    DimStyleSpec,
    HatchRequest,
    TextStyleSpec,
    LayerSpec,
    LayerStandard,
    PlotScopeRequest,
    TextUpdateRequest,
    TransformRequest,
)
from zwcad_standard_mcp.adapters.selection import _wild
from zwcad_standard_mcp import textops
from zwcad_standard_mcp.permissions.policy import PermissionLevel
from zwcad_standard_mcp.permissions_bridge import confirm_mode, require_permission


# In-memory batch job registry. Jobs are transient; they live only as long as the
# MCP server process. For long-running folder workflows the skill calls
# create_batch_job with files and polls get_batch_job_status.
_BATCH_JOBS: dict[str, dict[str, Any]] = {}
_BATCH_JOB_LOCK = threading.Lock()
_BATCH_JOB_SEQ = itertools.count(1)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job_id() -> str:
    return f"JOB{next(_BATCH_JOB_SEQ):03d}"


def _update_job(job_id: str, **kwargs: Any) -> None:
    with _BATCH_JOB_LOCK:
        job = _BATCH_JOBS.get(job_id)
        if job is None:
            return
        job.update(kwargs)
        job["updated_at"] = _utc_now()


def _execute_batch_job(
    job_id: str,
    settings: Any,
    files: list[str],
    operation: str,
    output_dir: str | None,
    config: dict[str, Any] | None,
) -> None:
    """Background worker: open each file, run the operation, close it.

    Uses a fresh COM adapter instance because COM apartment affinity prevents
    reusing the adapter that was created in the MCP server thread. This worker
    calls adapter methods directly: the user already confirmed the batch when
    calling create_batch_job with confirm=true, so we avoid nested permission
    gates and blocking UI prompts inside the background thread.
    """
    from zwcad_standard_mcp.adapters.com import ComCadAdapter
    import pythoncom

    # Each background worker owns a separate COM apartment.  Without this,
    # the first COM call can fail before the job state is updated, leaving a
    # batch job indefinitely reported as running at 0%.
    pythoncom.CoInitialize()
    adapter = ComCadAdapter(
        prog_id=settings.prog_id if hasattr(settings, "prog_id") else "ZWCAD.Application",
        auto_start=getattr(settings, "auto_start_cad", False),
        auto_dismiss_dialogs=getattr(settings, "auto_dismiss_dialogs", True),
    )
    cfg = config or {}
    out_dir = output_dir or ""
    results: list[dict[str, Any]] = []

    _update_job(job_id, status="running", started_at=_utc_now())

    failed_count = 0
    for index, file_path in enumerate(files, start=1):
        file_result: dict[str, Any] = {
            "index": index,
            "file_path": file_path,
            "success": False,
            "outputs": [],
            "error": None,
        }
        doc_name: str | None = None
        try:
            open_data = adapter.open_document(file_path, read_only=True)
            doc_name = open_data["name"]

            if operation == "plot_pdf":
                layouts = adapter.list_layouts()
                layout_names = [lo["name"] for lo in layouts if not lo.get("model_space")]
                if not layout_names:
                    raise ValidationError("No paper layouts found; nothing to plot.")
                plot_cfg = cfg.get("plot_configuration")
                ext = cfg.get("extension", "pdf")
                plot_results = adapter.plot_layouts(
                    layout_names=layout_names,
                    output_dir=out_dir,
                    plot_configuration=plot_cfg,
                    extension=ext,
                )
                file_result["outputs"] = plot_results
                if any(not r.get("success") for r in plot_results):
                    raise ValidationError("one or more layouts failed to plot")

            file_result["success"] = True
        except Exception as exc:
            file_result["error"] = str(exc)
            failed_count += 1
        finally:
            if doc_name:
                try:
                    adapter.close_document(doc_name, save_changes=False)
                except Exception as close_exc:
                    if file_result["error"] is None:
                        file_result["error"] = f"close failed: {close_exc}"
                        failed_count += 1
            _update_job(job_id, finished=index, failed=failed_count)
            results.append(file_result)

    status = "completed" if failed_count == 0 else "completed_with_errors"
    _update_job(job_id, status=status, finished=len(files), failed=failed_count, results=results, completed_at=_utc_now())


class CadService:
    """User-scenario-oriented operations with safe defaults and structured previews."""

    def __init__(self, adapter: CadAdapter, settings: Settings) -> None:
        self.adapter = adapter
        self.settings = settings

    def _limit(self, items: list[Any], label: str) -> None:
        if len(items) > self.settings.max_batch_size:
            raise BatchLimitError(
                f"{label} contains {len(items)} items; maximum is {self.settings.max_batch_size}."
            )

    def _ensure_write(self) -> None:
        # Per-call confirmation is enforced by require_permission() before this
        # method runs. Here we only guard against read-only documents.
        document = self.adapter.get_current_document()
        if document.get("readonly"):
            raise ReadOnlyDocumentError("The active drawing is read-only.")

    @staticmethod
    def _summary(results: list[dict]) -> dict:
        success_count = sum(1 for item in results if item.get("success"))
        return {
            "total": len(results),
            "success_count": success_count,
            "failure_count": len(results) - success_count,
            "results": results,
        }

    def diagnose_cad(self) -> dict:
        result = self.adapter.diagnose()
        result["server_policy"] = {
            "permission_model": "per-call confirm (v2)",
            "confirm_mode": confirm_mode(),
            "write_requires_confirm": confirm_mode() == "strict",
            "delete_requires_second_confirm": confirm_mode() == "strict",
            "save_requires_path_and_confirm": confirm_mode() == "strict",
            "how_to_change": "Set ZWCAD_MCP_CONFIRM_MODE=off (or strict) in the MCP server env and restart the client.",
            "adapter": self.settings.adapter,
            "max_batch_size": self.settings.max_batch_size,
            "max_query_results": self.settings.max_query_results,
        }
        return result

    def get_app_info(self) -> dict:
        return {"success": True, "data": self.adapter.get_app_info()}

    def get_current_document(self) -> dict:
        return {"success": True, "data": self.adapter.get_current_document()}

    def list_documents(self) -> dict:
        documents = self.adapter.list_documents()
        return {"success": True, "count": len(documents), "data": documents}

    def activate_document(self, name: str) -> dict:
        return {"success": True, "data": self.adapter.activate_document(name)}

    def scan_cad_folder(self, path: str, recursive: bool = False) -> dict:
        return {"success": True, "data": self.adapter.scan_cad_folder(path, recursive)}

    def open_document(
        self,
        path: str,
        read_only: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        target = Path(path).expanduser()
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "path": str(target),
                "read_only": read_only,
            }
        # Opening is a read-only filesystem/CAD operation; no permission gate.
        return {"success": True, "dry_run": False, "data": self.adapter.open_document(path, read_only)}

    def close_document(
        self,
        name: str,
        save_changes: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "name": name,
                "save_changes": save_changes,
            }
        # Closing may discard unsaved changes, so treat as MODIFY.
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        return {"success": True, "dry_run": False, "data": self.adapter.close_document(name, save_changes)}

    def create_batch_job(
        self,
        total: int,
        files: list[str] | None = None,
        operation: str = "plot_pdf",
        output_dir: str | None = None,
        config: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        if total <= 0:
            raise ValidationError("total must be greater than 0.")
        if files is not None and len(files) != total:
            total = len(files)

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "total": total,
                "files": files or [],
                "operation": operation,
                "output_dir": output_dir,
            }

        # A batch job that actually executes operations is a mutating workflow.
        if files:
            rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
            if rejection:
                return rejection

        job_id = _new_job_id()
        job = {
            "job_id": job_id,
            "status": "pending",
            "total": total,
            "finished": 0,
            "failed": 0,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "files": files or [],
            "operation": operation,
            "output_dir": output_dir,
            "config": config or {},
            "results": [],
        }
        with _BATCH_JOB_LOCK:
            _BATCH_JOBS[job_id] = job

        if files:
            thread = threading.Thread(
                target=_execute_batch_job,
                args=(job_id, self.settings, files, operation, output_dir, config),
                daemon=True,
            )
            thread.start()

        return {"success": True, "dry_run": False, "job_id": job_id, "status": job["status"], "total": total}

    def get_batch_job_status(self, job_id: str) -> dict:
        with _BATCH_JOB_LOCK:
            job = _BATCH_JOBS.get(job_id)
        if job is None:
            raise ValidationError(f"Batch job not found: {job_id}")
        return {
            "success": True,
            "data": {
                "job_id": job["job_id"],
                "status": job["status"],
                "total": job["total"],
                "finished": job["finished"],
                "failed": job["failed"],
                "progress_percent": round((job["finished"] / job["total"]) * 100, 1) if job["total"] else 0,
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "operation": job["operation"],
                "output_dir": job["output_dir"],
            },
        }

    def get_plot_capabilities(self) -> dict:
        return {"success": True, "data": self.adapter.get_plot_capabilities()}

    def preview_plot_scope(self, request: PlotScopeRequest) -> dict:
        return {"success": True, "data": self.adapter.preview_plot_scope(request)}

    def get_current_view(self) -> dict:
        return {"success": True, "data": self.adapter.get_current_view()}

    def get_drawing_extents(self) -> dict:
        return {"success": True, "data": self.adapter.get_drawing_extents()}

    def save_document(self, file_path: str | None, confirm: bool) -> dict:
        rejection = require_permission(PermissionLevel.SAVE, confirm=confirm, file_path=file_path)
        if rejection:
            return rejection
        self._ensure_write()
        if file_path:
            target = Path(file_path).expanduser()
            if target.exists() and not target.is_file():
                raise ValidationError(f"Save target is not a file: {target}")
        result = self.adapter.save_document(file_path)
        return {"success": True, "data": result}

    def audit_drawing(self, sample_limit: int = 30) -> dict:
        safe_limit = min(max(0, sample_limit), 100)
        return {"success": True, "data": self.adapter.audit_drawing(safe_limit)}

    def list_layers(self, detail: bool = True) -> dict:
        layers = self.adapter.list_layers(detail)
        return {"success": True, "count": len(layers), "data": layers}

    def ensure_layers(self, layers: list[LayerSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(layers, "layers")
        existing = {item["name"].lower(): item for item in self.adapter.list_layers(detail=True)}
        plan = []
        for spec in layers:
            current = existing.get(spec.name.lower())
            desired = spec.model_dump(exclude_none=True)
            if current is None:
                plan.append({"name": spec.name, "operation": "create", "desired": desired})
                continue
            changes = {
                key: value
                for key, value in desired.items()
                if key != "name" and current.get(key) != value
            }
            plan.append(
                {
                    "name": spec.name,
                    "operation": "update" if changes else "unchanged",
                    "changes": changes,
                    "current": current,
                }
            )
        if dry_run:
            return {"success": True, "dry_run": True, "planned": plan}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        return {"success": True, "dry_run": False, **self._summary(self.adapter.ensure_layers(layers))}

    def get_selected_entities(self, limit: int = 200) -> dict:
        safe_limit = min(max(1, limit), self.settings.max_query_results)
        entities = self.adapter.get_selected_entities(safe_limit)
        handles = [item["handle"] for item in entities if item.get("handle")]
        spaces = {item.get("space", "unknown") for item in entities}
        bounds = [item["bounding_box"] for item in entities if item.get("bounding_box")]
        merged = None
        if bounds:
            min_vals = [float("inf"), float("inf"), float("inf")]
            max_vals = [float("-inf"), float("-inf"), float("-inf")]
            for box in bounds:
                mn = box.get("min", [0, 0, 0])
                mx = box.get("max", [0, 0, 0])
                for i in range(3):
                    min_vals[i] = min(min_vals[i], float(mn[i]) if i < len(mn) else 0.0)
                    max_vals[i] = max(max_vals[i], float(mx[i]) if i < len(mx) else 0.0)
            merged = {"min": min_vals, "max": max_vals}
        return {
            "success": True,
            "count": len(entities),
            "truncated": len(entities) >= safe_limit,
            "empty": len(entities) == 0,
            "space": list(spaces)[0] if len(spaces) == 1 else ("mixed" if spaces else "unknown"),
            "handles": handles,
            "merged_bounding_box": merged,
            "data": entities,
        }

    def query_entities(self, query: EntityQuerySpec) -> dict:
        query.limit = min(query.limit, self.settings.max_query_results)
        entities = self.adapter.query_entities(query)
        return {
            "success": True,
            "count": len(entities),
            "truncated": len(entities) >= query.limit,
            "data": entities,
        }

    def get_entity_details(self, handles: list[str]) -> dict:
        self._limit(handles, "handles")
        return {"success": True, **self._summary(self.adapter.get_entity_details(handles))}

    # --- CAD-side filtered selection ------------------------------------------------

    def count_entities(self, flt: EntityFilter, group_by: list[str] | None = None) -> dict:
        unknown = set(group_by or []) - {"layer", "type"}
        if unknown:
            raise ValidationError(f"group_by supports 'layer' and 'type' only, got: {sorted(unknown)}")
        return {"success": True, "filter": flt.model_dump(exclude_none=True), **self.adapter.count_entities(flt, group_by)}

    def select_entities(self, flt: EntityFilter, limit: int = 200) -> dict:
        if limit < 1:
            raise ValidationError("limit must be at least 1.")
        limit = min(limit, self.settings.max_query_results)
        return {"success": True, "filter": flt.model_dump(exclude_none=True), **self.adapter.select_handles(flt, limit)}

    def update_entities_by_filter(
        self, patch: FilteredPropertyPatch, dry_run: bool = True, confirm: bool = False
    ) -> dict:
        properties = patch.model_dump(exclude={"filter"}, exclude_none=True)
        if not properties:
            raise ValidationError("No properties to change were given.")
        if "layer" in properties:
            existing = {item["name"].lower() for item in self.adapter.list_layers(detail=False)}
            if properties["layer"].lower() not in existing:
                raise ValidationError(f"Target layer does not exist: {properties['layer']} (create it with ensure_layers).")
        preview = self.adapter.count_entities(patch.filter, ["layer"])
        if preview["count"] > self.settings.max_batch_size:
            raise BatchLimitError(
                f"Filter matches {preview['count']} entities; maximum is {self.settings.max_batch_size} "
                "(raise ZWCAD_MCP_MAX_BATCH_SIZE or narrow the filter)."
            )
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "filter": patch.filter.model_dump(exclude_none=True),
                "matched": preview["count"],
                "by_layer": preview.get("by_layer", {}),
                "set": properties,
            }
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        result = self.adapter.update_entities_by_filter(patch.filter, properties)
        return {"success": result["failed"] == 0, "dry_run": False, "set": properties, **result}

    # --- layer standard mapping -----------------------------------------------------

    _PROTECTED_LAYERS = {"defpoints"}
    _SECONDS_PER_MOVED_ENTITY = 0.017  # measured on ZWCAD 2024 Standard (FINDINGS #31)

    def _load_layer_standard(self, request: LayerMapRequest) -> LayerStandard:
        if request.standard is not None and request.standard_path:
            raise ValidationError("Give either 'standard' or 'standard_path', not both.")
        if request.standard is not None:
            standard = request.standard
        elif request.standard_path:
            path = Path(request.standard_path)
            if not path.is_file():
                raise ValidationError(f"Layer standard file not found: {path}")
            try:
                standard = LayerStandard.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception as exc:
                raise ValidationError(f"Invalid layer standard file {path}: {exc}") from exc
        else:
            raise ValidationError("A layer standard is required ('standard' or 'standard_path').")
        if not standard.rules:
            raise ValidationError("The layer standard has no mapping rules.")
        return standard

    def _plan_layer_map(self, standard: LayerStandard) -> dict:
        layers = [item["name"] for item in self.adapter.list_layers(detail=False)]
        existing = {name.lower(): name for name in layers}
        counts = self.adapter.count_entities(EntityFilter(space="all"), ["layer"]).get("by_layer", {})
        counts_ci = {name.lower(): count for name, count in counts.items()}

        mapped: dict[str, str] = {}
        unmatched: dict[str, int] = {}
        for name in layers:
            low = name.lower()
            if low in self._PROTECTED_LAYERS or "|" in name:
                continue
            rule = next((r for r in standard.rules if _wild(name, [r.source])), None)
            if rule is None:
                if counts_ci.get(low, 0):
                    unmatched[name] = counts_ci[low]
                continue
            if low == rule.target.lower():
                continue
            mapped[name] = rule.target

        by_target: dict[str, list[str]] = {}
        for source, target in mapped.items():
            by_target.setdefault(target, []).append(source)

        steps: list[dict] = []
        for target, sources in by_target.items():
            renamed = None
            if target.lower() not in existing:
                candidates = [s for s in sources if s != "0"]
                if candidates:
                    renamed = max(candidates, key=lambda s: counts_ci.get(s.lower(), 0))
            for source in sorted(sources, key=lambda s: (s != renamed, s.lower())):
                steps.append(
                    {
                        "source": source,
                        "target": target,
                        "action": "rename" if source == renamed else "merge",
                        "entities": counts_ci.get(source.lower(), 0),
                    }
                )

        produced = {step["target"].lower() for step in steps if step["action"] == "rename"}
        create = sorted(
            {t for t in by_target if t.lower() not in existing and t.lower() not in produced}
            | {spec.name for spec in standard.layers if spec.name.lower() not in existing and spec.name.lower() not in produced}
        )
        merge_entities = sum(step["entities"] for step in steps if step["action"] == "merge")
        return {
            "steps": steps,
            "create_layers": create,
            "unmatched_layers_with_entities": dict(sorted(unmatched.items(), key=lambda kv: -kv[1])),
            "empty_layers": sorted(name for name in layers if not counts_ci.get(name.lower(), 0)),
            "merge_entities": merge_entities,
            "estimated_merge_s": round(merge_entities * self._SECONDS_PER_MOVED_ENTITY, 1),
        }

    def map_layers(self, request: LayerMapRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        started = time.monotonic()  # the budget covers the whole call, including planning (FINDINGS #38)
        standard = self._load_layer_standard(request)
        plan = self._plan_layer_map(standard)
        if dry_run:
            summary = {
                "success": True,
                "dry_run": True,
                "renames": sum(1 for s in plan["steps"] if s["action"] == "rename"),
                "merges": sum(1 for s in plan["steps"] if s["action"] == "merge"),
                "create_layers": plan["create_layers"],
                "merge_entities": plan["merge_entities"],
                "estimated_merge_s": plan["estimated_merge_s"],
                "unmatched_layers_with_entities": plan["unmatched_layers_with_entities"],
                "empty_layers_count": len(plan["empty_layers"]),
            }
            if request.verbose:
                return {**summary, "steps": plan["steps"], "empty_layers": plan["empty_layers"]}
            targets: dict[str, dict] = {}
            for step in plan["steps"]:
                entry = targets.setdefault(step["target"], {"rename_from": None, "merge": {}, "empty_sources": 0})
                if step["action"] == "rename":
                    entry["rename_from"] = step["source"]
                elif step["entities"]:
                    entry["merge"][step["source"]] = step["entities"]
                else:
                    entry["empty_sources"] += 1
            return {**summary, "targets": targets}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        budget = request.time_budget_s

        def left() -> float:
            return budget - (time.monotonic() - started)

        results: dict[str, Any] = {"renamed": [], "merged": [], "deleted": [], "delete_failed": [], "errors": []}

        for step in (s for s in plan["steps"] if s["action"] == "rename"):
            outcome = self.adapter.rename_layer(step["source"], step["target"])
            (results["renamed"] if outcome.get("success") else results["errors"]).append(outcome)

        specs = {spec.name.lower(): spec for spec in standard.layers}
        for name in plan["create_layers"]:
            specs.setdefault(name.lower(), LayerSpec(name=name))
        if specs:
            layer_results = self.adapter.ensure_layers(list(specs.values()))
            results["errors"].extend(item for item in layer_results if not item.get("success"))
            results["layers_ensured"] = len(layer_results)

        incomplete: list[str] = []
        for step in (s for s in plan["steps"] if s["action"] == "merge"):
            if left() <= 1:
                incomplete.append(step["source"])
                continue
            if not step["entities"]:
                # Empty source: nothing to move, delete it directly (no selection round-trip).
                if request.purge_sources:
                    deleted = self.adapter.delete_layer(step["source"])
                    (results["deleted"] if deleted.get("success") else results["delete_failed"]).append(deleted)
                continue
            moved = self.adapter.move_layer_entities(step["source"], step["target"], left())
            results["merged"].append({"source": step["source"], "target": step["target"], **moved})
            if moved.get("remaining") or moved.get("failed"):
                incomplete.append(step["source"])
                continue
            if request.purge_sources:
                deleted = self.adapter.delete_layer(step["source"])
                (results["deleted"] if deleted.get("success") else results["delete_failed"]).append(deleted)

        if request.purge_unused and not incomplete:
            # Never purge layers that the standard defines or targets, even when they are empty.
            keep = {spec.name.lower() for spec in standard.layers} | {rule.target.lower() for rule in standard.rules}
            for name in plan["empty_layers"]:
                if name.lower() in keep:
                    continue
                if name == "0" or name.lower() in self._PROTECTED_LAYERS or "|" in name:
                    continue
                if any(name.lower() == step["source"].lower() for step in plan["steps"]):
                    continue
                if left() <= 1:
                    incomplete.append(f"purge:{name}")
                    continue
                deleted = self.adapter.delete_layer(name)
                (results["deleted"] if deleted.get("success") else results["delete_failed"]).append(deleted)

        merged = results["merged"]
        out = {
            "success": not results["errors"] and not incomplete,
            "dry_run": False,
            "complete": not incomplete,
            "incomplete_sources": incomplete,
            "hint": "Run map_layers again to continue." if incomplete else None,
            "renamed": [f"{item['source']} -> {item['target']}" for item in results["renamed"]],
            "layers_ensured": results.get("layers_ensured", 0),
            "merged_sources": len(merged),
            "moved_entities": sum(item.get("moved", 0) for item in merged),
            "move_failures": [item for item in merged if item.get("failed")][:10],
            "deleted_count": len(results["deleted"]),
            "delete_failed": results["delete_failed"][:20],
            "errors": results["errors"][:20],
            "elapsed_s": round(time.monotonic() - started, 3),
        }
        if request.verbose:
            out["merged"] = merged
            out["deleted"] = [item["name"] for item in results["deleted"]]
        return out

    # --- text editing -------------------------------------------------------------

    def _new_text(self, request: TextUpdateRequest, item: dict) -> str:
        text = item["text"]
        if request.operation == "set":
            return request.value or ""
        if request.operation == "replace":
            return textops.replace_text(
                text, request.find or "", request.replacement, regex=request.regex, case_sensitive=request.case_sensitive
            )
        if request.operation == "strip_formatting":
            return textops.strip_mtext_formatting(text) if item["type"] == "MTEXT" else text
        return textops.repair_mojibake(text, request.wrong_codepage)

    def update_text_entities(self, request: TextUpdateRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        allowed = {"TEXT", "MTEXT"}
        flt = request.filter
        if not flt.entity_types:
            flt = flt.model_copy(update={"entity_types": sorted(allowed)})
        elif set(flt.entity_types) - allowed:
            raise ValidationError("update_text_entities works on TEXT and MTEXT only.")
        if request.operation == "set" and request.value is None:
            raise ValidationError("operation=set needs 'value'.")
        if request.operation == "replace" and not request.find:
            raise ValidationError("operation=replace needs 'find'.")
        if request.operation == "repair_encoding":
            try:
                "".encode(request.wrong_codepage)
            except LookupError as exc:
                raise ValidationError(f"Unknown codepage: {request.wrong_codepage}") from exc

        found = self.adapter.read_texts(flt, self.settings.max_batch_size)
        if found["truncated"]:
            raise BatchLimitError(
                f"Filter matches {found['count']} texts; maximum is {self.settings.max_batch_size} (narrow the filter)."
            )
        try:
            changes = [(item["handle"], new, item) for item in found["items"] if (new := self._new_text(request, item)) != item["text"]]
        except Exception as exc:  # bad regex
            raise ValidationError(f"Text operation failed: {exc}") from exc

        def _height(item: dict) -> float | None:
            # Stripping a leading \H code must keep the visible size (FINDINGS #34).
            if request.operation == "strip_formatting" and item["type"] == "MTEXT":
                return textops.mtext_leading_height(item["text"])
            return None

        writes = [(h, new, _height(item)) for h, new, item in changes]
        sample = [
            {"handle": h, "type": item["type"], "before": item["text"], "after": new, **({"height": hh} if hh is not None else {})}
            for (h, new, item), (_, _, hh) in zip(changes[:10], writes[:10])
        ]
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "filter": flt.model_dump(exclude_none=True),
                "matched": found["count"],
                "would_change": len(changes),
                "sample": sample,
            }
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        result = self.adapter.write_texts(writes, request.time_budget_s)
        return {
            "success": result["failed"] == 0 and result["remaining"] == 0,
            "dry_run": False,
            "matched": found["count"],
            "to_change": len(changes),
            **result,
            "sample": sample[:3],
            "hint": "Run again to continue." if result["remaining"] else None,
        }

    # --- annotation styles ------------------------------------------------------------

    def ensure_text_styles(self, styles: list[TextStyleSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(styles, "styles")
        if dry_run:
            plan = [{"name": s.name, "operation": "update" if self.adapter.style_exists("text", s.name) else "create",
                     "font": s.font} for s in styles]
            return {"success": True, "dry_run": True, "planned": plan}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = []
        for spec in styles:
            try:
                results.append(self.adapter.ensure_text_style(spec))
            except Exception as exc:
                results.append({"name": spec.name, "success": False, "error": str(exc)})
        return {"success": all(r.get("success") for r in results), "dry_run": False, "results": results}

    def ensure_dim_styles(self, styles: list[DimStyleSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(styles, "styles")
        for spec in styles:
            bad = [k for k in spec.extra if not k.upper().startswith("DIM")]
            if bad:
                raise ValidationError(f"extra may contain DIM* variables only, got: {bad}")
            if spec.text_style and not self.adapter.style_exists("text", spec.text_style):
                raise ValidationError(f"Text style not found: {spec.text_style} (create it with ensure_text_styles).")
        if dry_run:
            plan = [{"name": s.name, "operation": "update" if self.adapter.style_exists("dim", s.name) else "create",
                     "variables": s.model_dump(exclude={"name"}, exclude_none=True)} for s in styles]
            return {"success": True, "dry_run": True, "planned": plan}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = []
        for spec in styles:
            try:
                results.append(self.adapter.ensure_dim_style(spec))
            except Exception as exc:
                results.append({"name": spec.name, "success": False, "error": str(exc)})
        return {"success": all(r.get("success") for r in results), "dry_run": False, "results": results}

    # --- hatch and site balance -------------------------------------------------------

    _UNIT_TO_M2 = {4: 1e-6, 5: 1e-4, 6: 1.0}
    _UNIT_NAMES = {"mm": 4, "cm": 5, "m": 6}

    def create_hatch(self, request: HatchRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        handles = [request.outer_handle, *request.inner_handles]
        areas = self.adapter.curve_areas(handles)
        problems = {h: a.get("error") or "boundary is not closed" for h, a in areas.items()
                    if not a.get("success") or not a.get("closed", True)}
        if problems:
            raise ValidationError(f"Invalid hatch boundaries: {problems}")
        if request.layer:
            existing = {item["name"].lower() for item in self.adapter.list_layers(detail=False)}
            if request.layer.lower() not in existing:
                raise ValidationError(f"Layer does not exist: {request.layer} (create it with ensure_layers).")
        expected = areas[request.outer_handle]["area"] - sum(areas[h]["area"] for h in request.inner_handles)
        if dry_run:
            return {"success": True, "dry_run": True, "boundaries": areas, "expected_area": expected,
                    "pattern": request.pattern, "layer": request.layer}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        result = self.adapter.create_hatch(request)
        result["expected_area"] = expected
        return result

    _UNIT_TO_M = {4: 0.001, 5: 0.01, 6: 1.0}

    def dimension_to_boundaries(self, request: BoundaryDistanceRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        from zwcad_standard_mcp import geometry

        building = self.adapter.polyline_points(request.building_handle)
        plot = self.adapter.polyline_points(request.plot_handle)
        for label, shape in (("building", building), ("plot", plot)):
            if not shape["closed"] or len(shape["points"]) < 3:
                raise ValidationError(f"The {label} outline must be a closed polyline with at least 3 vertices.")
        insunits = self._UNIT_NAMES[request.units] if request.units != "auto" else self.adapter.drawing_units()
        to_m = self._UNIT_TO_M.get(insunits)
        if to_m is None:
            raise ValidationError(f"Drawing units unknown (INSUNITS={insunits}); pass units='mm'|'cm'|'m'.")
        b_pts = [tuple(p) for p in building["points"]]
        p_pts = [tuple(p) for p in plot["points"]]
        outside = [list(p) for p in b_pts if not geometry.point_in_polygon(p, p_pts)]

        rows = []
        for item in geometry.distances_to_boundaries(b_pts, p_pts):
            distance_m = round(item["distance"] * to_m, 3)
            if request.report_within_m is not None and distance_m > request.report_within_m:
                continue
            required = request.boundary_min_m.get(item["boundary_index"], request.min_distance_m)
            row = {
                "boundary_index": item["boundary_index"],
                "boundary_length_m": round(item["boundary_length"] * to_m, 2),
                "distance_m": distance_m,
                "building_point": item["building_point"],
                "boundary_point": item["boundary_point"],
            }
            if required is not None:
                row.update({"required_m": required, "ok": distance_m + 1e-6 >= required})
            rows.append(row)
        rows.sort(key=lambda r: r["distance_m"])
        result: dict[str, Any] = {
            "success": True,
            "units": {4: "mm", 5: "cm", 6: "m"}[insunits],
            "building_inside_plot": not outside,
            "vertices_outside_plot": outside[:10],
            "boundaries": rows,
            "closest_m": rows[0]["distance_m"] if rows else None,
        }
        checked = [r for r in rows if "ok" in r]
        if checked:
            result["all_ok"] = all(r["ok"] for r in checked)
            result["violations"] = [r["boundary_index"] for r in checked if not r["ok"]]
        if not request.create_dimensions:
            return result

        specs = [
            EntityCreateSpec(
                entity_type="dimension_aligned",
                params={
                    "point1": r["building_point"],
                    "point2": r["boundary_point"],
                    "text_point": [
                        (r["building_point"][0] + r["boundary_point"][0]) / 2,
                        (r["building_point"][1] + r["boundary_point"][1]) / 2,
                    ],
                },
                layer=request.layer or "0",
            )
            for r in rows
            if r["distance_m"] > 0
        ]
        if dry_run:
            return {**result, "dry_run": True, "dimensions_to_create": len(specs)}
        if request.layer:
            existing = {item["name"].lower() for item in self.adapter.list_layers(detail=False)}
            if request.layer.lower() not in existing:
                raise ValidationError(f"Layer does not exist: {request.layer} (create it with ensure_layers).")
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        created = self.adapter.create_entities(specs)
        return {**result, "dry_run": False, "dimensions": [c.get("handle") for c in created if c.get("success")],
                "dimension_failures": [c for c in created if not c.get("success")]}

    def area_takeoff(self, request: AreaTakeoffRequest) -> dict:
        groups: dict[str, list[str]] = {
            "plot": [request.plot_handle],
            "buildings": request.building_handles,
            "paved": request.paved_handles,
            **request.other_handles,
        }
        all_handles = [h for hs in groups.values() for h in hs]
        self._limit(all_handles, "handles")
        areas = self.adapter.curve_areas(all_handles)
        errors = {h: a.get("error") or "not closed" for h, a in areas.items() if not a.get("success") or not a.get("closed", True)}
        if errors:
            raise ValidationError(f"Cannot measure: {errors}")
        insunits = self._UNIT_NAMES[request.units] if request.units != "auto" else self.adapter.drawing_units()
        factor = self._UNIT_TO_M2.get(insunits)
        if factor is None:
            raise ValidationError(f"Drawing units unknown (INSUNITS={insunits}); pass units='mm'|'cm'|'m'.")

        def total(name: str) -> float:
            return round(sum(areas[h]["area"] for h in groups.get(name, [])) * factor, 2)

        plot = total("plot")
        sums = {name: total(name) for name in groups if name != "plot"}
        occupied = sum(sums.values())
        pbc = round(plot - occupied, 2)

        def pct(value: float) -> float:
            return round(100.0 * value / plot, 2) if plot else 0.0

        result: dict[str, Any] = {
            "success": True,
            "units": {4: "mm", 5: "cm", 6: "m"}[insunits],
            "plot_m2": plot,
            "areas_m2": sums,
            "biologically_active_m2": pbc,
            "percent_of_plot": {**{k: pct(v) for k, v in sums.items()}, "biologically_active": pct(pbc)},
            "assumption": "PBC = plot - all listed areas; outlines must not overlap and must lie inside the plot.",
        }
        if request.gross_floor_area_m2 is not None and plot:
            result["intensity"] = round(request.gross_floor_area_m2 / plot, 3)
        checks = []
        lim = request.limits
        if lim.min_pbc_percent is not None:
            checks.append({"check": "PBC >= min", "limit": lim.min_pbc_percent, "value": pct(pbc), "ok": pct(pbc) >= lim.min_pbc_percent})
        if lim.max_building_percent is not None:
            value = pct(sums.get("buildings", 0.0))
            checks.append({"check": "buildings <= max", "limit": lim.max_building_percent, "value": value, "ok": value <= lim.max_building_percent})
        if "intensity" in result:
            if lim.min_intensity is not None:
                checks.append({"check": "intensity >= min", "limit": lim.min_intensity, "value": result["intensity"], "ok": result["intensity"] >= lim.min_intensity})
            if lim.max_intensity is not None:
                checks.append({"check": "intensity <= max", "limit": lim.max_intensity, "value": result["intensity"], "ok": result["intensity"] <= lim.max_intensity})
        if checks:
            result["checks"] = checks
            result["all_ok"] = all(c["ok"] for c in checks)
        return result

    # --- modal dialogs ----------------------------------------------------------------

    def list_cad_dialogs(self) -> dict:
        dialogs = self.adapter.list_dialogs()
        return {
            "success": True,
            "count": len(dialogs),
            "dialogs": [{"title": d.get("title"), "buttons": d.get("buttons")} for d in dialogs],
        }

    def get_cad_job(self, job_id: str) -> dict:
        return self.adapter.job_status(job_id)

    def dismiss_cad_dialog(self, title_contains: str, button: str, confirm: bool = False) -> dict:
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        return self.adapter.dismiss_dialog(title_contains, button)

    def update_entity_properties(self, patch: EntityPropertyPatch, dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(patch.handles, "handles")
        properties = patch.model_dump(exclude={"handles"}, exclude_none=True)
        current = self.adapter.get_entity_details(patch.handles)
        plan = []
        for item in current:
            if not item.get("success"):
                plan.append(item)
                continue
            changes = {}
            for key, value in properties.items():
                current_key = {
                    "layer": "layer",
                    "color": "Color",
                    "linetype": "Linetype",
                    "linetype_scale": "LinetypeScale",
                    "lineweight": "Lineweight",
                    "visible": "visible",
                }[key]
                if item.get(current_key) != value:
                    changes[key] = {"from": item.get(current_key), "to": value}
            plan.append({"success": True, "handle": item["handle"], "changes": changes})
        if dry_run:
            return {"success": True, "dry_run": True, "planned": plan}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.update_entity_properties(patch.handles, properties)
        return {"success": True, "dry_run": False, **self._summary(results)}

    def normalize_selected_entities(
        self,
        target_layer: str | None,
        set_color_bylayer: bool,
        set_linetype_bylayer: bool,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        selected = self.adapter.get_selected_entities(self.settings.max_batch_size)
        if not selected:
            raise ValidationError("No preselected entities were found in ZWCAD.")
        handles = [item["handle"] for item in selected if item.get("handle")]
        properties: dict[str, Any] = {}
        if target_layer:
            properties["layer"] = target_layer
        if set_color_bylayer:
            properties["color"] = 256
        if set_linetype_bylayer:
            properties["linetype"] = "ByLayer"
        if not properties:
            raise ValidationError("No normalization properties were requested.")
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "selected_count": len(handles),
                "target_properties": properties,
                "entities": selected,
            }
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        if target_layer:
            existing = {item["name"].lower() for item in self.adapter.list_layers(detail=False)}
            if target_layer.lower() not in existing:
                self.adapter.ensure_layers([LayerSpec(name=target_layer)])
        results = self.adapter.update_entity_properties(handles, properties)
        return {"success": True, "dry_run": False, **self._summary(results)}

    def transform_entities(self, request: TransformRequest, dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(request.handles, "handles")
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "action": request.action,
                "params": request.params,
                "entities": self.adapter.get_entity_details(request.handles),
            }
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.transform_entities(request.action, request.handles, request.params)
        return {"success": True, "dry_run": False, **self._summary(results)}

    def delete_entities(self, handles: list[str], dry_run: bool = True, confirm: bool = False, second_confirm: bool = False) -> dict:
        self._limit(handles, "handles")
        current = self.adapter.get_entity_details(handles)
        if dry_run:
            return {"success": True, "dry_run": True, "entities_to_delete": current}
        rejection = require_permission(PermissionLevel.DELETE, confirm=confirm, second_confirm=second_confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.delete_entities(handles)
        return {"success": True, "dry_run": False, **self._summary(results)}

    def create_entities(self, entities: list[EntityCreateSpec], dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(entities, "entities")
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "entity_count": len(entities),
                "planned": [item.model_dump() for item in entities],
            }
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.create_entities(entities)
        return {"success": True, "dry_run": False, **self._summary(results)}

    def list_layouts(self) -> dict:
        layouts = self.adapter.list_layouts()
        return {"success": True, "count": len(layouts), "data": layouts}

    def get_layout_plot_settings(self, layout_name: str | None = None) -> dict:
        return {"success": True, "data": self.adapter.get_layout_plot_settings(layout_name)}

    def activate_layout(self, name: str) -> dict:
        return {"success": True, "data": self.adapter.activate_layout(name)}

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
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict:
        self._limit(layout_names, "layout_names")
        available = {item["name"] for item in self.adapter.list_layouts()}
        missing = [name for name in layout_names if name not in available]
        if missing:
            raise ValidationError(f"Unknown layouts: {', '.join(missing)}")
        if dry_run:
            document = self.adapter.get_current_document()
            stem = Path(document.get("name") or "drawing").stem
            planned = [
                {
                    "layout": name,
                    "file_path": str(Path(output_dir) / f"{stem}-{name}.{extension.lstrip('.')}"),
                    "scope_type": scope_type,
                }
                for name in layout_names
            ]
            return {"success": True, "dry_run": True, "planned": planned}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.plot_layouts(
            layout_names,
            output_dir,
            plot_configuration,
            extension.lstrip("."),
            scope_type=scope_type,
            window_lower_left=window_lower_left,
            window_upper_right=window_upper_right,
            selected_handles=selected_handles,
            center_plot=center_plot,
            fit_to_paper=fit_to_paper,
            custom_scale=custom_scale,
            override_policy=override_policy,
        )
        return {"success": True, "dry_run": False, **self._summary(results)}

    def export_drawing(self, base_file_path: str, extension: str, dry_run: bool = True, confirm: bool = False) -> dict:
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "base_file_path": base_file_path,
                "extension": extension.upper(),
            }
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        return {"success": True, "dry_run": False, "data": self.adapter.export_drawing(base_file_path, extension)}

    def verify_export_files(
        self,
        file_paths: list[str],
        layout_names: list[str] | None = None,
        min_size_bytes: int = 1024,
        expected_plot_range: dict[str, list[float]] | None = None,
    ) -> dict:
        if not file_paths:
            raise ValidationError("file_paths cannot be empty.")
        return {
            "success": True,
            "data": self.adapter.verify_export_files(
                file_paths,
                layout_names=layout_names,
                min_size_bytes=min_size_bytes,
                expected_plot_range=expected_plot_range,
            ),
        }

    def list_block_definitions(self, detail: bool = False) -> dict:
        blocks = self.adapter.list_block_definitions(detail)
        return {"success": True, "count": len(blocks), "data": blocks}

    def list_block_references(
        self,
        scope: str,
        block_name: str | None,
        has_attributes: bool | None,
        limit: int,
    ) -> dict:
        safe_limit = min(max(1, limit), self.settings.max_query_results)
        refs = self.adapter.list_block_references(
            scope, block_name, has_attributes, safe_limit
        )
        return {
            "success": True,
            "count": len(refs),
            "truncated": len(refs) >= safe_limit,
            "data": refs,
        }

    def get_block_attributes(self, handles: list[str]) -> dict:
        self._limit(handles, "handles")
        return {"success": True, **self._summary(self.adapter.get_block_attributes(handles))}

    def update_block_attributes(
        self, updates: list[BlockAttributePatch], dry_run: bool = True, confirm: bool = False
    ) -> dict:
        self._limit(updates, "updates")
        current = {
            item.get("handle"): item
            for item in self.adapter.get_block_attributes([item.handle for item in updates])
        }
        plan = []
        for update in updates:
            current_item = current.get(update.handle, {})
            attrs = {
                attr["tag"].upper(): attr.get("text", "")
                for attr in current_item.get("attributes", [])
            }
            requested = {tag.upper(): value for tag, value in update.attributes.items()}
            changes = [
                {"tag": tag, "from": attrs.get(tag), "to": value}
                for tag, value in requested.items()
                if attrs.get(tag) != value
            ]
            unmatched = sorted(tag for tag in requested if tag not in attrs)
            plan.append(
                {
                    "handle": update.handle,
                    "block_name": current_item.get("block_name"),
                    "changes": changes,
                    "unmatched_tags": unmatched,
                    "read_error": current_item.get("error"),
                }
            )
        if dry_run:
            return {"success": True, "dry_run": True, "planned": plan}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.update_block_attributes(
            [item.model_dump() for item in updates]
        )
        return {"success": True, "dry_run": False, **self._summary(results)}

    def insert_blocks(self, blocks: list[dict[str, Any]], dry_run: bool = True, confirm: bool = False) -> dict:
        self._limit(blocks, "blocks")
        for index, item in enumerate(blocks):
            for field in ("name", "insertion_point"):
                if field not in item:
                    raise ValidationError(f"blocks[{index}] is missing '{field}'.")
        if dry_run:
            return {"success": True, "dry_run": True, "planned": blocks}
        rejection = require_permission(PermissionLevel.MODIFY, confirm=confirm)
        if rejection:
            return rejection
        self._ensure_write()
        results = self.adapter.insert_blocks(blocks)
        return {"success": True, "dry_run": False, **self._summary(results)}
