from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.apex_scan import scan_apex_source
from core.errors import InputValidationError, SfCliError
from core.flow_summary import summarize_flow_xml

from .conftest import SERVICE

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "planted-bug" / "force-app" / "main" / "default"
FLOW = "metadata/force-app/main/default/flows/Case_Premium_Notification.flow-meta.xml"


# --- flow summary -----------------------------------------------------------------------


def test_flow_summary(inv):
    inv.retrieve_components("prod", "T-1", ["Flow:Case_Premium_Notification"])
    out = inv.summarize_flow("T-1", FLOW)
    assert out["path"] == FLOW and out["status"] == "Active" and out["label"] == "Case Premium Notification"
    assert out["start"]["object"] == "Case"
    assert out["start"]["trigger_type"] == "RecordAfterSave" and out["start"]["record_trigger_type"] == "Create"
    assert out["start"]["first_element"] == "Is_High_Priority"
    assert out["execution_order"] == ["Is_High_Priority", "Notify_Support_Lead"]
    decision = out["elements"][0]
    assert (decision["kind"], decision["line"]) == ("decision", 25)
    outcome = decision["outcomes"][0]
    assert outcome["conditions"] == ["$Record.Priority EqualTo 'High'"]
    assert (outcome["name"], outcome["next"], outcome["line"]) == ("High_Priority", "Notify_Support_Lead", 31)
    action = out["elements"][1]
    assert action["kind"] == "action" and "targetId = '{!$Record.Id}'" in action["inputs"]
    # The flow reads Priority and writes nothing: no record create/update/delete elements.
    assert not [e for e in out["elements"] if e["kind"] in ("update_records", "create_records", "delete_records")]
    assert out["notes"] == []


def test_flow_summary_notes_and_errors(inv, project):
    xml = (SAMPLE / "flows" / "Case_Premium_Notification.flow-meta.xml").read_text()
    draft = summarize_flow_xml(xml.replace("<status>Active</status>", "<status>Draft</status>"))
    assert "does not run unless it is Active" in draft["notes"][0]
    with_update = summarize_flow_xml(xml.replace(
        "<status>Active</status>",
        "<recordUpdates><name>Set_Priority</name><inputAssignments><field>Priority</field>"
        "<value><stringValue>Low</stringValue></value></inputAssignments>"
        "<inputReference>$Record</inputReference></recordUpdates><status>Active</status>"))
    assert with_update["unreachable_elements"] == ["Set_Priority"]
    assert any("without a fault path" in n for n in with_update["notes"])
    update = next(e for e in with_update["elements"] if e["name"] == "Set_Priority")
    assert update["field_values"] == ["Priority = 'Low'"]
    for bad in ("<Flow", "<Other xmlns='http://soap.sforce.com/2006/04/metadata'/>"):
        with pytest.raises(InputValidationError):
            summarize_flow_xml(bad)
    inv.retrieve_components("prod", "T-1", ["ApexClass:CaseTriggerHandler"])
    with pytest.raises(InputValidationError):
        inv.summarize_flow("T-1", "metadata/force-app/main/default/classes/CaseTriggerHandler.cls")


# --- Apex scan --------------------------------------------------------------------------


def test_scan_flags_the_planted_bug_at_line_30(inv):
    inv.retrieve_components("prod", "T-1", ["ApexClass:*", "ApexTrigger:*"])
    out = inv.scan_apex("T-1")
    assert out["files_scanned"] == 4
    assert [(l["rule"], l["path"], l["line"], l["code"]) for l in out["leads"]] == [
        ("return_inside_loop", SERVICE, 30, "return;")]
    assert "not findings" in out["how_to_use"]
    assert inv.scan_apex("T-1", glob="*Handler*")["lead_count"] == 0
    inv.init_ticket("EMPTY-1")
    assert "No Apex" in inv.scan_apex("EMPTY-1")["note"]


def test_scan_does_not_flag_the_fixed_version():
    source = (SAMPLE / "classes" / "CaseEscalationService.cls").read_text().replace(
        "                return;", "                continue;")
    assert scan_apex_source(source, "x.cls") == []


def rules(source, path="X.cls"):
    return [(l["rule"], l["line"]) for l in scan_apex_source(source, path)]


def test_soql_for_loop_idiom_is_not_a_false_positive():
    source = """public class X {
    public static void run() {
        for (Account a : [SELECT Id FROM Account]) {
            a.Name = 'x';
        }
        for (Account a : [
            SELECT Id FROM Account WHERE Name != null
        ]) {
            System.debug(a);
        }
    }
}"""
    assert rules(source) == []


def test_real_soql_and_dml_in_loops_are_flagged():
    source = """public class X {
    public static void run(List<Account> accounts) {
        for (Account a : accounts) {
            List<Contact> cs = [SELECT Id FROM Contact WHERE AccountId = :a.Id];
            update a;
            Database.insert(cs);
        }
        while (true) {
            Database.query('SELECT Id FROM Case');
            return;
        }
        update accounts;
        return;
    }
}"""
    assert rules(source) == [("soql_in_loop", 4), ("dml_in_loop", 5), ("dml_in_loop", 6), ("soql_in_loop", 9),
                             ("return_inside_loop", 10)]


def test_nested_soql_for_loop_inside_another_loop_is_flagged():
    source = """public class X {
    void run(List<Account> accounts) {
        for (Account a : accounts) {
            for (Contact c : [SELECT Id FROM Contact WHERE AccountId = :a.Id]) {
                System.debug(c);
            }
        }
    }
}"""
    assert rules(source) == [("soql_in_loop", 4)]


def test_other_rules_and_comment_handling():
    source = """trigger T on Case (before insert) {
    Case first = Trigger.new[0];
    // for (Case c : Trigger.new) { return; }
    /* update Trigger.new[0];
       return; */
    String s = 'for (x) { return; }';
    Id queue = '00G5e000001AbCdEAK';
    Account a = [SELECT Id FROM Account LIMIT 1];
    Id b = [SELECT Id FROM Account][0].Id;
    try {
        update first;
    } catch (Exception e) {
        System.debug(e);
    }
    try { update first; } catch (DmlException e) { throw e; }
}"""
    assert rules(source, "T.trigger") == [
        ("first_record_only", 2), ("hardcoded_id", 7), ("single_row_query", 8),
        ("query_indexed_without_check", 9), ("swallowed_exception", 12)]


def test_test_classes_only_report_hardcoded_ids():
    source = """@isTest
private class XTest {
    @isTest static void t() {
        for (Integer i = 0; i < 3; i++) { insert new Account(Name = 'a'); }
        Id x = '0015e00000AbCdEAAV';
    }
}"""
    assert rules(source) == [("hardcoded_id", 5)]


# --- timeline ---------------------------------------------------------------------------


def test_recent_org_changes(inv, sim):
    out = inv.recent_org_changes("prod", "2026-11-05")
    assert out["since"] == "2026-11-05T00:00:00Z"
    trail = out["setup_audit_trail"]
    assert [(t["when"][:10], t["section"]) for t in trail] == [
        ("2026-11-09", "Apex Class"), ("2026-11-09", "Connected Apps")]
    assert trail[0]["display"] == "Changed CaseEscalationService Apex Class code"
    assert trail[0]["by_user_id"].startswith("005")
    assert [c["Name"] for c in out["apex_classes"]] == ["CaseEscalationService"]
    assert out["apex_classes"][0]["LastModifiedDate"].startswith("2026-11-09T16:42:07")
    assert out["apex_triggers"] == [] and out["flow_versions"] == [] and out["validation_rules"] == []
    assert out["counts"]["setup_audit_trail"] == 2
    text = json.dumps(out)
    assert not any(u["Name"] in text or u["Username"] in text for u in sim.records("prod")["User"])


def test_recent_org_changes_window_filter_and_errors(inv, sim):
    wide = inv.recent_org_changes("prod", "2026-10-01", until="2026-10-31")
    assert [t["section"] for t in wide["setup_audit_trail"]] == ["Flows"]
    assert [f["Definition"]["DeveloperName"] for f in wide["flow_versions"]] == ["Case_Premium_Notification"]
    filtered = inv.recent_org_changes("prod", "2026-10-01", contains="caseescalation")
    assert len(filtered["setup_audit_trail"]) == 1 and len(filtered["apex_classes"]) == 1
    empty = inv.recent_org_changes("prod", "2026-11-13")
    assert "No changes recorded" in empty["note"]
    with pytest.raises(InputValidationError):
        inv.recent_org_changes("prod", "last week")
    # One failing source is reported in place and the others still come back.
    records = sim.records("prod")
    del records["SetupAuditTrail"]
    (sim.org_dir("prod") / "records.json").write_text(json.dumps(records))
    partial = inv.recent_org_changes("prod", "2026-11-05")
    assert "error" in partial["setup_audit_trail"] and len(partial["apex_classes"]) == 1


def test_setup_audit_trail_emails_are_masked(inv, sim):
    records = sim.records("prod")
    records["SetupAuditTrail"].append({
        "Id": "0YmWs00000Aud09IAB", "Action": "changedemail", "Section": "Manage Users",
        "Display": "Changed email for user from old.person@customer.test to new.person@customer.test",
        "CreatedDate": "2026-11-10T10:00:00.000+0000", "CreatedById": "005Ws000001AdmnIAB", "DelegateUser": None})
    (sim.org_dir("prod") / "records.json").write_text(json.dumps(records))
    out = inv.recent_org_changes("prod", "2026-11-10", contains="Manage Users")
    assert out["setup_audit_trail"][0]["display"] == "Changed email for user from [email] to [email]"


def test_record_history(inv, sim):
    empty = inv.record_history("prod", "Case", "500Ws00000C0003IAA")
    assert empty["count"] == 0 and empty["history_object"] == "CaseHistory"
    assert "not evidence that nothing changed" in empty["note"]
    records = sim.records("prod")
    records["CaseHistory"] = [
        {"Id": "017Ws0000000001IAA", "CaseId": "500Ws00000C0003IAA", "Field": "Priority", "OldValue": "Medium",
         "NewValue": "High", "CreatedById": "005Ws000001AgntIAB", "CreatedDate": "2026-11-12T08:01:00.000+0000"},
        {"Id": "017Ws0000000002IAA", "CaseId": "500Ws00000C0003IAA", "Field": "SuppliedEmail",
         "OldValue": "a@customer.test", "NewValue": "b@customer.test", "CreatedById": "005Ws000001AgntIAB",
         "CreatedDate": "2026-11-12T09:00:00.000+0000"},
        {"Id": "017Ws0000000003IAA", "CaseId": "500Ws00000C0004IAA", "Field": "Status", "OldValue": "New",
         "NewValue": "Closed", "CreatedById": "005Ws000001AgntIAB", "CreatedDate": "2026-11-12T09:30:00.000+0000"},
    ]
    (sim.org_dir("prod") / "records.json").write_text(json.dumps(records))
    out = inv.record_history("prod", "Case", "500Ws00000C0003IAA")
    assert [c["field"] for c in out["changes"]] == ["SuppliedEmail", "Priority"]
    assert out["changes"][0]["old_value"] == "[REDACTED]" and out["changes"][0]["new_value"] == "[REDACTED]"
    assert out["changes"][1] == {"when": "2026-11-12T08:01:00.000+0000", "field": "Priority", "old_value": "Medium",
                                 "new_value": "High", "by_user_id": "005Ws000001AgntIAB"}
    assert out["redacted_fields"] == ["SuppliedEmail"] and "customer.test" not in json.dumps(out)


def test_record_history_object_names_and_validation(inv, recording_runner, project):
    from core.app import Investigator
    from core.sf_cli import SfCli

    seen: list[str] = []

    def runner(argv, cwd, timeout, env):
        seen.append(Path(cwd, "query.soql").read_text())
        from core.sf_cli import ProcessResult
        return ProcessResult(0, '{"status":0,"result":{"records":[],"totalSize":0}}', "")

    fake = Investigator(root=project.root, cli=SfCli(executable="sf-test", runner=runner))
    fake.record_history("x", "Invoice__c", "a01Ws0000000001IAA")
    fake.record_history("x", "Opportunity", "006Ws0000000001IAA")
    fake.record_history("x", "Account", "001Ws0000000001IAA")
    assert "FROM Invoice__History WHERE ParentId = 'a01Ws0000000001IAA'" in seen[0]
    assert "FROM OpportunityFieldHistory WHERE OpportunityId =" in seen[1]
    assert "FROM AccountHistory WHERE AccountId =" in seen[2]
    for sobject, record_id in (("Case", "nope"), ("Case; x", "500Ws00000C0003IAA"), ("Case", "500' OR Id != '")):
        with pytest.raises(InputValidationError):
            inv.record_history("prod", sobject, record_id)


# --- dependencies -----------------------------------------------------------------------


def test_find_dependencies_both_directions(inv):
    out = inv.find_dependencies("prod", "CaseEscalationService")
    assert [(d["type"], d["name"]) for d in out["used_by"]] == [
        ("ApexClass", "CaseEscalationServiceTest"), ("ApexClass", "CaseTriggerHandler")]
    assert [(d["type"], d["name"]) for d in out["uses"]] == [("CustomField", "Support_Tier")]
    assert "none recorded" in out["coverage_note"]


def test_find_dependencies_direction_type_and_validation(inv):
    used_by = inv.find_dependencies("prod", "CaseTriggerHandler", direction="used_by", component_type="ApexClass")
    assert [d["name"] for d in used_by["used_by"]] == ["CaseTrigger"] and "uses" not in used_by
    uses = inv.find_dependencies("prod", "CaseTriggerHandler", direction="uses")
    assert [d["name"] for d in uses["uses"]] == ["CaseEscalationService"] and "used_by" not in uses
    nothing = inv.find_dependencies("prod", "Unknown_Component")
    assert nothing["used_by"] == [] and nothing["uses"] == [] and "coverage_note" in nothing
    for kwargs in ({"name": "x'; DROP"}, {"name": "ok", "direction": "sideways"}, {"name": "ok", "component_type": "a b"}):
        name = kwargs.pop("name")
        with pytest.raises(InputValidationError):
            inv.find_dependencies("prod", name, **kwargs)


def test_dependency_query_has_no_order_by(project):
    """MetadataComponentDependency has restricted query support: sort locally, never in SOQL."""
    from core.app import Investigator
    from core.sf_cli import ProcessResult, SfCli

    seen: list[str] = []

    def runner(argv, cwd, timeout, env):
        seen.append(Path(cwd, "query.soql").read_text())
        rows = [{"MetadataComponentName": n, "MetadataComponentType": t, "MetadataComponentId": "01p"}
                for t, n in (("Flow", "B"), ("ApexClass", "Z"), ("ApexClass", "A"))]
        return ProcessResult(0, json.dumps({"status": 0, "result": {"records": rows, "totalSize": 3}}), "")

    fake = Investigator(root=project.root, cli=SfCli(executable="sf-test", runner=runner))
    out = fake.find_dependencies("x", "Thing", direction="used_by")
    assert all("ORDER BY" not in q.upper() for q in seen)
    assert [(d["type"], d["name"]) for d in out["used_by"]] == [("ApexClass", "A"), ("ApexClass", "Z"), ("Flow", "B")]
