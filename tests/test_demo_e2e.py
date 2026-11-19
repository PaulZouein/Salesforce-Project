"""End-to-end replay of the DEMO-101 demonstration against the simulated orgs.

``samples/DEMO-101/expected/`` holds what the investigation produced. These tests replay the same
tool sequence, check that the facts the report relies on are what the tools return, and validate
the stored report against the replayed evidence.

Regenerate the stored apply-loop transcript with::

    SFTI_UPDATE_SAMPLES=1 python -m pytest tests/test_demo_e2e.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from core.app import Investigator
from core.errors import ProductionWriteBlockedError, WriteModeDisabledError
from simulator.build import DEFAULT_ORGS, build_state

from . import demo_flow
from .conftest import FIX_COMPONENTS, SERVICE, Project, Sim
from .demo_flow import KEY, PROD, SANDBOX, WRONG_CASES

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = ROOT / "samples" / "DEMO-101" / "expected"
EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


@pytest.fixture
def demo_sim(tmp_path) -> Sim:
    state = tmp_path / "demo-state"
    return Sim(state, build_state(state, orgs=DEFAULT_ORGS))


@pytest.fixture
def demo(demo_sim, project):
    inv = Investigator(root=project.root, cli=demo_sim.cli())
    results = demo_flow.investigate(inv, project.ticket(KEY))
    return inv, results


install_expected = demo_flow.demo.install_expected


# --- the investigation ------------------------------------------------------------------


def test_scope_and_classification(demo):
    _, r = demo
    assert r["config"]["mode"] == "advise"
    assert (r["prod"]["classification"], r["prod"]["writes_allowed"]) == ("production", False)
    assert (r["sandbox"]["classification"], r["sandbox"]["writes_allowed"]) == ("sandbox", True)
    assert r["earlier_tickets"]["match_count"] == 0


def test_records_show_the_batch_pattern(demo):
    """E1/E2: wrong cases are imported Premium cases that follow a Standard case of the same batch."""
    _, r = demo
    cases = r["cases"]["records"]
    assert len(cases) == 10
    imported = [c for c in cases if c["CreatedById"] == "005Ws000001IntgIAB"]
    batches: dict[str, list[dict]] = {}
    for case in imported:
        batches.setdefault(case["CreatedDate"], []).append(case)
    assert [len(b) for b in batches.values()] == [4, 2, 2]
    wrong, right = [], []
    for batch in batches.values():
        standard_seen = False
        for case in sorted(batch, key=lambda c: c["CaseNumber"]):
            tier = case["Account"]["Support_Tier__c"]
            if tier != "Premium":
                standard_seen = True
                continue
            assert case["Priority"] == ("Medium" if standard_seen else "High"), case["CaseNumber"]
            (wrong if standard_seen else right).append(case["Id"])
    assert wrong == WRONG_CASES
    assert right == ["500Ws00000C0001IAA", "500Ws00000C0009IAA", "500Ws00000C0010IAA"]
    agent_cases = [c for c in cases if c["CreatedById"] != "005Ws000001IntgIAB"]
    assert len(agent_cases) == 2 and all(c["Priority"] == "High" for c in agent_cases)
    assert [c["Id"] for c in r["not_high"]["records"]] == WRONG_CASES


def test_code_path_and_the_line(demo):
    """E3-E7, E16, E19."""
    inv, r = demo
    assert [t["Name"] for t in r["automation"]["triggers"]] == ["CaseTrigger"]
    assert [f["ApiName"] for f in r["automation"]["record_triggered_flows"]] == ["Case_Premium_Notification"]
    assert r["automation"]["validation_rules"] == [] and r["automation"]["workflow_rules"] == []
    assert [(l["rule"], l["path"], l["line"]) for l in r["scan"]["leads"]] == [("return_inside_loop", SERVICE, 30)]
    lines = r["service"]["content"].splitlines()
    assert lines[0].startswith("26:         for (Case c : cases)")
    assert lines[4].strip() == "30:                 return;"
    assert lines[6].strip() == "32:             c.Priority = HIGH_PRIORITY;"
    trigger = inv.read_file(KEY, "metadata/force-app/main/default/triggers/CaseTrigger.trigger", start_line=5, end_line=7)
    assert "CaseTriggerHandler.beforeInsert(Trigger.new);" in trigger["content"]
    handler = inv.read_file(KEY, "metadata/force-app/main/default/classes/CaseTriggerHandler.cls")["content"]
    assert "7:         CaseEscalationService.applyPremiumPriority(newCases);" in handler
    assert "14:             if (c.AccountId != oldCase.AccountId) {" in handler
    assert [(m["path"].rsplit("/", 1)[-1], m["line"]) for m in r["callers"]["matches"]] == [
        ("CaseEscalationService.cls", 11), ("CaseTriggerHandler.cls", 7), ("CaseTriggerHandler.cls", 19)]
    assert sorted(d["name"] for d in r["dependencies"]["used_by"]) == ["CaseEscalationServiceTest", "CaseTriggerHandler"]
    flow = r["flow"]
    assert flow["start"]["trigger_type"] == "RecordAfterSave" and flow["start"]["record_trigger_type"] == "Create"
    assert flow["elements"][0]["outcomes"][0]["conditions"] == ["$Record.Priority EqualTo 'High'"]
    assert {e["kind"] for e in flow["elements"]} == {"decision", "action"}  # nothing writes the record
    assert r["mixed_batch_test_before_fix"]["match_count"] == 0


def test_log_shows_statement_30_and_never_32(demo):
    """E8-E12."""
    inv, r = demo
    analysis = r["log_analysis"]
    assert analysis["triggers"][0] == {"trigger": "CaseTrigger", "object": "Case", "event": "BeforeInsert", "line": 4}
    service = next(u for u in analysis["apex_statements"]["by_method"] if "applyPremiumPriority" in u["in"])
    assert service["source_lines"][-3:] == [27, 28, 30] and 32 not in service["source_lines"]
    assert [(d["line"], d["result"]) for d in analysis["flow_decisions"]] == [(46, False), (51, False)]
    assert analysis["dml"]["statements"][0]["line"] == 40 and analysis["dml"]["statements"][0]["rows"] == 2
    log = inv.read_file(KEY, r["log"]["saved_to"])["content"].splitlines()
    assert "APEX_CODE,FINEST" in log[0]
    assert '"Support_Tier__c":"Standard"' in log[21] and "001Ws00000A1b2dIAB" in log[21]
    assert log[22].endswith("STATEMENT_EXECUTE|[28]") and log[23].endswith("STATEMENT_EXECUTE|[30]")
    assert "METHOD_EXIT" in log[24] and "applyPremiumPriority" in log[24]
    # The case of that batch whose account is Standard is the first one; the Premium one stayed Medium.
    batch = [c for c in r["cases"]["records"] if c["CreatedDate"].startswith("2026-11-11T02:00:09")]
    assert [(c["AccountId"], c["Priority"]) for c in batch] == [
        ("001Ws00000A1b2dIAB", "Medium"), ("001Ws00000A1b2eIAB", "Medium")]


def test_timeline(demo):
    """E13-E15, E18."""
    _, r = demo
    trail = {(t["section"], t["when"][:16]) for t in r["changes"]["setup_audit_trail"]}
    assert trail == {("Apex Class", "2026-11-09T16:42"), ("Connected Apps", "2026-11-09T11:20")}
    dates = {c["Name"]: c["LastModifiedDate"][:10] for c in r["class_dates"]["records"]}
    assert dates == {"CaseEscalationService": "2026-11-09", "CaseEscalationServiceTest": "2026-08-18",
                     "CaseTriggerHandler": "2026-08-18"}
    assert [(f["VersionNumber"], f["Status"], f["LastModifiedDate"][:10]) for f in r["flow_versions"]["records"]] == [
        (2, "Active", "2026-10-20")]
    sandbox_dates = {c["Name"]: c["LastModifiedDate"][:10] for c in r["sandbox_class_dates"]["records"]}
    assert sandbox_dates == dates
    first_failure = min(c["CreatedDate"] for c in r["cases"]["records"] if c["Id"] in WRONG_CASES)
    assert "2026-11-09T16:42" < first_failure[:16]


def test_existing_tests_pass_without_covering_the_scenario(demo):
    """E17."""
    _, r = demo
    assert (r["tests"]["outcome"], r["tests"]["passing"], r["tests"]["failing"]) == ("Passed", 2, 0)
    assert r["tests"]["classification"] == "sandbox"


def test_fix_diff_matches_the_stored_sample(demo, project):
    _, r = demo
    stored = (EXPECTED / "fix.diff").read_text(encoding="utf-8")
    assert r["diff"]["diff"] == stored
    assert (project.ticket(KEY) / "fix.diff").read_text(encoding="utf-8") == stored
    assert stored.count("\n-") == 2 and "-                return;\n+                continue;\n" in stored
    assert "mixedBatchEscalatesEveryPremiumCase" in stored


def test_stored_report_validates_against_the_replayed_evidence(demo, project):
    inv, _ = demo
    install_expected(project.ticket(KEY))
    check = inv.validate_report(KEY)
    assert check["ok"] is True, check["problems"]
    assert check["root_cause_status"] == "confirmed"
    assert check["citations"] == {"valid": 23, "broken": 0}
    # The only warning is the deliberate one: cited files now also hold the drafted fix.
    assert len(check["warnings"]) == 1 and "carry local edits" in check["warnings"][0]
    comment = inv.validate_comment(KEY)
    assert comment["ok"] is True and comment["warnings"] == [] and comment["words"] < 300


def test_every_evidence_row_of_the_report_is_cited_and_used(demo, project):
    install_expected(project.ticket(KEY))
    report = (project.ticket(KEY) / "report.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (E\d+) \| `([^`]+)` \|", report, flags=re.MULTILINE)
    assert [n for n, _ in rows] == [f"E{i}" for i in range(1, 20)]
    root_cause = report[report.index("## Root cause"):report.index("## Fix")]
    for number in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9", "E10", "E12"):
        assert re.search(rf"\b{number}\b", root_cause), number
    for case_id in WRONG_CASES:
        assert case_id in report


def test_report_states_what_was_simulated_and_not_run():
    report = (EXPECTED / "report.md").read_text(encoding="utf-8")
    assert "Simulated orgs" in report and "does not execute Apex" in report
    assert "advise" in report and "Nothing was deployed" in report
    assert "It was not run" in report
    assert "**Hypothesis:**" in report  # what is not proven is labelled


def test_stored_html_is_the_export_of_the_stored_report(demo, project):
    inv, _ = demo
    install_expected(project.ticket(KEY))
    inv.export_report_html(KEY)
    if os.environ.get("SFTI_UPDATE_EXPECTED") == "1":   # after a deliberate change to the HTML export
        shutil.copyfile(project.ticket(KEY) / "report.html", EXPECTED / "report.html")

    def body(text: str) -> str:
        text = re.sub(r"queries/\d{8}-\d{6}-", "queries/<ts>-", text)
        return re.sub(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", "Generated <when>", text)

    assert body((project.ticket(KEY) / "report.html").read_text(encoding="utf-8")) == \
        body((EXPECTED / "report.html").read_text(encoding="utf-8"))
    assert "Evidence check passed · 23 citations verified" in (EXPECTED / "report.html").read_text(encoding="utf-8")


# --- privacy ----------------------------------------------------------------------------


def test_stored_outputs_contain_no_personal_data(demo_sim):
    records = demo_sim.records(PROD)
    personal: set[str] = set()
    for case in records["Case"]:
        for field in ("SuppliedEmail", "SuppliedName", "SuppliedPhone"):
            if case.get(field):
                personal.add(case[field])
    for user in records["User"]:
        personal.update({user["Name"], user["Username"]})
    assert len(personal) >= 7
    for name in ("ticket.md", "report.md", "report.html", "comment.md", "fix.diff", "apply-loop-transcript.txt"):
        text = (EXPECTED / name).read_text(encoding="utf-8")
        for value in personal:
            assert value not in text, f"{name} contains personal data from the org"
        assert not EMAIL.search(text), f"{name} contains an email address"
    assert "Reporter" not in (EXPECTED / "ticket.md").read_text(encoding="utf-8")


def test_saved_evidence_of_the_replay_is_redacted(demo, project, demo_sim):
    personal = {u["Name"] for u in demo_sim.records(PROD)["User"]} | \
               {c["SuppliedEmail"] for c in demo_sim.records(PROD)["Case"] if c.get("SuppliedEmail")}
    for path in (project.ticket(KEY) / "queries").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in personal), path.name


# --- guards during the demonstration ----------------------------------------------------


def test_nothing_was_written_to_either_org_and_writes_are_refused(demo_sim, project):
    before = {alias: demo_sim.fingerprint(alias) for alias in (PROD, SANDBOX)}
    inv = Investigator(root=project.root, cli=demo_sim.cli())
    demo_flow.investigate(inv, project.ticket(KEY))
    with pytest.raises(WriteModeDisabledError):  # advise mode: the fix is handed over, not deployed
        inv.deploy_component(SANDBOX, KEY, FIX_COMPONENTS, reason="DEMO-101: not in advise mode")
    with pytest.raises(ProductionWriteBlockedError):
        inv.deploy_component(PROD, KEY, FIX_COMPONENTS, reason="DEMO-101: never on production")
    with pytest.raises(ProductionWriteBlockedError):
        inv.update_records(PROD, KEY, "Case", [{"id": WRONG_CASES[0], "fields": {"Priority": "High"}}],
                           reason="DEMO-101: never on production")
    assert {alias: demo_sim.fingerprint(alias) for alias in (PROD, SANDBOX)} == before
    assert [e["outcome"] for e in inv.audit_entries(KEY)["entries"]][:3] == ["blocked"] * 3
    assert inv.list_changes(KEY)["changes"] == []


# --- apply mode -------------------------------------------------------------------------


def test_apply_loop_on_the_simulated_sandbox(demo_sim, tmp_path):
    """Validate, deploy, test, re-check, roll back: the sandbox ends exactly as it started."""
    root = tmp_path / "apply-project"
    root.mkdir()
    project = Project(root)
    project.write_config(mode="apply")  # a temporary root: the repository's own config stays 'advise'
    inv = Investigator(root=root, cli=demo_sim.cli())
    inv.init_ticket(KEY)
    before = {alias: demo_sim.fingerprint(alias) for alias in (PROD, SANDBOX)}

    transcript = "\n".join(demo_flow.apply_loop(inv, project.ticket(KEY))).strip() + "\n"

    assert {alias: demo_sim.fingerprint(alias) for alias in (PROD, SANDBOX)} == before
    statuses = [(c["kind"], c["status"]) for c in reversed(inv.list_changes(KEY)["changes"])]
    assert statuses == [("deploy", "failed"), ("deploy", "validated"), ("deploy", "rolled_back"),
                        ("records", "rolled_back")]
    assert "REFUSED [deploy_failed]" in transcript and "REFUSED [production_write_blocked]" in transcript
    assert "outcome = Passed, passing = 3, failing = 0" in transcript

    stored = EXPECTED / "apply-loop-transcript.txt"
    if os.environ.get("SFTI_UPDATE_SAMPLES") == "1":
        stored.write_text(transcript, encoding="utf-8")
    assert transcript == stored.read_text(encoding="utf-8")


def test_repository_config_was_not_switched_to_apply():
    assert json.loads((ROOT / "sfti.config.json").read_text(encoding="utf-8"))["mode"] == "advise"
