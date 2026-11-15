"""MCP server for the Salesforce Ticket Investigator.

This file is a thin adapter: each tool validates nothing itself and simply calls
``core.app.Investigator``. All Salesforce logic and every safety check live in ``core``.

Tools are grouped as:

* read-only tools — allowed without a prompt in ``.claude/settings.json``;
* write tools — always prompt for approval, and are refused by ``core`` on production.
"""

from __future__ import annotations

import functools
import inspect
import json
import sys
import traceback
from typing import Any, Callable

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from core.app import Investigator
from core.errors import SfInvestigatorError

SERVER_NAME = "sf-investigator"
APPROVED_BY = "claude-code-permission-prompt"

INSTRUCTIONS = """Salesforce Ticket Investigator.

Investigate Salesforce bug tickets with evidence. Read CLAUDE.md for the rules and procedure.
Read-only tools never change the org. Write tools ask the user for approval every time and
are refused in code on production orgs. Call get_config to see the current mode (advise or
apply) and org_info to see whether an org is production. Paths in results are relative to
tickets/<KEY>/.
"""

server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

READ_ONLY_TOOLS: list[str] = []
WRITE_TOOLS: list[str] = []
#: Tool name -> the registered callable (returns JSON text). Used by scripts/call_tool.py and tests.
TOOLS: dict[str, Callable[..., str]] = {}

_investigator: Investigator | None = None


def investigator() -> Investigator:
    global _investigator
    if _investigator is None:
        _investigator = Investigator()
    return _investigator


def _respond(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def tool(*, write: bool = False) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., str]]:
    """Register a tool and convert results and errors to JSON text."""

    def decorate(fn: Callable[..., dict[str, Any]]) -> Callable[..., str]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return _respond({"ok": True, **fn(*args, **kwargs)})
            except SfInvestigatorError as exc:
                return _respond({"ok": False, "error": exc.to_dict()})
            except Exception as exc:  # noqa: BLE001 - never crash the server on one bad call
                traceback.print_exc(file=sys.stderr)
                return _respond({"ok": False, "error": {"type": type(exc).__name__, "code": "internal", "message": str(exc)}})

        # The SDK builds the tool schema from the signature: keep the parameters, return text.
        wrapper.__annotations__ = {**fn.__annotations__, "return": str}
        wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=str)  # type: ignore[attr-defined]
        del wrapper.__wrapped__  # type: ignore[attr-defined]
        (WRITE_TOOLS if write else READ_ONLY_TOOLS).append(fn.__name__)
        TOOLS[fn.__name__] = wrapper
        server.tool()(wrapper)
        return wrapper

    return decorate


# ======================================================================================
# Read-only tools
# ======================================================================================


@tool()
def get_config() -> dict[str, Any]:
    """Show the tool's settings: mode (advise or apply), row limits, and optional switches.

    Call this at the start of an investigation. In advise mode you explain the fix and the
    user applies it; in apply mode you may deploy and test with the user's approval.
    Settings can only be changed by the user editing sfti.config.json.
    """
    return investigator().get_config()


@tool()
def list_orgs() -> dict[str, Any]:
    """List the Salesforce orgs authenticated in the Salesforce CLI (alias, username, org Id, status)."""
    return investigator().list_orgs()


@tool()
def org_info(org: str) -> dict[str, Any]:
    """Describe one org and classify it as production, sandbox, scratch or developer.

    The classification comes from Organization.IsSandbox and OrganizationType, read live.
    Production is read-only: every write tool refuses to run against it.

    Args:
        org: Org alias or username from list_orgs.
    """
    return investigator().org_info(org)


@tool()
def init_ticket(ticket_key: str) -> dict[str, Any]:
    """Create tickets/<KEY>/ with its folders and a ticket.md template. Local only; touches no org.

    Args:
        ticket_key: The ticket key, for example ABC-123.
    """
    return investigator().init_ticket(ticket_key)


@tool()
def list_tickets() -> dict[str, Any]:
    """List the ticket workspaces on this machine, with title and root-cause status when a report exists."""
    return investigator().list_tickets()


@tool()
def search_tickets(pattern: str, max_results: int = 60) -> dict[str, Any]:
    """Search earlier tickets (ticket.md, report.md, comment.md) for a regex: has this been seen before?

    Args:
        pattern: Regular expression, case-insensitive. For example an error message or a class name.
        max_results: Maximum matches to return.
    """
    return investigator().search_tickets(pattern, max_results)


@tool()
def jira_get_ticket(ticket_key: str, max_comments: int = 50) -> dict[str, Any]:
    """Phase 5, optional. Read one ticket from Jira Server / Data Center: fields and comments. Read-only.

    Off unless the user set "jira_base_url" in sfti.config.json and the SFTI_JIRA_TOKEN environment
    variable. People come back as roles, not names. Nothing is ever written to Jira: there is no
    tool that comments on, edits or transitions a ticket. When it is off, ask the user to paste
    the ticket.

    Args:
        ticket_key: The Jira key, for example ABC-123.
        max_comments: Maximum comments to return (up to 200).
    """
    return investigator().jira_get_ticket(ticket_key, max_comments)


@tool()
def retrieve_components(org: str, ticket_key: str, components: list[str], wait_minutes: int = 10) -> dict[str, Any]:
    """Retrieve metadata components from the org into tickets/<KEY>/metadata/ and commit them.

    Retrieve only what a hypothesis needs. Each retrieve is a git commit, which becomes the
    baseline that later edits are diffed against.

    Args:
        org: Org alias or username.
        ticket_key: The ticket key.
        components: "Type:Name" references, for example ["ApexTrigger:CaseTrigger",
            "ApexClass:CaseTriggerHandler", "Flow:Case_Routing", "ValidationRule:Case.Require_Reason",
            "CustomObject:Case"]. "ApexTrigger:*" retrieves a whole type. At most 50 per call.
        wait_minutes: How long to wait for the retrieve.
    """
    return investigator().retrieve_components(org, ticket_key, components, wait_minutes)


@tool()
def list_files(ticket_key: str, scope: str = "metadata", glob: str = "") -> dict[str, Any]:
    """List files in a ticket workspace folder.

    Args:
        ticket_key: The ticket key.
        scope: "metadata", "logs", "queries", "apex" or "all".
        glob: Optional filter such as "*.cls" or "*.flow-meta.xml".
    """
    return investigator().list_files(ticket_key, scope=scope, glob=glob)


@tool()
def search_metadata(
    ticket_key: str,
    pattern: str,
    glob: str = "",
    scope: str = "metadata",
    ignore_case: bool = True,
    max_results: int = 100,
    context_lines: int = 0,
) -> dict[str, Any]:
    """Regex search over files already retrieved into the ticket workspace. Returns path:line matches.

    It only sees what was retrieved: a component that was never retrieved cannot match.

    Args:
        ticket_key: The ticket key.
        pattern: Regular expression, for example "Priority\\s*=" or a field API name.
        glob: Optional file filter such as "*.cls", "*.trigger" or "*.flow-meta.xml".
        scope: "metadata" (default), "logs", "queries", "apex" or "all".
        ignore_case: Case-insensitive match.
        max_results: Maximum matches (up to 500).
        context_lines: Lines of context around each match (0-5).
    """
    return investigator().search_metadata(
        ticket_key, pattern, glob=glob, scope=scope, ignore_case=ignore_case,
        max_results=max_results, context_lines=context_lines,
    )


@tool()
def read_file(ticket_key: str, path: str, start_line: int = 1, end_line: int = 0, max_lines: int = 400) -> dict[str, Any]:
    """Read a file from the ticket workspace with line numbers, for citing file:line evidence.

    Args:
        ticket_key: The ticket key.
        path: Path relative to tickets/<KEY>/, for example
            "metadata/force-app/main/default/classes/CaseTriggerHandler.cls".
        start_line: First line to return.
        end_line: Last line to return; 0 means to the end (subject to max_lines).
        max_lines: Maximum number of lines to return.
    """
    return investigator().read_file(
        ticket_key, path, start_line=start_line, end_line=end_line or None, max_lines=max_lines
    )


@tool()
def summarize_flow(ticket_key: str, path: str) -> dict[str, Any]:
    """Outline a retrieved Flow: trigger and entry conditions, decisions, record operations, actions, order.

    Each element comes with the line of its <name> tag in the XML so it can be cited.

    Args:
        ticket_key: The ticket key.
        path: Path of the *.flow-meta.xml file relative to tickets/<KEY>/.
    """
    return investigator().summarize_flow(ticket_key, path)


@tool()
def scan_apex(ticket_key: str, glob: str = "") -> dict[str, Any]:
    """Scan retrieved Apex for bug-prone patterns (queries or DML in loops, first-record-only handling,
    return inside a loop, hardcoded Ids, swallowed exceptions, unchecked single-row queries).

    The results are leads to read in context, not findings. Never report one as a cause
    without confirming it with records, logs or a test.

    Args:
        ticket_key: The ticket key.
        glob: Optional file filter such as "*Case*".
    """
    return investigator().scan_apex(ticket_key, glob)


@tool()
def workspace_diff(ticket_key: str) -> dict[str, Any]:
    """Show local edits under tickets/<KEY>/metadata/ as a unified diff against the version retrieved
    from the org, and save it to fix.diff. Use it to present a proposed fix. Nothing is deployed.

    Args:
        ticket_key: The ticket key.
    """
    return investigator().workspace_diff(ticket_key)


@tool()
def soql_query(
    org: str,
    query: str,
    ticket_key: str = "",
    limit: int = 0,
    redact_fields: list[str] | None = None,
    reveal_fields: list[str] | None = None,
    save_as: str = "",
) -> dict[str, Any]:
    """Run a SOQL SELECT with an enforced row limit and personal-data redaction.

    Select only the fields you need. A LIMIT is added when missing (default 50) and a LIMIT
    above the maximum (200) is rejected. Values of fields that look like personal data are
    replaced with [REDACTED]. When ticket_key is given, the redacted result is saved under
    tickets/<KEY>/queries/ so it can be cited as evidence.

    Args:
        org: Org alias or username.
        query: A SOQL SELECT statement.
        ticket_key: Save the result as evidence for this ticket (recommended).
        limit: Row limit for this call; 0 uses the default.
        redact_fields: Extra field names to redact (for example free-text fields).
        reveal_fields: Field names to show in this reply even though they match a personal-data
            pattern. Use only when the value itself is the subject of the bug. Saved files stay redacted.
        save_as: Short name for the saved file, for example "affected-cases".
    """
    return investigator().soql_query(
        org, query, tooling=False, ticket_key=ticket_key or None, limit=limit or None,
        redact_fields=redact_fields or (), reveal_fields=reveal_fields or (), save_as=save_as or None,
    )


@tool()
def tooling_query(
    org: str,
    query: str,
    ticket_key: str = "",
    limit: int = 0,
    save_as: str = "",
) -> dict[str, Any]:
    """Run a Tooling API SELECT (ApexClass, ApexTrigger, Flow, ValidationRule, EntityDefinition, …).

    Same limit rules as soql_query. Querying the Metadata or FullName field requires a
    query that returns exactly one row.

    Args:
        org: Org alias or username.
        query: A Tooling API SELECT statement.
        ticket_key: Save the result as evidence for this ticket.
        limit: Row limit for this call; 0 uses the default.
        save_as: Short name for the saved file.
    """
    return investigator().soql_query(
        org, query, tooling=True, ticket_key=ticket_key or None, limit=limit or None, save_as=save_as or None,
    )


@tool()
def describe_object(
    org: str,
    object_name: str,
    fields: list[str] | None = None,
    field_filter: str = "",
    include: list[str] | None = None,
) -> dict[str, Any]:
    """Describe an object: fields, record types, child relationships, and the automation that runs on it.

    Args:
        org: Org alias or username.
        object_name: Object API name, for example "Case" or "Invoice__c".
        fields: Return only these fields.
        field_filter: Return fields whose name or label contains this text.
        include: Any of "fields", "record_types", "child_relationships", "automation".
            "automation" lists triggers, record-triggered flows, validation rules and workflow
            rules on the object. Default: ["fields", "record_types"].
    """
    return investigator().describe_object(
        org, object_name, fields=fields, field_filter=field_filter or None,
        include=include or ("fields", "record_types"),
    )


@tool()
def find_dependencies(org: str, name: str, component_type: str = "", direction: str = "both", limit: int = 200) -> dict[str, Any]:
    """What uses this component, and what does it use (Tooling API MetadataComponentDependency).

    An empty answer means "none recorded", not "none exist": confirm with search_metadata.

    Args:
        org: Org alias or username.
        name: Component name, for example "CaseTriggerHandler". For a custom field use the name
            without the object and without __c.
        component_type: Optional type filter such as "ApexClass", "ApexTrigger", "Flow", "CustomField".
        direction: "used_by", "uses" or "both".
        limit: Maximum rows per direction (up to 500).
    """
    return investigator().find_dependencies(
        org, name, component_type=component_type or None, direction=direction, limit=limit
    )


@tool()
def recent_org_changes(org: str, since: str, until: str = "", contains: str = "", limit: int = 100) -> dict[str, Any]:
    """What changed in the org in a time window: setup audit trail plus modified Apex classes,
    triggers, flow versions and validation rules. Use it to compare with when the problem started.

    Args:
        org: Org alias or username.
        since: Start of the window, ISO 8601 (for example "2026-03-01" or "2026-03-01T08:00:00Z").
        until: End of the window; empty means now.
        contains: Keep only entries mentioning this text (for example an object or class name).
        limit: Maximum rows per source (up to 200).
    """
    return investigator().recent_org_changes(org, since, until=until or None, contains=contains or None, limit=limit)


@tool()
def record_history(org: str, object_name: str, record_id: str, limit: int = 100) -> dict[str, Any]:
    """Field history of one record (who changed which field and when), newest first.

    Needs field history tracking on the object. Users are returned as Ids.

    Args:
        org: Org alias or username.
        object_name: Object API name, for example "Case".
        record_id: The record Id.
        limit: Maximum rows (up to 200).
    """
    return investigator().record_history(org, object_name, record_id, limit=limit)


@tool()
def list_debug_logs(
    org: str,
    user_id: str = "",
    since: str = "",
    operation_contains: str = "",
    failed_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """List Apex debug logs, newest first. Logs exist only while a trace flag is active for the user.

    Args:
        org: Org alias or username.
        user_id: Only logs of this user Id (005…).
        since: Only logs started at or after this ISO 8601 time.
        operation_contains: Only logs whose operation contains this text.
        failed_only: Only logs whose status is not Success.
        limit: Maximum logs (up to 100).
    """
    return investigator().list_debug_logs(
        org, user_id=user_id or None, since=since or None, operation_contains=operation_contains or None,
        status_not_success=failed_only, limit=limit,
    )


@tool()
def get_debug_log(
    org: str,
    log_id: str,
    ticket_key: str,
    grep: str = "",
    start_line: int = 1,
    max_lines: int = 200,
    context_lines: int = 0,
) -> dict[str, Any]:
    """Download a debug log into tickets/<KEY>/logs/ and return a summary plus an excerpt.

    Args:
        org: Org alias or username.
        log_id: The log Id (07L…).
        ticket_key: The ticket key.
        grep: Return only lines matching this regex (for example "EXCEPTION|FATAL|USER_DEBUG").
        start_line: First line of the excerpt when grep is not used.
        max_lines: Maximum lines in the excerpt.
        context_lines: Lines of context around each grep match (0-5).
    """
    return investigator().get_debug_log(
        org, log_id, ticket_key, grep=grep or None, start_line=start_line, max_lines=max_lines,
        context_lines=context_lines,
    )


@tool()
def analyze_debug_log(ticket_key: str, path: str) -> dict[str, Any]:
    """Analyze a downloaded debug log: order of triggers and flows, validation results, exceptions,
    DML and SOQL (with repeated-query detection), and governor limit usage. Every item has its log line.

    Args:
        ticket_key: The ticket key.
        path: Path of the log relative to tickets/<KEY>/, for example "logs/07L….log".
    """
    return investigator().analyze_debug_log(ticket_key, path)


@tool()
def run_apex_tests(org: str, class_names: list[str], ticket_key: str = "", code_coverage: bool = True,
                   wait_minutes: int = 10) -> dict[str, Any]:
    """Run Apex test classes in a non-production org and return outcomes, failures and coverage.

    Refused on production (see run_apex_tests_on_production).

    Args:
        org: Org alias or username.
        class_names: Test class names, for example ["CaseTriggerHandlerTest"]. At most 20.
        ticket_key: Save the result as evidence for this ticket.
        code_coverage: Include per-class coverage with uncovered lines.
        wait_minutes: How long to wait for the run.
    """
    return investigator().run_apex_tests(
        org, class_names, ticket_key=ticket_key or None, code_coverage=code_coverage, wait_minutes=wait_minutes
    )


@tool()
def list_changes(ticket_key: str = "", limit: int = 20) -> dict[str, Any]:
    """List changes this tool applied (deploys and record updates) with their status and change Ids.

    Args:
        ticket_key: Only changes for this ticket; empty for all.
        limit: Maximum rows.
    """
    return investigator().list_changes(ticket_key or None, limit)


@tool()
def audit_log(ticket_key: str = "", limit: int = 30) -> dict[str, Any]:
    """Show the audit log: every write attempt, including the ones that were blocked.

    Args:
        ticket_key: Only entries for this ticket; empty for all.
        limit: Maximum rows.
    """
    return investigator().audit_entries(ticket_key or None, limit)


@tool()
def write_template(ticket_key: str, kind: str, overwrite: bool = False) -> dict[str, Any]:
    """Write the report.md or comment.md template into the ticket folder. Local only.

    Args:
        ticket_key: The ticket key.
        kind: "report" or "comment".
        overwrite: Replace an existing file.
    """
    return investigator().write_template(ticket_key, kind, overwrite)


@tool()
def evidence_index(ticket_key: str) -> dict[str, Any]:
    """List every artifact saved for a ticket (query results, debug logs, test runs, Apex scripts,
    retrieved source, metadata commits) with a one-line description and the exact string to cite.

    Use it when writing the report's Evidence table. Local only; touches no org.

    Args:
        ticket_key: The ticket key.
    """
    return investigator().evidence_index(ticket_key)


@tool()
def validate_report(ticket_key: str, filename: str = "report.md") -> dict[str, Any]:
    """Check a report: required sections, every citation resolves to a real file and line in the ticket
    folder, a confirmed root cause cites evidence, speculation is labelled, and no personal data.

    Run it before presenting a report, and fix every problem it lists.

    Args:
        ticket_key: The ticket key.
        filename: Report file relative to tickets/<KEY>/.
    """
    return investigator().validate_report(ticket_key, filename)


@tool()
def validate_comment(ticket_key: str, filename: str = "comment.md") -> dict[str, Any]:
    """Check a draft ticket comment for personal data, unfinished placeholders and length.

    Args:
        ticket_key: The ticket key.
        filename: Comment file relative to tickets/<KEY>/.
    """
    return investigator().validate_comment(ticket_key, filename)


@tool()
def export_report_html(ticket_key: str, filename: str = "report.md") -> dict[str, Any]:
    """Render the report as a self-contained HTML page (report.html) for reading or sharing by hand.

    Args:
        ticket_key: The ticket key.
        filename: Report file relative to tickets/<KEY>/.
    """
    return investigator().export_report_html(ticket_key, filename)


# ======================================================================================
# Write tools — every call asks the user for approval; core refuses them on production
# ======================================================================================


@tool(write=True)
def set_trace_flag(org: str, user: str, minutes: int = 30, ticket_key: str = "") -> dict[str, Any]:
    """WRITE. Create or extend a debug trace flag for a user so their next actions produce debug logs.

    Refused on production. Allowed in advise and apply mode.

    Args:
        org: Org alias or username.
        user: User Id (005…) or username of the user to trace.
        minutes: How long the trace flag stays active (1-1440).
        ticket_key: The ticket this is for.
    """
    return investigator().set_trace_flag(
        org, user, minutes=minutes, ticket_key=ticket_key or None, approved_by=APPROVED_BY
    )


@tool(write=True)
def execute_anonymous_apex(org: str, ticket_key: str, apex: str, reason: str) -> dict[str, Any]:
    """WRITE. Run anonymous Apex in a sandbox, scratch or (if enabled) Developer Edition org.

    Refused on production and in advise mode. DML committed by the script cannot be rolled
    back automatically; prefer scripts that only read, or that roll back with a savepoint.

    Args:
        org: Org alias or username.
        ticket_key: The ticket key; the script and its log are saved under tickets/<KEY>/apex/.
        apex: The Apex code to run.
        reason: Why this is needed.
    """
    return investigator().execute_anonymous_apex(org, ticket_key, apex, reason=reason, approved_by=APPROVED_BY)


@tool(write=True)
def update_records(org: str, ticket_key: str, object_name: str, updates: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    """WRITE. Update up to 10 records in a non-production org. Previous values are captured for rollback.

    Refused on production and in advise mode.

    Args:
        org: Org alias or username.
        ticket_key: The ticket key.
        object_name: Object API name.
        updates: List of {"id": "<record id>", "fields": {"Field__c": value}}.
        reason: Why this is needed.
    """
    return investigator().update_records(org, ticket_key, object_name, updates, reason=reason, approved_by=APPROVED_BY)


@tool(write=True)
def deploy_component(
    org: str,
    ticket_key: str,
    components: list[str],
    reason: str,
    test_level: str = "NoTestRun",
    tests: list[str] | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """WRITE. Deploy the locally edited version of a component to a non-production org.

    Sequence: commit the edit, snapshot the org's current version, stop if the org drifted,
    deploy, verify by retrieving again, record the change for rollback. Refused on production
    and in advise mode.

    Args:
        org: Org alias or username.
        ticket_key: The ticket key.
        components: The component to deploy as ["Type:Name"]. Up to 5 only when they must land
            together to compile (for example a class and the test class that covers the change).
        reason: What this change fixes.
        test_level: "NoTestRun", "RunSpecifiedTests" or "RunLocalTests".
        tests: Test class names when test_level is "RunSpecifiedTests".
        validate_only: Check-only deploy; the org is not changed.
    """
    return investigator().deploy_component(
        org, ticket_key, components, reason=reason, test_level=test_level, tests=tests or (),
        validate_only=validate_only, approved_by=APPROVED_BY,
    )


@tool(write=True)
def rollback_change(change_id: str, reason: str, force: bool = False) -> dict[str, Any]:
    """WRITE. Undo a change made by deploy_component or update_records using its pre-change snapshot.

    Args:
        change_id: The change Id returned by the tool that made the change (see list_changes).
        reason: Why the change is being rolled back.
        force: Restore the snapshot even if the component changed again in the org afterwards.
    """
    return investigator().rollback_change(change_id, reason=reason, force=force, approved_by=APPROVED_BY)


@tool(write=True)
def run_apex_tests_on_production(org: str, class_names: list[str], ticket_key: str = "",
                                 code_coverage: bool = False, wait_minutes: int = 10) -> dict[str, Any]:
    """Run Apex test classes on a PRODUCTION org. Asks for approval every time.

    Only works when the user has set "allow_apex_tests_on_production": true in
    sfti.config.json. Test data is rolled back by the platform, but the run uses org
    resources and creates test-run records.

    Args:
        org: Org alias or username.
        class_names: Test class names. At most 20.
        ticket_key: Save the result as evidence for this ticket.
        code_coverage: Include coverage.
        wait_minutes: How long to wait for the run.
    """
    return investigator().run_apex_tests(
        org, class_names, ticket_key=ticket_key or None, code_coverage=code_coverage,
        wait_minutes=wait_minutes, production_confirmed=True, approved_by=APPROVED_BY,
    )


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
