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
