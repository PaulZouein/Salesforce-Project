from __future__ import annotations

import json

import pytest

from core.errors import InputValidationError, QueryValidationError, RowLimitError, SfCliError
from core.query import REDACTED, Redactor, enforce_limit

from .conftest import SIMULATED_TOKEN


def limit(query, requested=None, default=50, maximum=200):
    return enforce_limit(query, default_limit=default, max_limit=maximum, requested_limit=requested)


# --- limits -----------------------------------------------------------------------------


def test_limit_added_when_missing():
    assert limit("SELECT Id FROM Case") == ("SELECT Id FROM Case LIMIT 50", 50)
    assert limit("select Id from Case where Subject = 'no LIMIT 9999 here';") == \
        ("select Id from Case where Subject = 'no LIMIT 9999 here' LIMIT 50", 50)


def test_limit_kept_when_within_the_maximum():
    assert limit("SELECT Id FROM Case LIMIT 7") == ("SELECT Id FROM Case LIMIT 7", 7)
    assert limit("SELECT Id FROM Case limit 200") == ("SELECT Id FROM Case limit 200", 200)


def test_limit_lowered_by_the_request_never_raised():
    assert limit("SELECT Id FROM Case LIMIT 100", requested=10) == ("SELECT Id FROM Case LIMIT 10", 10)
    assert limit("SELECT Id FROM Case LIMIT 5", requested=10) == ("SELECT Id FROM Case LIMIT 5", 5)
    assert limit("SELECT Id FROM Case", requested=10) == ("SELECT Id FROM Case LIMIT 10", 10)


def test_default_is_capped_by_the_maximum():
    assert limit("SELECT Id FROM Case", default=500, maximum=20)[1] == 20


@pytest.mark.parametrize("query,requested", [
    ("SELECT Id FROM Case LIMIT 201", None),
    ("SELECT Id FROM Case LIMIT 50000", None),
    ("SELECT Id FROM Case", 201),
    ("SELECT Id FROM Case LIMIT 10", 5000),
])
def test_limit_above_the_maximum_is_rejected(query, requested):
    with pytest.raises(RowLimitError) as err:
        limit(query, requested)
    assert "200" in err.value.message


@pytest.mark.parametrize("requested", [0, -1, True, "10", 2.5])
def test_bad_requested_limit(requested):
    with pytest.raises(RowLimitError):
        limit("SELECT Id FROM Case", requested)


def test_subquery_limit_is_checked_and_top_level_limit_still_added():
    with pytest.raises(RowLimitError):
        limit("SELECT Id, (SELECT Id FROM Cases LIMIT 5000) FROM Account LIMIT 10")
    text, applied = limit("SELECT Id, (SELECT Id FROM Cases LIMIT 5) FROM Account")
    assert text.endswith("FROM Account LIMIT 50") and applied == 50
    text, applied = limit("SELECT Id FROM Account WHERE Id IN (SELECT AccountId FROM Case LIMIT 3)")
    assert text.endswith(") LIMIT 50")


def test_limit_goes_before_offset_and_for_view():
    assert limit("SELECT Id FROM Case ORDER BY Id OFFSET 20")[0] == "SELECT Id FROM Case ORDER BY Id LIMIT 50 OFFSET 20"
    assert limit("SELECT Id FROM Case FOR VIEW")[0] == "SELECT Id FROM Case LIMIT 50 FOR VIEW"
    assert limit("SELECT Id FROM Case LIMIT 5 OFFSET 20")[0] == "SELECT Id FROM Case LIMIT 5 OFFSET 20"
    assert limit("SELECT Id FROM Case ALL ROWS")[0] == "SELECT Id FROM Case LIMIT 50 ALL ROWS"


@pytest.mark.parametrize("query", [
    "",
    "   ",
    "UPDATE Case SET Priority = 'High'",
    "DELETE FROM Case",
    "INSERT INTO Case (Subject) VALUES ('x')",
    "FIND {x} IN ALL FIELDS",
    "  (SELECT Id FROM Case)",
    "SELECTED",
    None,
    42,
])
def test_non_select_is_rejected(query):
    with pytest.raises(QueryValidationError):
        limit(query)


@pytest.mark.parametrize("query", [
    "SELECT Id FROM Case; DELETE FROM Case",
    "SELECT Id FROM Case; SELECT Id FROM Account",
    "SELECT Id FROM Case;;",
])
def test_multiple_statements_rejected(query):
    with pytest.raises(QueryValidationError):
        limit(query)


def test_semicolon_inside_a_string_is_fine():
    assert limit("SELECT Id FROM Case WHERE Subject = 'a;b'")[1] == 50


@pytest.mark.parametrize("query", [
    "SELECT Id FROM Case FOR UPDATE",
    "SELECT Id FROM Case LIMIT 1 for   update",
])
def test_for_update_rejected(query):
    with pytest.raises(QueryValidationError) as err:
        limit(query)
    assert "locks" in err.value.message


@pytest.mark.parametrize("query", [
    "SELECT Id FROM Case WHERE Subject = 'unterminated",
    "SELECT Id FROM Case WHERE (Subject = 'x'",
    "SELECT Id FROM Case WHERE Subject = 'x')",
    "SELECT Id FROM Case LIMIT 5 LIMIT 6",
])
def test_malformed_queries_rejected(query):
    with pytest.raises(QueryValidationError):
        limit(query)


# --- redaction --------------------------------------------------------------------------

CASE = {
    "attributes": {"type": "Case"},
    "Id": "500X", "Subject": "Printer", "SuppliedEmail": "person@customer.test", "SuppliedPhone": "+33 1 23 45 67 89",
    "Priority": "High", "Notes__c": "free text", "Empty_Email__c": None,
    "Owner": {"attributes": {"type": "User"}, "Name": "Jane Doe", "Username": "jane@corp.test", "Id": "005X"},
    "Account": {"attributes": {"type": "Account"}, "Name": "Acme", "BillingStreet": "1 Main St",
                "BillingAddress": {"street": "1 Main St", "city": "Paris"}},
    "Contacts": {"totalSize": 1, "records": [
        {"attributes": {"type": "Contact"}, "Name": "John Roe", "Birthdate": "1990-01-01", "Title": "CTO"}]},
}


def test_default_redaction():
    redactor = Redactor()
    out = redactor.record(CASE)
    assert "attributes" not in out
    assert out["SuppliedEmail"] == REDACTED and out["SuppliedPhone"] == REDACTED
    assert out["Subject"] == "Printer" and out["Priority"] == "High" and out["Notes__c"] == "free text"
    assert out["Empty_Email__c"] is None
    assert redactor.redacted >= {"SuppliedEmail", "SuppliedPhone"}


def test_nested_relationship_redaction():
    redactor = Redactor()
    out = redactor.record(CASE)
    assert out["Owner"] == {"Name": REDACTED, "Username": REDACTED, "Id": "005X"}
    assert out["Account"]["Name"] == "Acme"
    assert out["Account"]["BillingStreet"] == REDACTED
    assert out["Account"]["BillingAddress"] == REDACTED
    assert out["Contacts"]["records"][0] == {"Name": REDACTED, "Birthdate": REDACTED, "Title": "CTO"}
    assert {"Owner.Name", "Owner.Username", "Account.BillingStreet", "Contacts.Name"} <= redactor.redacted
    assert "Jane" not in json.dumps(out) and "John" not in json.dumps(out)


def test_extra_and_reveal():
    out = Redactor(extra_fields=["Notes__c", "Account.Name"], reveal_fields=["SuppliedEmail", "owner.name"]).record(CASE)
    assert out["Notes__c"] == REDACTED
    assert out["Account"]["Name"] == REDACTED
    assert out["SuppliedEmail"] == "person@customer.test"
    assert out["Owner"]["Name"] == "Jane Doe"
    assert out["SuppliedPhone"] == REDACTED


def test_list_of_records():
    out = Redactor().record([CASE, CASE])
    assert [r["SuppliedEmail"] for r in out] == [REDACTED, REDACTED]


# --- running against the simulator ------------------------------------------------------


def test_query_adds_limit_and_redacts(inv):
    out = inv.soql_query("prod", "SELECT Id, CaseNumber, Priority, SuppliedEmail, Owner.Name FROM Case "
                                 "WHERE Origin = 'Phone'")
    assert out["applied_limit"] == 50 and out["query"].endswith("LIMIT 50")
    assert out["returned"] == 1
    row = out["records"][0]
    assert row["SuppliedEmail"] == REDACTED
    assert "SuppliedEmail" in out["redacted_fields"]
    assert "saved_to" not in out
    assert SIMULATED_TOKEN not in json.dumps(out)


def test_relationship_name_of_a_user_is_redacted(inv, sim):
    out = inv.soql_query("prod", "SELECT Id, CreatedBy.Name, CreatedBy.Username, Account.Name FROM Case LIMIT 3")
    names = {u["Name"] for u in sim.records("prod")["User"]}
    text = json.dumps(out)
    assert not any(name in text for name in names)
    assert out["records"][0]["CreatedBy"]["Name"] == REDACTED
    assert out["records"][0]["Account"]["Name"].startswith("Demo")
    assert "CreatedBy.Name" in out["redacted_fields"]


def test_saved_files_stay_redacted_even_with_reveal(inv, sim, project):
    emails = {c["SuppliedEmail"] for c in sim.records("prod")["Case"] if c.get("SuppliedEmail")}
    assert emails, "the sample data must contain personal data for this test to mean anything"
    out = inv.soql_query("prod", "SELECT Id, SuppliedEmail FROM Case WHERE SuppliedEmail != null",
                         ticket_key="T-1", reveal_fields=["SuppliedEmail"], save_as="revealed emails!")
    shown = {r["SuppliedEmail"] for r in out["records"]}
    assert shown == emails
    assert out["saved_to"].startswith("queries/") and out["saved_to"].endswith("-revealed-emails.json")
    saved = (project.ticket("T-1") / out["saved_to"]).read_text(encoding="utf-8")
    assert not any(email in saved for email in emails)
    data = json.loads(saved)
    assert all(r["SuppliedEmail"] == REDACTED for r in data["records"])
    assert data["org"] == "prod" and data["redacted_fields"] == ["SuppliedEmail"]


def test_limit_reached_note_and_requested_limit(inv):
    out = inv.soql_query("prod", "SELECT Id FROM Case", limit=3)
    assert out["returned"] == 3 and "more rows may exist" in out["note"]


def test_limit_above_configured_maximum(inv, project):
    project.write_config(max_row_limit=5, default_row_limit=2)
    assert inv.soql_query("prod", "SELECT Id FROM Case")["returned"] == 2
    with pytest.raises(RowLimitError):
        inv.soql_query("prod", "SELECT Id FROM Case LIMIT 6")


def test_non_select_never_reaches_the_cli(project, recording_runner):
    from core.app import Investigator
    from core.sf_cli import SfCli
    runner = recording_runner()
    inv = Investigator(root=project.root, cli=SfCli(executable="sf-test", runner=runner))
    with pytest.raises(QueryValidationError):
        inv.soql_query("prod", "DELETE FROM Case")
    with pytest.raises(InputValidationError):
        inv.soql_query("bad alias!", "SELECT Id FROM Case")
    assert runner.calls == []


def test_tooling_query(inv):
    out = inv.soql_query("prod", "SELECT Id, Name FROM ApexClass WHERE Name = 'CaseEscalationService'", tooling=True)
    assert out["api"] == "tooling" and out["returned"] == 1


def test_query_error_is_typed_with_a_hint(inv):
    with pytest.raises(SfCliError) as err:
        inv.soql_query("prod", "SELECT Id FROM NoSuchObject__c")
    assert "describe_object" in err.value.hint


def test_count_query(inv):
    out = inv.soql_query("prod", "SELECT COUNT() FROM Case WHERE Priority = 'Medium'")
    assert out["total_size"] == 5 and out["records"] == []


# --- describe ---------------------------------------------------------------------------


def test_describe_fields_and_filters(inv):
    out = inv.describe_object("prod", "Case")
    assert out["name"] == "Case" and out["field_count"] == 11
    assert "automation" not in out
    priority = next(f for f in out["fields"] if f["name"] == "Priority")
    assert "High" in priority["picklist_values"]
    out = inv.describe_object("prod", "Case", fields=["Priority", "Nope__c"], include=["fields"])
    assert [f["name"] for f in out["fields"]] == ["Priority"] and out["fields_not_found"] == ["nope__c"]
    out = inv.describe_object("prod", "Case", field_filter="email", include=["fields"])
    assert [f["name"] for f in out["fields"]] == ["SuppliedEmail"]


def test_describe_automation(inv):
    auto = inv.describe_object("prod", "Case", include=["automation"])["automation"]
    assert [t["Name"] for t in auto["triggers"]] == ["CaseTrigger"]
    assert auto["triggers"][0]["UsageBeforeInsert"] is True
    assert [f["ApiName"] for f in auto["record_triggered_flows"]] == ["Case_Premium_Notification"]
    assert auto["validation_rules"] == [] and auto["workflow_rules"] == []
    assert "retrieve_components" in auto["how_to_read_the_source"]


def test_automation_reports_a_failing_section_in_place(inv, sim):
    records = sim.records("prod")
    del records["FlowDefinitionView"]
    (sim.org_dir("prod") / "records.json").write_text(json.dumps(records))
    auto = inv.describe_object("prod", "Case", include=["automation"])["automation"]
    assert "error" in auto["record_triggered_flows"]
    assert [t["Name"] for t in auto["triggers"]] == ["CaseTrigger"]


def test_describe_validation(inv):
    with pytest.raises(InputValidationError):
        inv.describe_object("prod", "Case; DROP")
    with pytest.raises(InputValidationError):
        inv.describe_object("prod", "Case", include=["everything"])
    with pytest.raises(SfCliError):
        inv.describe_object("prod", "Nope__c")
