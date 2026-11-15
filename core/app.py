"""Facade that wires the core modules together.

Front ends (the MCP server, a future web app) create one :class:`Investigator` and call
its methods. Each method returns plain JSON-serializable data and raises
:class:`core.errors.SfInvestigatorError` subclasses on failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from . import apex_tests, dependencies, logs, metadata, query
from .audit import AuditLog
from .changes import ChangeManager
from .config import Settings, load_settings, project_root
from .org_guard import OrgGuard
from .sf_cli import SfCli
from .workspace import Workspace


class Investigator:
    def __init__(self, root: Path | None = None, *, cli: SfCli | None = None):
        self.root = Path(root).resolve() if root else project_root()
        self.cli = cli or SfCli()
        self.workspace = Workspace(self.root)
        self.audit = AuditLog(self.root / "audit" / "audit.db")
        self.guard = OrgGuard(self.cli, settings_provider=self.settings, audit=self.audit)
        self.changes = ChangeManager(self.cli, self.guard, self.workspace, self.audit)

    def settings(self) -> Settings:
        return load_settings(self.root)

    # -- configuration and history ---------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        settings = self.settings()
        out = settings.to_dict()
        out["config_file"] = "sfti.config.json"
        out["mode_meaning"] = (
            "advise: the tool explains the fix and you apply it. "
            "apply: the tool may deploy and test, asking for approval on every write."
        )
        out["production"] = "Writes to production are always refused, whatever these settings say."
        return out

    def list_changes(self, ticket_key: str | None = None, limit: int = 20) -> dict[str, Any]:
        return {"changes": self.changes.list_changes(ticket_key, limit)}

    def audit_entries(self, ticket_key: str | None = None, limit: int = 30) -> dict[str, Any]:
        entries = self.audit.entries(limit=limit, ticket_key=ticket_key)
        for entry in entries:
            detail = entry.get("detail")
            if isinstance(detail, dict):
                # Stored field values may be personal data; show names only.
                detail.pop("updates", None)
        return {"entries": entries}

    # -- orgs --------------------------------------------------------------------------------

    def list_orgs(self) -> dict[str, Any]:
        orgs = self.guard.list_orgs()
        out: dict[str, Any] = {"count": len(orgs), "orgs": orgs}
        if not orgs:
            out["note"] = (
                "No authenticated org. Log in with: sf org login web --alias <alias> "
                "(add --instance-url https://test.salesforce.com for a sandbox)."
            )
        return out

    def org_info(self, org: str) -> dict[str, Any]:
        info = self.guard.org_info(org).to_dict()
        settings = self.settings()
        info["mode"] = settings.mode
        info["what_is_allowed"] = _allowed_summary(info["classification"], info["writes_allowed"], settings)
        return info

    # -- metadata ----------------------------------------------------------------------------

    def retrieve_components(self, org: str, ticket_key: str, components: Sequence[str],
                            wait_minutes: int = 10) -> dict[str, Any]:
        return metadata.retrieve_components(self.cli, self.workspace, org, ticket_key, components,
                                            wait_minutes=wait_minutes)

    def search_metadata(self, ticket_key: str, pattern: str, **kwargs: Any) -> dict[str, Any]:
        return metadata.search_metadata(self.workspace, ticket_key, pattern, **kwargs)

    def read_file(self, ticket_key: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return metadata.read_file(self.workspace, ticket_key, path, **kwargs)

    def list_files(self, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        return metadata.list_files(self.workspace, ticket_key, **kwargs)

    def workspace_diff(self, ticket_key: str) -> dict[str, Any]:
        return self.changes.workspace_diff(ticket_key)

    # -- data --------------------------------------------------------------------------------

    def soql_query(self, org: str, query_text: str, *, tooling: bool = False, **kwargs: Any) -> dict[str, Any]:
        return query.run_query(self.cli, org, query_text, tooling=tooling, workspace=self.workspace,
                               settings=self.settings(), **kwargs)

    def describe_object(self, org: str, sobject: str, **kwargs: Any) -> dict[str, Any]:
        return query.describe_object(self.cli, org, sobject, **kwargs)

    def find_dependencies(self, org: str, name: str, **kwargs: Any) -> dict[str, Any]:
        return dependencies.find_dependencies(self.cli, org, name, **kwargs)

    # -- logs and tests ----------------------------------------------------------------------

    def list_debug_logs(self, org: str, **kwargs: Any) -> dict[str, Any]:
        return logs.list_debug_logs(self.cli, org, **kwargs)

    def get_debug_log(self, org: str, log_id: str, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        return logs.get_debug_log(self.cli, self.workspace, org, log_id, ticket_key, **kwargs)

    def run_apex_tests(self, org: str, class_names: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        return apex_tests.run_apex_tests(self.cli, self.guard, org, class_names, workspace=self.workspace, **kwargs)

    # -- writes ------------------------------------------------------------------------------

    def set_trace_flag(self, org: str, user: str, **kwargs: Any) -> dict[str, Any]:
        return logs.set_trace_flag(self.cli, self.guard, org, user, audit=self.audit, **kwargs)

    def execute_anonymous_apex(self, org: str, ticket_key: str, apex: str, **kwargs: Any) -> dict[str, Any]:
        return self.changes.execute_anonymous_apex(org, ticket_key, apex, **kwargs)

    def update_records(self, org: str, ticket_key: str, sobject: str, updates: Sequence[dict[str, Any]],
                       **kwargs: Any) -> dict[str, Any]:
        return self.changes.update_records(org, ticket_key, sobject, updates, **kwargs)

    def deploy_component(self, org: str, ticket_key: str, components: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        return self.changes.deploy_component(org, ticket_key, components, **kwargs)

    def rollback_change(self, change_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.changes.rollback_change(change_id, **kwargs)


def _allowed_summary(classification: str, writes_allowed: bool, settings: Settings) -> str:
    if classification == "production":
        tests = (
            "Apex tests only through run_apex_tests_on_production, with approval."
            if settings.allow_apex_tests_on_production
            else "Apex test runs are disabled."
        )
        return f"Read-only. Every write is refused and this cannot be overridden. {tests}"
    if not writes_allowed:
        return (
            "Read-only for now: this is a Developer Edition org and allow_developer_edition_writes is off. "
            "Apex tests can run."
        )
    if settings.mode == "apply":
        return "Reads, Apex tests, trace flags, and fix-applying writes. Every write asks for approval."
    return (
        "Reads, Apex tests and trace flags (with approval). Deploys, record updates and anonymous Apex "
        "need mode 'apply' in sfti.config.json."
    )
