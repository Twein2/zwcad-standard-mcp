from __future__ import annotations

import threading
import time

from zwcad_standard_mcp.adapters.detached import DetachedRunner
from zwcad_standard_mcp.errors import CadBusyError, JobDetachedError


def _runner(dialogs: list[dict], **kw) -> DetachedRunner:
    return DetachedRunner(
        find_dialogs=lambda: list(dialogs),
        is_safe_dialog=lambda d: d.get("title") == "safe",
        poll_s=0.01,
        **kw,
    )


def test_fast_call_returns_result() -> None:
    runner = _runner([])
    assert runner.run("op", lambda: {"ok": 1}) == {"ok": 1}
    assert runner.running_job() is None


def test_unknown_dialog_detaches_then_job_completes() -> None:
    dialogs = [{"title": "Too Many Hatch Lines", "buttons": ["OK"]}]
    release = threading.Event()
    runner = _runner(dialogs, unknown_dialog_grace_s=0.05)

    def work() -> dict:
        release.wait(2)
        return {"opened": True}

    try:
        runner.run("open_document", work)
    except JobDetachedError as exc:
        data = exc.as_dict()
        assert data["code"] == "MODAL_DIALOG_OPEN" and data["dialogs"][0]["title"] == "Too Many Hatch Lines"
        job_id = exc.job_id
    else:
        raise AssertionError("expected detach")

    # Server stays usable for status calls, but further CAD calls are refused while busy.
    assert runner.status(job_id)["status"] == "running"
    try:
        runner.ensure_idle()
    except CadBusyError as exc:
        assert exc.job_id == job_id
    else:
        raise AssertionError("expected CadBusyError")

    dialogs.clear()
    release.set()
    for _ in range(100):
        status = runner.status(job_id)
        if status.get("status") == "done":
            break
        time.sleep(0.01)
    assert status == {"success": True, "job_id": job_id, "name": "open_document", "status": "done", "result": {"opened": True}}
    assert runner.running_job() is None


def test_safe_dialog_does_not_detach_and_errors_propagate() -> None:
    runner = _runner([{"title": "safe"}], unknown_dialog_grace_s=0.01)

    def boom() -> dict:
        time.sleep(0.05)
        raise ValueError("cannot open")

    try:
        runner.run("open_document", boom)
    except ValueError as exc:
        assert "cannot open" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_long_call_detaches_as_still_running() -> None:
    runner = _runner([], detach_after_s=0.05)
    release = threading.Event()
    try:
        runner.run("plot", lambda: release.wait(2) and {"plotted": True})
    except JobDetachedError as exc:
        assert exc.code == "STILL_RUNNING"
    release.set()
