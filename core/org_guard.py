"""Org information, production detection and write authorization.

Classification is read live from the org (``Organization.IsSandbox`` and
``OrganizationType``) every time a write or a test run is requested. It is never cached,
because an alias can be re-pointed at another org.

Rules, in order:

1. ``production`` — everything that cannot be shown to be something else. Writes are
   refused. There is no parameter, setting or environment variable that changes this.
2. ``developer`` — a Developer Edition org (``IsSandbox = false``). Writes are refused
   unless the user set ``allow_developer_edition_writes`` in ``sfti.config.json``.
3. ``sandbox`` / ``scratch`` — writes may be authorized.

On top of that, changes that apply a fix need ``mode = "apply"``. Diagnostic writes (trace
flags) are available in both modes. Approval by the user is a separate layer handled by
the front end; the guard records who approved.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from .audit import OUTCOME_ALLOWED, OUTCOME_BLOCKED, AuditLog
from .config import MODE_APPLY, Settings, load_settings
from .errors import (
    DeveloperEditionWriteBlockedError,
    GuardError,
    ProductionTestRunBlockedError,
    ProductionWriteBlockedError,
    SfInvestigatorError,
    WriteModeDisabledError,
)
from .sf_cli import KIND_TEST, KIND_WRITE, Permit, SfCli, _issue_permit, validate_target_org

CLASS_PRODUCTION = "production"
CLASS_SANDBOX = "sandbox"
CLASS_SCRATCH = "scratch"
CLASS_DEVELOPER = "developer"

DEVELOPER_EDITION = "Developer Edition"

ORGANIZATION_QUERY = (
    "SELECT Id, Name, IsSandbox, OrganizationType, InstanceName, TrialExpirationDate FROM Organization LIMIT 1"
)


def classify_org(is_sandbox: Any, organization_type: Any, is_scratch: bool = False) -> str:
    """Classify an org. Anything unexpected is production."""
    if is_sandbox is True:
        return CLASS_SCRATCH if is_scratch else CLASS_SANDBOX
    if is_sandbox is False and organization_type == DEVELOPER_EDITION:
        return CLASS_DEVELOPER
    return CLASS_PRODUCTION


@dataclass(frozen=True)
class OrgInfo:
    target: str
    username: str | None
    alias: str | None
    org_id: str | None
    name: str | None
    instance_url: str | None
    instance_name: str | None
    api_version: str | None
    connected_status: str | None
    is_sandbox: bool | None
    organization_type: str | None
    is_scratch: bool
    classification: str
    writes_allowed: bool
    classification_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrgGuard:
    def __init__(
        self,
        cli: SfCli,
        *,
        settings_provider: Callable[[], Settings] = load_settings,
        audit: AuditLog | None = None,
    ):
        self._cli = cli
        self._settings = settings_provider
        self._audit = audit

    # -- read-only information -----------------------------------------------------------

    def list_orgs(self) -> list[dict[str, Any]]:
        """Authenticated orgs known to the CLI, de-duplicated, without secrets."""
        result = self._cli.run(["org", "list"], timeout=180) or {}
        merged: dict[str, dict[str, Any]] = {}
        for group, entries in result.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("username") or entry.get("orgId") or id(entry))
                org = merged.setdefault(
                    key,
                    {
                        "alias": entry.get("alias"),
                        "username": entry.get("username"),
                        "org_id": entry.get("orgId"),
                        "instance_url": entry.get("instanceUrl"),
                        "connected_status": entry.get("connectedStatus"),
                        "is_default": bool(entry.get("isDefaultUsername")),
                        "is_dev_hub": bool(entry.get("isDevHub")),
                        "expiration_date": entry.get("expirationDate"),
                        "listed_under": [],
                    },
                )
                org["listed_under"].append(group)
                if not org.get("alias") and entry.get("alias"):
                    org["alias"] = entry.get("alias")
        orgs = list(merged.values())
        for org in orgs:
            org["note"] = "Use org_info for the authoritative production/sandbox classification."
        return orgs

    def org_info(self, target: str) -> OrgInfo:
        """Describe and classify one org. Fails closed to ``production``."""
        validate_target_org(target)
        display = self._cli.run(["org", "display", "--target-org", target], timeout=120) or {}
        is_scratch = bool(display.get("devHubId") or display.get("expirationDate"))

        is_sandbox: bool | None = None
        org_type: str | None = None
        name: str | None = None
        instance_name: str | None = None
        org_id: str | None = display.get("id")
        reason: str
        try:
            record = self._query_organization(target)
        except SfInvestigatorError as exc:
            record = None
            reason = f"The Organization record could not be read ({exc.message}); treated as production."
        else:
            if record is None:
                reason = "The Organization query returned no row; treated as production."
            else:
                raw = record.get("IsSandbox")
                is_sandbox = raw if isinstance(raw, bool) else None
                org_type = record.get("OrganizationType")
                name = record.get("Name")
                instance_name = record.get("InstanceName")
                org_id = record.get("Id") or org_id
                reason = f"IsSandbox = {raw!r}, OrganizationType = {org_type!r}"
                if is_sandbox is None:
                    reason += "; IsSandbox is not a boolean, treated as production."

        classification = classify_org(is_sandbox, org_type, is_scratch)
        settings = self._settings()
        writes_allowed = classification in (CLASS_SANDBOX, CLASS_SCRATCH) or (
            classification == CLASS_DEVELOPER and settings.allow_developer_edition_writes
        )
        return OrgInfo(
            target=target,
            username=display.get("username"),
            alias=display.get("alias"),
            org_id=org_id,
            name=name,
            instance_url=display.get("instanceUrl"),
            instance_name=instance_name,
            api_version=display.get("apiVersion"),
            connected_status=display.get("connectedStatus"),
            is_sandbox=is_sandbox,
            organization_type=org_type,
            is_scratch=is_scratch,
            classification=classification,
            writes_allowed=writes_allowed,
            classification_reason=reason,
        )

    def _query_organization(self, target: str) -> dict[str, Any] | None:
        # Imported here to avoid a cycle: query -> sf_cli only, but keep org_guard standalone.
        from .query import run_raw_query

        records = run_raw_query(self._cli, target, ORGANIZATION_QUERY)
        return records[0] if records else None

    # -- authorization -------------------------------------------------------------------

    def _log(self, operation: str, outcome: str, info: OrgInfo | None, target: str, **kw: Any) -> None:
        if self._audit is None:
            return
        self._audit.record(
            operation,
            outcome,
            org_target=target,
            org_id=info.org_id if info else None,
            classification=info.classification if info else None,
            **kw,
        )

    def authorize_write(
        self,
        target: str,
        operation: str,
        *,
        commands: int = 1,
        diagnostic: bool = False,
        ticket_key: str | None = None,
        approved_by: str | None = None,
    ) -> tuple[Permit, OrgInfo]:
        """Authorize ``commands`` write commands against ``target`` or raise a ``GuardError``.

        ``diagnostic`` marks writes that only help the investigation (trace flags). They are
        allowed in advise mode; everything else needs apply mode.
        """
        try:
            info = self.org_info(target)
        except GuardError:
            raise
        except SfInvestigatorError as exc:
            self._log(operation, OUTCOME_BLOCKED, None, target, ticket_key=ticket_key, approved_by=approved_by,
                      detail={"reason": f"org could not be inspected: {exc.message}"})
            raise ProductionWriteBlockedError(
                f"The org {target!r} could not be inspected, so it is treated as production: {exc.message}",
                hint=exc.hint,
            ) from exc

        try:
            self._check_write(info, operation, diagnostic)
        except GuardError as exc:
            self._log(operation, OUTCOME_BLOCKED, info, target, ticket_key=ticket_key, approved_by=approved_by,
                      detail={"reason": exc.message, "code": exc.code})
            raise

        permit = _issue_permit(
            kind=KIND_WRITE,
            target=target,
            org_id=info.org_id or "",
            classification=info.classification,
            operation=operation,
            uses=commands,
        )
        self._log(operation, OUTCOME_ALLOWED, info, target, ticket_key=ticket_key, approved_by=approved_by,
                  detail={"commands": commands, "diagnostic": diagnostic})
        return permit, info

    def _check_write(self, info: OrgInfo, operation: str, diagnostic: bool) -> None:
        if info.classification == CLASS_PRODUCTION:
            raise ProductionWriteBlockedError(
                f"'{operation}' was refused: {info.target!r} is classified as production "
                f"({info.classification_reason}). Production is read-only.",
                hint="Reproduce and fix in a sandbox. This block cannot be overridden.",
            )
        settings = self._settings()
        if info.classification == CLASS_DEVELOPER and not settings.allow_developer_edition_writes:
            raise DeveloperEditionWriteBlockedError(
                f"'{operation}' was refused: {info.target!r} is a Developer Edition org, which reports "
                "IsSandbox = false, and writes to Developer Edition orgs are not enabled.",
                hint='To allow them, set "allow_developer_edition_writes": true in sfti.config.json yourself.',
            )
        if info.classification not in (CLASS_SANDBOX, CLASS_SCRATCH, CLASS_DEVELOPER):
            raise ProductionWriteBlockedError(f"Unknown classification {info.classification!r}; treated as production.")
        if not diagnostic and settings.mode != MODE_APPLY:
            raise WriteModeDisabledError(
                f"'{operation}' was refused: the tool is in advise mode, where it explains the fix and you apply it.",
                hint='To let the tool apply changes (each still needs your approval), set "mode": "apply" '
                "in sfti.config.json yourself.",
            )

    def authorize_test_run(
        self,
        target: str,
        *,
        production_confirmed: bool = False,
        ticket_key: str | None = None,
        approved_by: str | None = None,
    ) -> tuple[Permit, OrgInfo]:
        """Authorize one Apex test run.

        Non-production orgs are always allowed. Production needs both the
        ``allow_apex_tests_on_production`` setting and ``production_confirmed``, which only the
        separately approved production tool passes.
        """
        operation = "run_apex_tests"
        try:
            info = self.org_info(target)
        except SfInvestigatorError as exc:
            self._log(operation, OUTCOME_BLOCKED, None, target, ticket_key=ticket_key,
                      detail={"reason": f"org could not be inspected: {exc.message}"})
            raise ProductionTestRunBlockedError(
                f"The org {target!r} could not be inspected, so it is treated as production: {exc.message}",
                hint=exc.hint,
            ) from exc

        if info.classification == CLASS_PRODUCTION:
            settings = self._settings()
            if not settings.allow_apex_tests_on_production:
                self._log(operation, OUTCOME_BLOCKED, info, target, ticket_key=ticket_key, approved_by=approved_by,
                          detail={"reason": "apex tests on production are disabled"})
                raise ProductionTestRunBlockedError(
                    f"Apex tests were not run: {target!r} is classified as production "
                    f"({info.classification_reason}) and test runs on production are disabled.",
                    hint='Run the tests in a sandbox, or set "allow_apex_tests_on_production": true in '
                    "sfti.config.json yourself and use run_apex_tests_on_production.",
                )
            if not production_confirmed:
                self._log(operation, OUTCOME_BLOCKED, info, target, ticket_key=ticket_key, approved_by=approved_by,
                          detail={"reason": "production test run requires the approved production tool"})
                raise ProductionTestRunBlockedError(
                    f"{target!r} is production. Use run_apex_tests_on_production, which asks for approval each time.",
                )

        permit = _issue_permit(
            kind=KIND_TEST,
            target=target,
            org_id=info.org_id or "",
            classification=info.classification,
            operation=operation,
            uses=1,
        )
        self._log(operation, OUTCOME_ALLOWED, info, target, ticket_key=ticket_key, approved_by=approved_by,
                  detail={"production_confirmed": production_confirmed})
        return permit, info
