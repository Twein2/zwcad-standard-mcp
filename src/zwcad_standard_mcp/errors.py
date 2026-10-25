from __future__ import annotations


class ZWCADMCPError(RuntimeError):
    """Base error for the MCP server."""

    code = "ZWCAD_MCP_ERROR"

    def as_dict(self) -> dict:
        return {"success": False, "code": self.code, "error": str(self)}


class CadConnectionError(ZWCADMCPError):
    code = "CAD_CONNECTION_ERROR"


class NoActiveDocumentError(ZWCADMCPError):
    code = "NO_ACTIVE_DOCUMENT"


class ReadOnlyDocumentError(ZWCADMCPError):
    code = "READ_ONLY_DOCUMENT"


class WriteDisabledError(ZWCADMCPError):
    code = "WRITE_DISABLED"


class EntityNotFoundError(ZWCADMCPError):
    code = "ENTITY_NOT_FOUND"


class ValidationError(ZWCADMCPError):
    code = "VALIDATION_ERROR"


class UnsupportedOperationError(ZWCADMCPError):
    code = "UNSUPPORTED_OPERATION"


class BatchLimitError(ZWCADMCPError):
    code = "BATCH_LIMIT_EXCEEDED"


class ModalDialogError(ZWCADMCPError):
    """ZWCAD shows a modal dialog that blocks COM automation."""

    code = "MODAL_DIALOG_OPEN"

    def __init__(self, message: str, dialogs: list[dict] | None = None) -> None:
        super().__init__(message)
        self.dialogs = dialogs or []

    def as_dict(self) -> dict:
        data = super().as_dict()
        data["dialogs"] = self.dialogs
        data["hint"] = "Close the dialog in ZWCAD (or call dismiss_cad_dialog) and retry."
        return data


class CadBusyError(ZWCADMCPError):
    """A detached operation is still running in ZWCAD."""

    code = "CAD_BUSY"

    def __init__(self, message: str, job_id: str | None = None) -> None:
        super().__init__(message)
        self.job_id = job_id

    def as_dict(self) -> dict:
        data = super().as_dict()
        data["job_id"] = self.job_id
        return data


class JobDetachedError(ZWCADMCPError):
    """A blocking call was moved to the background because a dialog appeared or it ran too long."""

    def __init__(self, job_id: str, name: str, dialogs: list[dict], reason: str) -> None:
        self.code = "MODAL_DIALOG_OPEN" if reason == "modal_dialog" else "STILL_RUNNING"
        super().__init__(
            f"'{name}' is waiting in ZWCAD"
            + (" on a dialog" if reason == "modal_dialog" else " (still running)")
            + f"; continued in the background as {job_id}."
        )
        self.job_id = job_id
        self.dialogs = dialogs

    def as_dict(self) -> dict:
        data = super().as_dict()
        data.update(
            {
                "job_id": self.job_id,
                "dialogs": [
                    {"title": d.get("title"), "message": d.get("message", ""), "buttons": d.get("buttons")}
                    for d in self.dialogs
                ],
                "hint": "Answer the dialog with dismiss_cad_dialog (or in ZWCAD), then call get_cad_job(job_id).",
            }
        )
        return data
