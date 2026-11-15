"""The DEMO-101 tool sequences, shared by the end-to-end tests.

``investigate`` is the advise-mode investigation that produced ``samples/DEMO-101/expected/``.
``apply_loop`` is the apply-mode loop (validate, deploy, test, re-check, roll back) on the simulated
sandbox; it returns a transcript whose normalized form is stored next to the expected report.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.app import Investigator
from core.errors import SfInvestigatorError

from .conftest import FIX_COMPONENTS, SERVICE, SERVICE_TEST, apply_planted_bug_fix

KEY = "DEMO-101"
PROD, SANDBOX = "demo-prod", "demo-sandbox"
LOG_ID = "07LWs00000L0001MAA"
FLOW = "metadata/force-app/main/default/flows/Case_Premium_Notification.flow-meta.xml"
WRONG_CASES = ["500Ws00000C0003IAA", "500Ws00000C0004IAA", "500Ws00000C0006IAA"]

CASES_QUERY = (
    "SELECT Id, CaseNumber, Priority, Origin, AccountId, Account.Support_Tier__c, CreatedById, CreatedDate "
    "FROM Case WHERE CreatedDate >= 2026-11-09T00:00:00Z ORDER BY CreatedDate, CaseNumber"
)
NOT_HIGH_QUERY = (
    "SELECT Id, CaseNumber, Priority, CreatedDate FROM Case WHERE Priority != 'High' "
    "AND Account.Support_Tier__c = 'Premium' ORDER BY CaseNumber"
)


def investigate(inv: Investigator, ticket_dir: Path) -> dict[str, Any]:
    """Replay the investigation. Returns the tool results the report is built on."""
    out: dict[str, Any] = {}
    out["config"] = inv.get_config()
    out["init"] = inv.init_ticket(KEY)
    out["prod"] = inv.org_info(PROD)
    out["sandbox"] = inv.org_info(SANDBOX)
    out["earlier_tickets"] = inv.search_tickets("Premium")

    out["cases"] = inv.soql_query(PROD, CASES_QUERY, ticket_key=KEY, save_as="cases-since-9-nov")
    out["automation"] = inv.describe_object(PROD, "Case", include=["automation"])["automation"]
    out["retrieve"] = inv.retrieve_components(PROD, KEY, [
        "ApexTrigger:CaseTrigger", "ApexClass:CaseTriggerHandler", "ApexClass:CaseEscalationService",
        "ApexClass:CaseEscalationServiceTest", "Flow:Case_Premium_Notification"])
    out["scan"] = inv.scan_apex(KEY)
    out["service"] = inv.read_file(KEY, SERVICE, start_line=26, end_line=33)
    out["flow"] = inv.summarize_flow(KEY, FLOW)
    out["changes"] = inv.recent_org_changes(PROD, "2026-11-03", until="2026-11-12T23:59:59Z")
    out["audit_trail"] = inv.soql_query(
        PROD, "SELECT Action, Section, Display, CreatedDate, CreatedById FROM SetupAuditTrail "
              "WHERE CreatedDate >= 2026-11-03T00:00:00Z ORDER BY CreatedDate",
        ticket_key=KEY, save_as="setup-audit-trail")
    out["class_dates"] = inv.soql_query(
        PROD, "SELECT Name, LastModifiedDate, LastModifiedById FROM ApexClass WHERE Name LIKE 'Case%' ORDER BY Name",
        tooling=True, ticket_key=KEY, save_as="apex-class-last-modified")
    out["flow_versions"] = inv.soql_query(
        PROD, "SELECT Id, VersionNumber, Status, LastModifiedDate FROM Flow ORDER BY LastModifiedDate",
        tooling=True, ticket_key=KEY, save_as="flow-versions")
    out["logs"] = inv.list_debug_logs(PROD)
    out["log"] = inv.get_debug_log(PROD, LOG_ID, KEY, grep=r"STATEMENT_EXECUTE\|\[(2[6-9]|3[0-3])\]|FLOW_RULE_DETAIL")
    out["log_analysis"] = inv.analyze_debug_log(KEY, out["log"]["saved_to"])
    out["dependencies"] = inv.find_dependencies(PROD, "CaseEscalationService")
    out["callers"] = inv.search_metadata(KEY, r"applyPremiumPriority\(", glob="*.cls")
    out["tests"] = inv.run_apex_tests(SANDBOX, ["CaseEscalationServiceTest"], ticket_key=KEY)
    out["sandbox_class_dates"] = inv.soql_query(
        SANDBOX, "SELECT Name, LastModifiedDate FROM ApexClass WHERE Name LIKE 'Case%' ORDER BY Name",
        tooling=True, ticket_key=KEY, save_as="sandbox-apex-class-last-modified")
    out["mixed_batch_test_before_fix"] = inv.search_metadata(KEY, "mixedBatch|List<Case>\\{", glob="*Test.cls")
    out["not_high"] = inv.soql_query(PROD, NOT_HIGH_QUERY, ticket_key=KEY, save_as="premium-cases-not-high")

    apply_planted_bug_fix(ticket_dir)
    out["diff"] = inv.workspace_diff(KEY)
    out["evidence"] = inv.evidence_index(KEY)
    return out


_NORMALIZE = [
    (re.compile(r"CHG-\d{8}-\d{6}-[0-9a-f]{6}"), "CHG-<id>"),
    (re.compile(r"\b0Af[0-9a-f]{15}\b"), "0Af<deploy id>"),
    (re.compile(r"\b[0-9a-f]{40}\b"), "<commit>"),
    (re.compile(r"queries/\d{8}-\d{6}-"), "queries/<timestamp>-"),
]


def normalize(text: str) -> str:
    for pattern, replacement in _NORMALIZE:
        text = pattern.sub(replacement, text)
    return text


def apply_loop(inv: Investigator, ticket_dir: Path) -> list[str]:
    """Validate, deploy, test, re-check, roll back on the sandbox. ``inv`` must be in apply mode."""
    lines: list[str] = []

    def step(title: str) -> None:
        lines.append("")
        lines.append(f"## {title}")

    def say(text: str) -> None:
        lines.append(text)

    def refused(call) -> str:
        try:
            call()
        except SfInvestigatorError as exc:
            text = f"REFUSED [{exc.code}] {exc.message}"
            details = exc.details if isinstance(exc.details, dict) else {}
            for failure in details.get("test_failures") or []:
                text += f"\n  test failure: {failure['name']}: {failure['message']}"
            return text
        raise AssertionError("the call was expected to be refused")

    step("get_config")
    say(f"mode = {inv.get_config()['mode']}")

    step(f"org_info {SANDBOX}")
    info = inv.org_info(SANDBOX)
    say(f"classification = {info['classification']}, writes_allowed = {info['writes_allowed']}")

    step(f"retrieve_components {SANDBOX} (the fix is based on the sandbox's own version)")
    retrieved = inv.retrieve_components(SANDBOX, KEY, FIX_COMPONENTS)
    say(f"retrieved {retrieved['retrieved_count']} files")

    step("attempt 1: add the new test only, validate")
    apply_planted_bug_fix(ticket_dir)
    service = ticket_dir / SERVICE
    fixed_service = service.read_text(encoding="utf-8")
    service.write_text(fixed_service.replace("continue;", "return;"), encoding="utf-8")
    say(refused(lambda: inv.deploy_component(
        SANDBOX, KEY, ["ApexClass:CaseEscalationServiceTest"], reason="DEMO-101: add the mixed-batch test",
        validate_only=True, test_level="RunSpecifiedTests", tests=["CaseEscalationServiceTest"])))
    failed = inv.list_changes(KEY)["changes"][0]
    say(f"change {failed['change_id']}: status = {failed['status']}")
    say("-> the new test fails against the current service: it reproduces the defect. The org is unchanged.")

    step("attempt 2: service fix and test together, validate")
    service.write_text(fixed_service, encoding="utf-8")
    validated = inv.deploy_component(
        SANDBOX, KEY, FIX_COMPONENTS, reason="DEMO-101: escalate every Premium case in a mixed batch",
        validate_only=True, test_level="RunSpecifiedTests", tests=["CaseEscalationServiceTest"])
    say(f"change {validated['change_id']}: status = {validated['status']}, tests_run = {validated['tests_run']}")
    say(validated["note"])

    step("deploy_component")
    deployed = inv.deploy_component(
        SANDBOX, KEY, FIX_COMPONENTS, reason="DEMO-101: escalate every Premium case in a mixed batch",
        test_level="RunSpecifiedTests", tests=["CaseEscalationServiceTest"])
    say(f"change {deployed['change_id']}: status = {deployed['status']}, verified = {deployed['verified']}, "
        f"tests_run = {deployed['tests_run']}")
    say("deployed: " + ", ".join(f"{f['type']}:{f['name']} [{f['state']}]" for f in deployed["deployed_files"]
                                  if not str(f.get("path", "")).endswith("-meta.xml")))
    say(f"pre_commit = {deployed['pre_commit']}, post_commit = {deployed['post_commit']}")

    step("run_apex_tests")
    tests = inv.run_apex_tests(SANDBOX, ["CaseEscalationServiceTest"], ticket_key=KEY)
    say(f"outcome = {tests['outcome']}, passing = {tests['passing']}, failing = {tests['failing']}")
    for test in tests["tests"]:
        say(f"  {test['outcome']:4} {test['name']}")

    step("re-check the symptom in data")
    before_fix = inv.soql_query(SANDBOX, NOT_HIGH_QUERY, ticket_key=KEY, save_as="sandbox-premium-cases-not-high")
    say(f"Premium cases that are not High: {before_fix['total_size']} "
        f"({', '.join(r['Id'] for r in before_fix['records'])})")
    say("-> as expected: the code fix does not repair records that were saved before it.")

    step("update_records (data correction, a separate approved step)")
    updated = inv.update_records(
        SANDBOX, KEY, "Case", [{"id": case_id, "fields": {"Priority": "High"}} for case_id in WRONG_CASES],
        reason="DEMO-101: correct the cases saved with the wrong priority")
    say(f"change {updated['change_id']}: status = {updated['status']}, verified = {updated['verified']}")
    for record in updated["records"]:
        priority = record["fields"]["Priority"]
        say(f"  {record['id']}: Priority {priority['before']} -> {priority['after']}")
    after_fix = inv.soql_query(SANDBOX, NOT_HIGH_QUERY, ticket_key=KEY, save_as="sandbox-premium-cases-not-high-after")
    say(f"Premium cases that are not High: {after_fix['total_size']}")

    step("the same write on production")
    say(refused(lambda: inv.deploy_component(PROD, KEY, FIX_COMPONENTS, reason="DEMO-101: must be refused")))

    step("rollback_change (records, then the deploy)")
    rolled_records = inv.rollback_change(updated["change_id"], reason="DEMO-101: demonstration finished")
    say(f"change {rolled_records['change_id']}: status = {rolled_records['status']}, "
        f"restored = {len(rolled_records['restored_record_ids'])} records")
    rolled = inv.rollback_change(deployed["change_id"], reason="DEMO-101: demonstration finished")
    say(f"change {rolled['change_id']}: status = {rolled['status']}, verified = {rolled['verified']}, "
        f"restored = {', '.join(sorted(rolled['restored_components']))}")
    tests = inv.run_apex_tests(SANDBOX, ["CaseEscalationServiceTest"])
    say(f"run_apex_tests: outcome = {tests['outcome']}, tests_ran = {tests['tests_ran']} (the new test is gone)")
    final = inv.soql_query(SANDBOX, NOT_HIGH_QUERY)
    say(f"Premium cases that are not High: {final['total_size']} (back to the starting state)")

    step("list_changes")
    for change in reversed(inv.list_changes(KEY)["changes"]):
        say(f"  {change['change_id']}  {change['kind']:8} {change['status']}")

    step("audit_log (oldest first)")
    for entry in reversed(inv.audit_entries(KEY, limit=100)["entries"]):
        say(f"  {entry['operation']:22} {entry['outcome']:9} {entry['org_target']} ({entry['classification']})")
    return [normalize(line) for line in lines]
