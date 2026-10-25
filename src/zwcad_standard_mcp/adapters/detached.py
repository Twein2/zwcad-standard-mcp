"""Run blocking CAD calls in a worker thread so the MCP server stays responsive (FINDINGS #37).

COM calls such as Documents.Open or PlotToFile do not return while ZWCAD shows a modal dialog. Run on the
server's event loop they block every other tool, including list_cad_dialogs / dismiss_cad_dialog. The runner
executes the call in a worker thread and returns early with a job id when an unknown dialog stays open
(or the call runs too long); the worker keeps waiting, and get_cad_job collects the result later.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from typing import Any

from zwcad_standard_mcp.errors import CadBusyError, JobDetachedError

_ids = itertools.count(1)


class DetachedRunner:
    def __init__(
        self,
        find_dialogs: Callable[[], list[dict]],
        is_safe_dialog: Callable[[dict], bool],
        detach_after_s: float = 45.0,
        unknown_dialog_grace_s: float = 6.0,
        poll_s: float = 0.3,
    ) -> None:
        self._find_dialogs = find_dialogs
        self._is_safe = is_safe_dialog
        self.detach_after_s = detach_after_s
        self.unknown_dialog_grace_s = unknown_dialog_grace_s
        self.poll_s = poll_s
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- state -------------------------------------------------------------------------

    def running_job(self) -> dict[str, Any] | None:
        with self._lock:
            for job in self.jobs.values():
                if not job["done"].is_set():
                    return job
        return None

    def ensure_idle(self) -> None:
        job = self.running_job()
        if job is not None:
            raise CadBusyError(
                f"ZWCAD is still busy with '{job['name']}' (job {job['id']}). Answer the dialog with "
                "list_cad_dialogs / dismiss_cad_dialog, then call get_cad_job.",
                job_id=job["id"],
            )

    # -- run ---------------------------------------------------------------------------

    def run(self, name: str, work: Callable[[], dict], on_finish: Callable[[], None] | None = None) -> dict:
        """Run ``work`` in a worker thread; return its result, or raise JobDetachedError."""
        self.ensure_idle()
        job: dict[str, Any] = {
            "id": f"job-{next(_ids)}",
            "name": name,
            "started": time.monotonic(),
            "done": threading.Event(),
            "result": None,
            "error": None,
        }

        def _target() -> None:
            try:
                job["result"] = work()
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                job["error"] = exc
            finally:
                try:
                    if on_finish is not None:
                        on_finish()
                finally:
                    job["finished"] = time.monotonic()
                    job["done"].set()

        with self._lock:
            self.jobs[job["id"]] = job
        threading.Thread(target=_target, name=f"zwcad-{name}", daemon=True).start()

        unknown_since: float | None = None
        while not job["done"].wait(self.poll_s):
            elapsed = time.monotonic() - job["started"]
            unknown = [d for d in self._find_dialogs() if not self._is_safe(d)]
            if unknown:
                unknown_since = unknown_since or time.monotonic()
                if time.monotonic() - unknown_since >= self.unknown_dialog_grace_s:
                    raise JobDetachedError(job["id"], name, unknown, reason="modal_dialog")
            else:
                unknown_since = None
            if elapsed >= self.detach_after_s:
                raise JobDetachedError(job["id"], name, unknown, reason="still_running")
        return self._collect(job)

    def _collect(self, job: dict[str, Any]) -> dict:
        with self._lock:
            self.jobs.pop(job["id"], None)
        if job["error"] is not None:
            raise job["error"]
        return job["result"]

    def status(self, job_id: str) -> dict:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            return {"success": False, "job_id": job_id, "error": "Unknown or already collected job."}
        if not job["done"].is_set():
            return {
                "success": True,
                "job_id": job_id,
                "name": job["name"],
                "status": "running",
                "elapsed_s": round(time.monotonic() - job["started"], 1),
                "dialogs": self._find_dialogs(),
            }
        try:
            result = self._collect(job)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "UNEXPECTED_ERROR")
            return {"success": False, "job_id": job_id, "name": job["name"], "status": "failed", "code": code, "error": str(exc)}
        return {"success": True, "job_id": job_id, "name": job["name"], "status": "done", "result": result}
