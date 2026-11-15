"""Typed errors for the investigator core.

Every error carries a short machine-readable ``code`` and an optional ``hint`` so that
any front end (MCP server, web app, CLI) can show the same explanation.
"""

from __future__ import annotations

from typing import Any


class SfInvestigatorError(Exception):
    """Base class for all errors raised by ``core``."""

    code = "error"

    def __init__(self, message: str, *, hint: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": type(self).__name__, "code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details is not None:
            out["details"] = self.details
        return out


# --- Salesforce CLI ---------------------------------------------------------------------


class SfCliNotFoundError(SfInvestigatorError):
    code = "sf_cli_not_found"


class SfCliTimeoutError(SfInvestigatorError):
    code = "sf_cli_timeout"


class SfCliOutputError(SfInvestigatorError):
    """The CLI did not return parseable JSON."""

    code = "sf_cli_bad_output"


class SfCliError(SfInvestigatorError):
    """The CLI ran and reported a failure."""

    code = "sf_cli_error"

    def __init__(
        self,
        message: str,
        *,
        name: str = "Error",
        exit_code: int | None = None,
        command: str = "",
        data: Any = None,
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint, details=data)
        self.name = name
        self.exit_code = exit_code
        self.command = command
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out.update({"name": self.name, "exit_code": self.exit_code, "command": self.command})
        return out


# --- Guards -----------------------------------------------------------------------------


class GuardError(SfInvestigatorError):
    """A safety check refused the operation."""

    code = "guard"


class ProductionWriteBlockedError(GuardError):
    code = "production_write_blocked"


class DeveloperEditionWriteBlockedError(GuardError):
    code = "developer_edition_write_blocked"


class WriteModeDisabledError(GuardError):
    code = "write_mode_disabled"


class ProductionTestRunBlockedError(GuardError):
    code = "production_test_run_blocked"


class WritePermitError(GuardError):
    code = "write_permit"


class UnsafeArgumentError(GuardError):
    code = "unsafe_argument"


# --- Input and workspace ----------------------------------------------------------------


class InputValidationError(SfInvestigatorError):
    code = "invalid_input"


class QueryValidationError(InputValidationError):
    code = "invalid_query"


class RowLimitError(QueryValidationError):
    code = "row_limit"


class WorkspaceError(SfInvestigatorError):
    code = "workspace"


class GitError(SfInvestigatorError):
    code = "git"


# --- Changes ----------------------------------------------------------------------------


class ChangeError(SfInvestigatorError):
    code = "change"


class DriftError(ChangeError):
    """The org no longer matches the version the change was based on."""

    code = "org_drift"


class DeployFailedError(ChangeError):
    code = "deploy_failed"
