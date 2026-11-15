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
