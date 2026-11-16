"""The org memory: what one investigation learns is available to the next, and is double-checked."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.app import Investigator
from core.errors import InputValidationError
from core.knowledge import OrgKnowledge, outline_apex, outline_flow

from .conftest import SERVICE

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "planted-bug" / "force-app" / "main" / "default"
SERVICE_SPEC = "ApexClass:CaseEscalationService"
HANDLER_SPEC = "ApexClass:CaseTriggerHandler"
COMPONENTS = ["ApexTrigger:CaseTrigger", HANDLER_SPEC, SERVICE_SPEC, "Flow:Case_Premium_Notification"]
PROD_ID = "00DWs000000PrdAMAS"


def learn(inv, org="prod", key="T-1"):
    return inv.retrieve_components(org, key, COMPONENTS)


def change_in_org(sim, alias="prod", when="2026-11-20T09:00:00.000+0000"):
    """Someone edits the service in the org: new source and a new last-modified date."""
    source = sim.source(alias, "classes/CaseEscalationService.cls")
    source.write_text(source.read_text().replace("PREMIUM_TIER = 'Premium'", "PREMIUM_TIER = 'Platinum'"))
    records = sim.records(alias)
    for row in records["ApexClass"]:
        if row["Name"] == "CaseEscalationService":
            row["LastModifiedDate"] = when
    (sim.org_dir(alias) / "records.json").write_text(json.dumps(records))


# --- outlines -----------------------------------------------------------------------------


def test_apex_outline_of_the_sample_service():
    outline = outline_apex((SAMPLE / "classes" / "CaseEscalationService.cls").read_text())
    assert outline["kind"] == "class" and outline["is_test"] is False and outline["lines"] == 35
    assert outline["methods"] == [{"name": "applyPremiumPriority", "line": 11}]
    assert outline["objects_queried"] == ["Account"]
    assert outline["fields_assigned"] == ["Priority"]
    assert outline["dml_lines"] == []


def test_apex_outline_of_handler_trigger_and_test_class():
    handler = outline_apex((SAMPLE / "classes" / "CaseTriggerHandler.cls").read_text())
    assert [m["name"] for m in handler["methods"]] == ["beforeInsert", "beforeUpdate"]
    assert handler["calls"] == ["CaseEscalationService.applyPremiumPriority"]
    trigger = outline_apex((SAMPLE / "triggers" / "CaseTrigger.trigger").read_text(), is_trigger=True)
    assert trigger["object"] == "Case" and trigger["events"] == ["before insert", "before update"]
    assert "CaseTriggerHandler.beforeInsert" in trigger["calls"]
    test_class = outline_apex((SAMPLE / "classes" / "CaseEscalationServiceTest.cls").read_text())
    assert test_class["is_test"] is True and test_class["dml_lines"]


def test_comments_and_strings_do_not_pollute_the_outline():
    outline = outline_apex("""public class X {
    // Helper.run(); record.Status = 'x'; [SELECT Id FROM Lead]
    public void go() { String s = 'Other.call() FROM Contact'; }
}""")
    assert outline["calls"] == [] and outline["objects_queried"] == [] and outline["fields_assigned"] == []


def test_flow_outline():
    outline = outline_flow((SAMPLE / "flows" / "Case_Premium_Notification.flow-meta.xml").read_text())
    assert (outline["object"], outline["trigger_type"], outline["record_trigger_type"]) == \
        ("Case", "RecordAfterSave", "Create")
    assert outline["reads"] == ["$Record.Priority"] and outline["writes"] == []
    assert outline["actions"] == ["customNotificationAction"] and outline["status"] == "Active"
    assert outline_flow("<not xml")["unreadable"] is True


# --- learning -----------------------------------------------------------------------------


def test_retrieve_teaches_the_memory(inv, project):
    out = learn(inv)
    memory = out["org_memory"]
    assert sorted(memory["new"]) == sorted(COMPONENTS)
    assert memory["changed_since_last_seen"] == [] and memory["unchanged"] == 0

    stored = json.loads((project.root / "knowledge" / "00DWs000000PrdA.json").read_text())
    assert stored["aliases"] == ["prod"]
    service = stored["components"][SERVICE_SPEC]
    assert service["path"] == SERVICE and len(service["sha256"]) == 64
    assert service["org_last_modified"].startswith("2026-11-09T16:42:07")
    assert service["seen_in_tickets"] == ["T-1"]
    assert service["outline"]["fields_assigned"] == ["Priority"]
    assert stored["components"]["Flow:Case_Premium_Notification"]["org_last_modified"].startswith("2026-10-20")
    assert stored["objects"]["Case"] == {"triggers": ["CaseTrigger"], "flows": ["Case_Premium_Notification"]}
    # Calls found in source become links between components that are known.
    assert {"from": HANDLER_SPEC, "to": SERVICE_SPEC, "kind": "calls", "origin": "source"} in stored["edges"]
    assert {"from": "ApexTrigger:CaseTrigger", "to": HANDLER_SPEC, "kind": "calls", "origin": "source"} in stored["edges"]


def test_second_retrieve_is_unchanged_and_adds_the_ticket(inv):
    learn(inv)
    again = inv.retrieve_components("prod", "T-2", COMPONENTS)["org_memory"]
    assert again == {"new": [], "changed_since_last_seen": [], "unchanged": 4}
    component = inv.knowledge.load(PROD_ID)["components"][SERVICE_SPEC]
    assert component["seen_in_tickets"] == ["T-1", "T-2"] and "history" not in component


def test_describe_and_dependencies_teach_the_memory(inv):
    learn(inv)
    inv.describe_object("prod", "Case", include=["automation"])
    inv.find_dependencies("prod", "CaseEscalationService")
    data = inv.knowledge.load(PROD_ID)
    case = data["objects"]["Case"]
    assert case["triggers"] == ["CaseTrigger"] and case["flows"] == ["Case_Premium_Notification"]
    assert case["validation_rules"] == [] and case["workflow_rules"] == [] and case["automation_checked_at"]
    assert {"from": SERVICE_SPEC, "to": "CustomField:Support_Tier", "kind": "uses", "origin": "dependency_api"} \
        in data["edges"]
    assert {"from": "ApexClass:CaseEscalationServiceTest", "to": SERVICE_SPEC, "kind": "uses",
            "origin": "dependency_api"} in data["edges"]


def test_memory_is_per_org_and_keyed_by_org_id_not_alias(inv, sim, project):
    learn(inv, "prod")
    learn(inv, "sandbox", "T-2")
    files = sorted(p.name for p in (project.root / "knowledge").iterdir())
    assert files == ["00DWs000000PrdA.json", "00DWs000000SbxA.json"]
    assert {o["aliases"][0] for o in inv.knowledge.known_orgs()} == {"prod", "sandbox"}

    # The alias "prod" is re-pointed at another org: a fresh Investigator files nothing under the old org.
    org_file = sim.org_dir("prod") / "org.json"
    org = json.loads(org_file.read_text())
    org["orgId"] = "00DWs000000NewAMAS"
    org_file.write_text(json.dumps(org))
    other = Investigator(root=project.root, cli=sim.cli())
    assert other.recall_org_knowledge("prod", "case priority")["totals"] == {"notes": 0, "components": 0, "objects": 0}
    assert other.retrieve_components("prod", "T-3", [SERVICE_SPEC])["org_memory"]["new"] == [SERVICE_SPEC]


def test_a_memory_failure_never_fails_the_tool(inv, project, monkeypatch):
    (project.root / "knowledge").write_text("a file where the folder should be")
    out = learn(inv)
    assert out["retrieved_count"] == 7
    assert "not updated" in out["org_memory"]["skipped"]
    assert inv.describe_object("prod", "Case", include=["automation"])["automation"]["triggers"]
    assert inv.find_dependencies("prod", "CaseEscalationService")["used_by"]


def test_damaged_memory_file_is_set_aside(inv, project):
    learn(inv)
    path = project.root / "knowledge" / "00DWs000000PrdA.json"
    path.write_text("{ not json")
    assert inv.knowledge.load(PROD_ID)["components"] == {}
    assert path.with_suffix(".damaged.json").is_file()
    assert sorted(learn(inv, key="T-2")["org_memory"]["new"]) == sorted(COMPONENTS)


# --- notes --------------------------------------------------------------------------------

FACT = ("CaseEscalationService.applyPremiumPriority sets Priority to High for cases whose account has "
        "Support_Tier__c = Premium. It is called by CaseTriggerHandler on insert and when the account changes.")


def remember(inv, text=FACT, **kwargs):
    defaults = dict(topics=["Case Priority", "premium", "escalation"], components=[SERVICE_SPEC, HANDLER_SPEC],
                    evidence=[f"`{SERVICE}:26-33`"], ticket_key="T-1")
    return inv.remember_org_fact("prod", text, **{**defaults, **kwargs})["remembered"]


def test_remember_and_recall(inv):
    learn(inv)
    note = remember(inv)
    assert note["id"].startswith("N-") and note["topics"] == ["case priority", "premium", "escalation"]
    assert note["evidence"] == [f"{SERVICE}:26-33"] and note["stale"] is False
    assert len(note["fingerprints"][SERVICE_SPEC]) == 64

    found = inv.recall_org_knowledge("prod", "premium customer cases get the wrong priority")
    assert [n["id"] for n in found["notes"]] == [note["id"]]
    assert found["components"][0]["component"] == SERVICE_SPEC
    assert found["components"][0]["freshness"]["status"] == "unchanged"
    assert found["freshness"]["checked"] is True and found["freshness"]["counts"] == {"unchanged": 4}
    assert SERVICE_SPEC in found["suggested_retrieve"] and HANDLER_SPEC in found["suggested_retrieve"]
    assert "lead, not evidence" in found["how_to_use"]
    assert [o["object"] for o in found["objects"]] == ["Case"]


def test_recall_finds_components_by_what_they_do(inv):
    learn(inv)
    # No notes at all: the outline alone answers "what writes Priority?" and "what runs on Case?".
    by_field = inv.recall_org_knowledge("prod", "Priority", check=False)
    assert SERVICE_SPEC in [c["component"] for c in by_field["components"]]
    assert "Flow:Case_Premium_Notification" in [c["component"] for c in by_field["components"]]
    by_words = inv.recall_org_knowledge("prod", "notification flow", check=False)
    assert by_words["components"][0]["component"] == "Flow:Case_Premium_Notification"
    assert by_words["freshness"] == {"checked": False, "reason": "not requested"}
    nothing = inv.recall_org_knowledge("prod", "voice calls telephony")
    assert nothing["notes"] == [] and nothing["components"] == [] and "Nothing remembered" in nothing["note"]


def test_recall_before_anything_was_learned(inv):
    out = inv.recall_org_knowledge("prod", "case priority")
    assert out["totals"] == {"notes": 0, "components": 0, "objects": 0}
    assert out["freshness"]["checked"] is False
    assert inv.org_map("prod")["counts"]["components"] == 0


@pytest.mark.parametrize("text,kwargs", [
    ("", {}),
    ("too short", {}),
    ("x" * 1300, {}),
    ("Ask jane.doe@customer.test about the escalation rules on Case.", {}),
    (FACT, {"topics": ["owner is jane.doe@customer.test"]}),
    (FACT, {"components": ["CaseEscalationService"]}),
    (FACT, {"components": ["ApexClass:Bad Name; x"]}),
    (FACT, {"topics": [f"topic {i}" for i in range(13)]}),
    (FACT, {"supersedes": "N-missing"}),
])
def test_note_validation(inv, text, kwargs):
    learn(inv)
    with pytest.raises(InputValidationError):
        remember(inv, text, **kwargs)
    assert inv.knowledge.load(PROD_ID)["notes"] == []


def test_supersede_and_forget(inv):
    learn(inv)
    old = remember(inv)
    new = remember(inv, FACT + " Fixed in DEMO-101 to continue instead of return.", supersedes=old["id"])
    found = inv.recall_org_knowledge("prod", "premium priority", check=False)
    assert [n["id"] for n in found["notes"]] == [new["id"]]
    assert inv.org_map("prod")["counts"]["notes"] == 1
    assert inv.forget_org_fact("prod", new["id"]) == {"removed": new["id"], "notes_left": 1}
    with pytest.raises(InputValidationError):
        inv.forget_org_fact("prod", "N-nothere")


def test_note_about_a_component_never_retrieved_is_accepted_with_a_warning(inv):
    note = remember(inv, components=["ApexClass:NeverSeen"])
    assert "cannot be detected" in note["warning"] and note["fingerprints"] == {"ApexClass:NeverSeen": None}


# --- the double check ---------------------------------------------------------------------


def test_recall_flags_what_changed_in_the_org_since_it_was_learned(inv, sim):
    learn(inv)
    note = remember(inv)
    change_in_org(sim)

    found = inv.recall_org_knowledge("prod", "premium priority")
    by_spec = {c["component"]: c["freshness"] for c in found["components"]}
    assert by_spec[SERVICE_SPEC]["status"] == "changed"
    assert by_spec[SERVICE_SPEC]["remembered_last_modified"].startswith("2026-11-09")
    assert by_spec[SERVICE_SPEC]["org_last_modified"].startswith("2026-11-20")
    assert "read it again" in by_spec[SERVICE_SPEC]["reason"]
    assert by_spec[HANDLER_SPEC]["status"] == "unchanged"
    recalled = found["notes"][0]
    assert recalled["id"] == note["id"] and recalled["stale"] is True
    assert recalled["stale_because"] == [f"{SERVICE_SPEC} was modified in the org after this was learned"]
    # The flow also matches the words ("premium"), so three components were double-checked.
    assert found["freshness"]["counts"] == {"changed": 1, "unchanged": 2}


def test_retrieve_detects_the_change_by_content_and_refreshes_the_memory(inv, sim):
    learn(inv)
    note = remember(inv)
    change_in_org(sim)

    memory = inv.retrieve_components("prod", "T-2", COMPONENTS)["org_memory"]
    assert memory["new"] == [] and memory["unchanged"] == 3
    assert memory["changed_since_last_seen"] == [
        {"component": SERVICE_SPEC, "last_seen": memory["changed_since_last_seen"][0]["last_seen"],
         "seen_in_tickets": ["T-1"]}]
    assert memory["notes_now_flagged_stale"] == [note["id"]]
    assert "read them again" in memory["note"]

    data = inv.knowledge.load(PROD_ID)
    component = data["components"][SERVICE_SPEC]
    assert len(component["history"]) == 1 and component["history"][0]["ticket_key"] == "T-2"
    assert component["org_last_modified"].startswith("2026-11-20")
    # The component itself is current again; the old note stays flagged until it is replaced.
    after = inv.recall_org_knowledge("prod", "premium priority")
    assert {c["component"]: c["freshness"]["status"] for c in after["components"]}[SERVICE_SPEC] == "unchanged"
    assert after["notes"][0]["stale"] is True and "retrieved again and differs" in after["notes"][0]["stale_because"][0]

    fresh = remember(inv, FACT.replace("Premium", "Platinum"), supersedes=note["id"])
    final = inv.recall_org_knowledge("prod", "platinum priority")
    assert [n["id"] for n in final["notes"]] == [fresh["id"]] and final["notes"][0]["stale"] is False


def test_verify_reports_removed_components_and_types_it_cannot_check(inv, sim):
    inv.retrieve_components("prod", "T-1", COMPONENTS + ["CustomField:Account.Support_Tier__c"])
    records = sim.records("prod")
    records["ApexClass"] = [r for r in records["ApexClass"] if r["Name"] != "CaseTriggerHandler"]
    (sim.org_dir("prod") / "records.json").write_text(json.dumps(records))

    out = inv.verify_org_knowledge("prod")
    statuses = {spec: entry["status"] for spec, entry in out["components"].items()}
    assert statuses == {
        "ApexClass:CaseEscalationService": "unchanged", "ApexClass:CaseTriggerHandler": "not_found_in_org",
        "ApexTrigger:CaseTrigger": "unchanged", "Flow:Case_Premium_Notification": "unchanged",
        "CustomField:Account.Support_Tier__c": "not_checked",
    }
    assert out["counts"] == {"unchanged": 3, "not_found_in_org": 1, "not_checked": 1}
    assert "retrieve it to compare" in out["components"]["CustomField:Account.Support_Tier__c"]["reason"]


def test_verify_when_the_org_cannot_be_asked(inv, sim):
    learn(inv)
    records = sim.records("prod")
    del records["ApexClass"]
    (sim.org_dir("prod") / "records.json").write_text(json.dumps(records))
    out = inv.verify_org_knowledge("prod")
    assert out["components"][SERVICE_SPEC]["status"] == "not_checked"
    assert "could not be asked" in out["components"][SERVICE_SPEC]["reason"]
    assert out["components"]["ApexTrigger:CaseTrigger"]["status"] == "unchanged"


def test_double_check_only_reads_from_the_org(inv, sim):
    learn(inv)
    remember(inv)
    before = sim.fingerprint("prod")
    inv.recall_org_knowledge("prod", "premium priority")
    inv.verify_org_knowledge("prod")
    inv.org_map("prod")
    assert sim.fingerprint("prod") == before
    assert inv.audit.entries() == []  # no write was even attempted


# --- map and privacy ----------------------------------------------------------------------


def test_org_map(inv):
    learn(inv)
    inv.describe_object("prod", "Case", include=["automation"])
    remember(inv)
    out = inv.org_map("prod")
    assert out["aliases"] == ["prod"] and out["tickets"] == ["T-1"]
    assert out["counts"] == {"components": 4, "objects": 1, "links": 2, "notes": 1, "stale_notes": 0}
    assert sorted(out["components_by_type"]) == ["ApexClass", "ApexTrigger", "Flow"]
    assert out["topics"] == {"case priority": 1, "escalation": 1, "premium": 1}
    assert out["objects"]["Case"]["flows"] == ["Case_Premium_Notification"]


def test_memory_holds_no_record_data(inv, sim, project):
    """Queries, logs and test runs are not remembered: only metadata facts are."""
    learn(inv)
    inv.soql_query("prod", "SELECT Id, SuppliedEmail, CreatedBy.Name FROM Case", ticket_key="T-1",
                   reveal_fields=["SuppliedEmail"])
    inv.get_debug_log("prod", "07LWs00000L0001MAA", "T-1")
    inv.describe_object("prod", "Case", include=["automation"])
    remember(inv)
    text = (project.root / "knowledge" / "00DWs000000PrdA.json").read_text()
    records = sim.records("prod")
    for case in records["Case"]:
        assert case["Id"] not in text and case["CaseNumber"] not in text
        assert not case.get("SuppliedEmail") or case["SuppliedEmail"] not in text
    for user in records["User"]:
        assert user["Name"] not in text and user["Username"] not in text and user["Id"] not in text


def test_knowledge_folder_is_never_committed(inv, project):
    assert "knowledge/" in (ROOT / ".gitignore").read_text().splitlines()
    ignored = subprocess.run(["git", "check-ignore", "-q", "knowledge/00DWs000000PrdA.json"], cwd=ROOT)
    assert ignored.returncode == 0
    with pytest.raises(InputValidationError):
        OrgKnowledge(project.root).load("../../etc/passwd")


def test_org_info_says_what_is_remembered(inv):
    assert inv.org_info("prod")["org_memory"]["components"] == 0
    learn(inv)
    remember(inv)
    memory = inv.org_info("prod")["org_memory"]
    assert (memory["components"], memory["notes"]) == (4, 1) and "recall_org_knowledge" in memory["hint"]


# --- two programs write the same memory -----------------------------------------------------


def test_a_slow_double_check_does_not_undo_what_another_program_learned_meanwhile(inv, sim, project, monkeypatch):
    """The page checks the memory against the org (seconds) while the investigator saves a note."""
    import core.knowledge as knowledge_module

    learn(inv)
    other = OrgKnowledge(project.root)           # the second program, with its own view of the file
    ask_org = knowledge_module.fetch_last_modified

    def slow_org(cli, org, component_type, names):
        answer = ask_org(cli, org, component_type, names)
        if component_type == "ApexClass":        # in the middle of the check, the other program writes
            other.add_note(PROD_ID, "prod", "Case priority is decided in CaseEscalationService only.",
                           components=[SERVICE_SPEC])
        return answer

    monkeypatch.setattr(knowledge_module, "fetch_last_modified", slow_org)
    checked = inv.verify_org_knowledge("prod")
    assert checked["counts"] == {"unchanged": 4}
    data = inv.knowledge.load(PROD_ID)
    assert [n["text"] for n in data["notes"]] == ["Case priority is decided in CaseEscalationService only."]
    assert data["components"][SERVICE_SPEC]["last_check"]["status"] == "unchanged"


def test_many_writers_at_once_lose_nothing(project):
    import threading

    stores = [OrgKnowledge(project.root) for _ in range(6)]
    errors = []

    def write(store, number):
        try:
            for i in range(5):
                store.add_note(PROD_ID, "prod", f"Fact number {number}-{i} about how this org is built.")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(store, n)) for n, store in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    notes = OrgKnowledge(project.root).load(PROD_ID)["notes"]
    assert len(notes) == 30 and len({n["text"] for n in notes}) == 30
    assert not list((project.root / "knowledge").glob("*.lock")) and not list((project.root / "knowledge").glob("*.tmp"))


def test_a_lock_left_by_a_crashed_program_is_taken_over_and_a_held_one_is_reported(project, monkeypatch):
    import os
    import time

    import core.knowledge as knowledge_module
    from core.errors import WorkspaceError

    store = OrgKnowledge(project.root)
    store.add_note(PROD_ID, "prod", "A first fact, so that the folder exists.")
    lock = project.root / "knowledge" / f"{PROD_ID[:15]}.lock"
    lock.write_text("12345", encoding="utf-8")

    monkeypatch.setattr(knowledge_module, "LOCK_WAIT_SECONDS", 0.3)
    with pytest.raises(WorkspaceError) as busy:          # someone holds it right now
        store.add_note(PROD_ID, "prod", "A second fact that has to wait its turn.")
    assert "being updated by another program" in busy.value.message and ".lock" in busy.value.hint
    assert len(store.load(PROD_ID)["notes"]) == 1 and lock.exists()

    old = time.time() - 3600                             # ... or it was left behind an hour ago
    os.utime(lock, (old, old))
    store.add_note(PROD_ID, "prod", "A second fact that has to wait its turn.")
    assert len(store.load(PROD_ID)["notes"]) == 2 and not lock.exists()


def test_a_busy_memory_never_fails_a_read_tool(inv, project, monkeypatch):
    """Learning is a side effect: a tool that reads from the org still answers when the memory cannot be written."""
    import core.knowledge as knowledge_module

    learn(inv)
    monkeypatch.setattr(knowledge_module, "LOCK_WAIT_SECONDS", 0.2)
    (project.root / "knowledge" / f"{PROD_ID[:15]}.lock").write_text("1", encoding="utf-8")
    out = inv.describe_object("prod", "Case", include=["automation"])
    assert out["automation"]["triggers"]
    again = inv.retrieve_components("prod", "T-9", [SERVICE_SPEC])
    assert again["files"] and "org_memory" in again


# --- earlier tickets ------------------------------------------------------------------------


def write_report(inv, key, status, cause):
    folder = inv.workspace.ticket_dir(key)
    (folder / "report.md").write_text(
        f"# Investigation report: {key}\n\n| | |\n|---|---|\n| Ticket | {key} |\n| Root cause status | {status} |\n\n"
        f"## Ticket summary\n\nSomething was wrong.\n\n## Root cause\n\n{cause}\n\nMore detail follows here.\n\n"
        "## Fix\n\nNone.\n", encoding="utf-8")


def test_recall_brings_back_what_earlier_tickets_on_these_components_concluded(inv):
    learn(inv, key="T-1")
    write_report(inv, "T-1", "Confirmed", "**Confirmed.** The service leaves the loop early (E1),\nso later cases are skipped.")
    inv.retrieve_components("prod", "T-2", [SERVICE_SPEC])
    inv.remember_org_fact("prod", "Case priority is decided in CaseEscalationService only.", ticket_key="T-2",
                          components=[SERVICE_SPEC])
    inv.init_ticket("T-3")

    out = inv.recall_org_knowledge("prod", "case priority escalation", ticket_key="T-3")
    earlier = {entry["ticket"]: entry for entry in out["earlier_tickets"]}
    assert list(earlier) == ["T-1", "T-2"]                       # the one sharing most comes first
    assert earlier["T-1"]["root_cause_status"] == "Confirmed" and earlier["T-1"]["report"] == "tickets/T-1/report.md"
    assert earlier["T-1"]["root_cause"] == "**Confirmed.** The service leaves the loop early (E1), so later cases are skipped."
    assert SERVICE_SPEC in earlier["T-1"]["shares"] and earlier["T-1"]["title"] == "Investigation report: T-1"
    assert earlier["T-2"]["note"] == "Its report is no longer on this computer." and "root_cause" not in earlier["T-2"]
    assert "a similar symptom can have a different cause" in out["how_to_use"]

    # The ticket being worked on is not its own precedent.
    mine = inv.recall_org_knowledge("prod", "case priority escalation", ticket_key="T-1")
    assert [entry["ticket"] for entry in mine["earlier_tickets"]] == ["T-2"]
    assert inv.recall_org_knowledge("prod", "zebra")["earlier_tickets"] == []


def test_report_digest_is_short_and_survives_odd_reports(tmp_path):
    from core.report import report_digest

    assert report_digest(tmp_path / "missing.md") is None
    plain = tmp_path / "plain.md"
    plain.write_text("no headings at all", encoding="utf-8")
    assert report_digest(plain) == {"title": None, "root_cause_status": None, "root_cause": ""}
    long = tmp_path / "long.md"
    long.write_text("# R\n\n## Root cause\n\n| a | b |\n|---|---|\n\n" + "word " * 400 + "\n\n## Fix\n", encoding="utf-8")
    digest = report_digest(long)
    assert digest["title"] == "R" and len(digest["root_cause"]) == 501 and digest["root_cause"].endswith("…")
