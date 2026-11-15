"""Facade that wires the core modules together.

Front ends (the MCP server, a future web app) create one :class:`Investigator` and call
its methods. Each method returns plain JSON-serializable data and raises
:class:`core.errors.SfInvestigatorError` subclasses on failure.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Sequence

from . import apex_scan, apex_tests, dependencies, evidence, flow_summary, jira, log_analysis, logs, metadata, query, report, timeline
from .audit import AuditLog
from .changes import ChangeManager
from .config import Settings, load_settings, project_root
from .errors import SfInvestigatorError
from .knowledge import OrgKnowledge, fetch_last_modified
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
        self.knowledge = OrgKnowledge(self.root)
        self._org_ids: dict[str, tuple[str, float]] = {}

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
        out["jira"] = jira.status(settings)
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
        if info.get("org_id"):
            self._org_ids[org] = (info["org_id"], time.monotonic())
            known = self.knowledge.load(info["org_id"])
            info["org_memory"] = {
                "components": len(known["components"]), "objects": len(known["objects"]),
                "notes": sum(1 for n in known["notes"] if not n.get("superseded_by")),
                "hint": "recall_org_knowledge shows what earlier tickets learned about this org."
                if known["components"] or known["notes"] else "Nothing remembered about this org yet.",
            }
        settings = self.settings()
        info["mode"] = settings.mode
        info["what_is_allowed"] = _allowed_summary(info["classification"], info["writes_allowed"], settings)
        return info

    # -- metadata ----------------------------------------------------------------------------

    def retrieve_components(self, org: str, ticket_key: str, components: Sequence[str],
                            wait_minutes: int = 10) -> dict[str, Any]:
        out = metadata.retrieve_components(self.cli, self.workspace, org, ticket_key, components,
                                           wait_minutes=wait_minutes)
        learned = self._remember(lambda org_id: self._learn_from_retrieve(org, org_id, ticket_key, out))
        if learned is not None:
            out["org_memory"] = learned
        return out

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
        out = query.describe_object(self.cli, org, sobject, **kwargs)
        if isinstance(out.get("automation"), dict):
            self._remember(lambda org_id: self.knowledge.record_automation(org_id, org, sobject, out["automation"]),
                           org=org)
        return out

    def find_dependencies(self, org: str, name: str, **kwargs: Any) -> dict[str, Any]:
        out = dependencies.find_dependencies(self.cli, org, name, **kwargs)
        self._remember(lambda org_id: self.knowledge.record_dependencies(
            org_id, org, name, kwargs.get("component_type"), out), org=org)
        return out

    # -- org memory --------------------------------------------------------------------------
    #
    # What earlier investigations learned about an org. Learning happens as a side effect of the
    # read tools and must never make one of them fail, hence ``_remember``.

    ORG_ID_CACHE_SECONDS = 300.0

    def _org_id(self, org: str) -> str:
        """Org Id for filing memory. Cached briefly; guard decisions never use this cache."""
        cached = self._org_ids.get(org)
        if cached and time.monotonic() - cached[1] < self.ORG_ID_CACHE_SECONDS:
            return cached[0]
        org_id = self.guard.org_info(org).org_id
        if not org_id:
            raise SfInvestigatorError(f"The org Id of {org!r} could not be read, so nothing can be remembered for it.")
        self._org_ids[org] = (org_id, time.monotonic())
        return org_id

    def _remember(self, action: Any, *, org: str | None = None) -> Any:
        try:
            return action(self._org_id(org) if org else None)
        except Exception as exc:  # noqa: BLE001 - memory is a convenience, never a reason to fail a tool
            return {"skipped": f"The org memory was not updated: {getattr(exc, 'message', exc)}"}

    def _learn_from_retrieve(self, org: str, _unused: Any, ticket_key: str, result: dict[str, Any]) -> dict[str, Any]:
        org_id = self._org_id(org)
        files = [{**f, "path": str(f["path"])[len("metadata/"):]} for f in result.get("files", []) if f.get("path")]
        last_modified: dict[str, str] = {}
        by_type: dict[str, list[str]] = {}
        for f in files:
            by_type.setdefault(str(f.get("type")), []).append(str(f.get("name")))
        for component_type, names in by_type.items():
            if component_type in ("ApexClass", "ApexTrigger", "Flow"):
                try:
                    last_modified.update({k: v for k, v in fetch_last_modified(
                        self.cli, org, component_type, names).items() if v})
                except SfInvestigatorError:
                    pass  # the fingerprint still detects change at the next retrieve
        return self.knowledge.record_retrieve(
            org_id, org, self.workspace.metadata_dir(ticket_key), files, ticket_key=ticket_key,
            last_modified=last_modified)

    def recall_org_knowledge(self, org: str, query_text: str, *, check: bool = True, limit: int = 10) -> dict[str, Any]:
        """What is remembered about this org for the given words, double-checked against the org."""
        org_id = self._org_id(org)
        found = self.knowledge.search(org_id, query_text, limit=limit)
        specs = [c["component"] for c in found["components"]]
        for note in found["notes"]:
            specs.extend(note.get("components", []))
        specs = list(dict.fromkeys(specs))
        out: dict[str, Any] = {"org": org, "org_id": org_id, **found}
        if check and specs:
            try:
                verified = self.knowledge.verify(self.cli, org, org_id, components=specs)
            except SfInvestigatorError as exc:
                out["freshness"] = {"checked": False, "reason": exc.message}
            else:
                status = {spec: entry for spec, entry in verified["components"].items()}
                for component in out["components"]:
                    component["freshness"] = status.get(component["component"], {"status": "not_checked"})
                refreshed = {n["id"]: n for n in self.knowledge.load(org_id)["notes"]}
                out["notes"] = [refreshed.get(n["id"], n) for n in out["notes"]]
                out["freshness"] = {"checked": True, "checked_at": verified["checked_at"], "counts": verified["counts"]}
        else:
            out["freshness"] = {"checked": False, "reason": "not requested" if not check else "nothing to check"}
        out["suggested_retrieve"] = specs[:25]
        out["how_to_use"] = (
            "Memory is a lead, not evidence. Use it to decide what to retrieve and read first; then cite the "
            "file you read today, never the memory. Anything marked changed, stale or not_checked must be "
            "read again before you rely on it."
        )
        if not found["notes"] and not found["components"] and not found["objects"]:
            out["note"] = (
                "Nothing remembered for these words. That only means no earlier ticket touched it: "
                "investigate as usual, and remember what you learn."
            )
        return out

    def remember_org_fact(self, org: str, text: str, **kwargs: Any) -> dict[str, Any]:
        return {"remembered": self.knowledge.add_note(self._org_id(org), org, text, **kwargs)}

    def forget_org_fact(self, org: str, note_id: str) -> dict[str, Any]:
        return self.knowledge.remove_note(self._org_id(org), note_id)

    def org_map(self, org: str) -> dict[str, Any]:
        out = self.knowledge.org_map(self._org_id(org))
        out["org"] = org
        out["reminder"] = "This is what earlier investigations saw. Run verify_org_knowledge to check it against the org."
        return out

    def verify_org_knowledge(self, org: str) -> dict[str, Any]:
        out = self.knowledge.verify(self.cli, org, self._org_id(org))
        out["org"] = org
        return out

    # -- logs and tests ----------------------------------------------------------------------

    def list_debug_logs(self, org: str, **kwargs: Any) -> dict[str, Any]:
        return logs.list_debug_logs(self.cli, org, **kwargs)

    def get_debug_log(self, org: str, log_id: str, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        return logs.get_debug_log(self.cli, self.workspace, org, log_id, ticket_key, **kwargs)

    def run_apex_tests(self, org: str, class_names: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        return apex_tests.run_apex_tests(self.cli, self.guard, org, class_names, workspace=self.workspace, **kwargs)

    def analyze_debug_log(self, ticket_key: str, path: str) -> dict[str, Any]:
        resolved = self.workspace.resolve(ticket_key, path)
        out = log_analysis.analyze_log(resolved.read_text(encoding="utf-8", errors="replace"))
        out["path"] = self.workspace.relative(ticket_key, resolved)
        return out

    # -- static analysis ---------------------------------------------------------------------

    def summarize_flow(self, ticket_key: str, path: str) -> dict[str, Any]:
        resolved = self.workspace.resolve(ticket_key, path)
        out = flow_summary.summarize_flow_file(resolved)
        out["path"] = self.workspace.relative(ticket_key, resolved)
        return out

    def scan_apex(self, ticket_key: str, glob: str = "") -> dict[str, Any]:
        return apex_scan.scan_workspace(self.workspace, ticket_key, glob=glob)

    # -- history -----------------------------------------------------------------------------

    def recent_org_changes(self, org: str, since: str, **kwargs: Any) -> dict[str, Any]:
        return timeline.recent_org_changes(self.cli, org, since, **kwargs)

    def record_history(self, org: str, sobject: str, record_id: str, **kwargs: Any) -> dict[str, Any]:
        return timeline.record_history(self.cli, org, sobject, record_id, **kwargs)

    # -- ticket files ------------------------------------------------------------------------

    def init_ticket(self, ticket_key: str) -> dict[str, Any]:
        return report.init_ticket(self.workspace, ticket_key)

    def write_template(self, ticket_key: str, kind: str, overwrite: bool = False) -> dict[str, Any]:
        return report.write_template(self.workspace, ticket_key, kind, overwrite=overwrite)

    def validate_report(self, ticket_key: str, filename: str = "report.md") -> dict[str, Any]:
        return report.validate_report(self.workspace, ticket_key, filename)

    def validate_comment(self, ticket_key: str, filename: str = "comment.md") -> dict[str, Any]:
        return report.validate_comment(self.workspace, ticket_key, filename)

    def export_report_html(self, ticket_key: str, filename: str = "report.md") -> dict[str, Any]:
        return report.export_report_html(self.workspace, ticket_key, filename)

    def jira_get_ticket(self, ticket_key: str, max_comments: int = 50) -> dict[str, Any]:
        return jira.get_ticket(self.settings(), ticket_key, max_comments=max_comments)

    def evidence_index(self, ticket_key: str) -> dict[str, Any]:
        return evidence.evidence_index(self.workspace, ticket_key)

    def list_tickets(self) -> dict[str, Any]:
        return report.list_tickets(self.workspace)

    def search_tickets(self, pattern: str, max_results: int = 60) -> dict[str, Any]:
        return report.search_tickets(self.workspace, pattern, max_results=max_results)

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
