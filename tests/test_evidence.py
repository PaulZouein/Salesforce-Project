from __future__ import annotations

import json

from .conftest import SERVICE


def test_evidence_index_lists_what_was_saved(inv, sim):
    inv.init_ticket("T-1")
    empty = inv.evidence_index("T-1")
    assert empty["by_kind"] == {"ticket": 1} and empty["retrieved_source"]["files"] == []

    inv.retrieve_components("prod", "T-1", ["ApexClass:CaseEscalationService", "Flow:Case_Premium_Notification"])
    query = inv.soql_query("prod", "SELECT Id, Priority, SuppliedEmail FROM Case WHERE Priority = 'Medium'",
                           ticket_key="T-1", save_as="medium-cases")
    inv.soql_query("prod", "SELECT COUNT() FROM Case", ticket_key="T-1", save_as="count")
    log = inv.get_debug_log("prod", "07LWs00000L0001MAA", "T-1")
    tests = inv.run_apex_tests("sandbox", ["CaseEscalationServiceTest"], ticket_key="T-1")

    out = inv.evidence_index("T-1")
    assert out["by_kind"] == {"query_result": 2, "apex_test_run": 1, "debug_log": 1, "ticket": 1}
    by_path = {a["path"]: a for a in out["artifacts"]}
    saved = by_path[query["saved_to"]]
    assert saved["cite"] == f"`{query['saved_to']}`"
    assert saved["description"].startswith("5 row(s) from prod at ") and "WHERE Priority = 'Medium'" in saved["description"]
    assert saved["redacted_fields"] == ["SuppliedEmail"]
    count = next(a for a in out["artifacts"] if a["path"].endswith("-count.json"))
    assert count["description"].startswith("10 row(s)")
    assert "2 passing, 0 failing" in by_path[tests["saved_to"]]["description"]
    assert by_path[log["saved_to"]]["cite"] == f"`{log['saved_to']}:<line>`"
    assert "56 lines" in by_path[log["saved_to"]]["description"] and "CaseTrigger" in by_path[log["saved_to"]]["description"]
    assert SERVICE in out["retrieved_source"]["files"]
    assert not any(f.endswith(".cls-meta.xml") for f in out["retrieved_source"]["files"])
    assert any(f.endswith(".flow-meta.xml") for f in out["retrieved_source"]["files"])
    assert out["metadata_commits"][0]["subject"].startswith("retrieve: ")
    # Descriptions are built from redacted files: no personal data from the org.
    text = json.dumps(out)
    for case in sim.records("prod")["Case"]:
        assert not case.get("SuppliedEmail") or case["SuppliedEmail"] not in text
    assert not any(u["Name"] in text for u in sim.records("prod")["User"])


def test_answers_of_the_look_up_tools_can_be_kept_and_cited(inv, sim):
    """What these tools return used to exist only in the conversation, so a report could not cite it."""
    inv.init_ticket("T-2")
    automation = inv.describe_object("prod", "Case", include=["automation"], ticket_key="T-2", save_as="what runs on Case")
    used_by = inv.find_dependencies("prod", "CaseEscalationService", ticket_key="T-2")
    changes = inv.recent_org_changes("prod", "2026-11-01", ticket_key="T-2", save_as="changes-in-november")
    history = inv.record_history("prod", "Case", "500Ws00000C0003IAB", ticket_key="T-2")
    saved = [automation["saved_to"], used_by["saved_to"], changes["saved_to"], history["saved_to"]]
    assert [name.split("-", 2)[2] for name in saved] == [
        "what-runs-on-Case.json", "dependencies-CaseEscalationService.json", "changes-in-november.json",
        "history-500Ws00000C0003IAB.json"]
    assert all(name.startswith("queries/20") for name in saved)

    stored = json.loads((inv.workspace.ticket_dir("T-2") / automation["saved_to"]).read_text(encoding="utf-8"))
    assert stored["tool"] == "describe_object" and stored["org"] == "prod" and stored["executed_at"].endswith("Z")
    assert stored["asked"] == {"object": "Case", "include": ["automation"]}
    assert stored["result"]["automation"] == automation["automation"] and "saved_to" not in stored["result"]

    out = inv.evidence_index("T-2")
    assert out["by_kind"] == {"tool_result": 4, "ticket": 1}
    by_path = {a["path"]: a for a in out["artifacts"]}
    assert by_path[automation["saved_to"]]["description"].startswith("Answer of describe_object from prod at 20")
    assert by_path[automation["saved_to"]]["description"].endswith(": object Case")
    assert "name CaseEscalationService" in by_path[used_by["saved_to"]]["description"]
    assert by_path[changes["saved_to"]]["cite"] == f"`{changes['saved_to']}`"

    # A report that cites them passes the evidence check.
    report = inv.workspace.ticket_dir("T-2") / "report.md"
    inv.write_template("T-2", "report")
    text = report.read_text(encoding="utf-8")
    report.write_text(text + f"\n\nNo validation rule on Case: `{automation['saved_to']}`. "
                             f"Last change: `{changes['saved_to']}`.\n", encoding="utf-8")
    check = inv.validate_report("T-2")
    assert not [p for p in check["problems"] if "queries/" in p]
    # Kept files hold what the tools return: Ids and metadata, never a user's name or address.
    kept = "".join((inv.workspace.ticket_dir("T-2") / name).read_text(encoding="utf-8") for name in saved)
    assert not any(u["Name"] in kept or u.get("Email", "@none") in kept for u in sim.records("prod")["User"])


def test_look_up_tools_save_nothing_unless_asked(inv):
    inv.init_ticket("T-3")
    out = inv.describe_object("prod", "Case", include=["automation"])
    assert "saved_to" not in out and "saved_to" not in inv.find_dependencies("prod", "CaseEscalationService")
    assert inv.evidence_index("T-3")["by_kind"] == {"ticket": 1}
