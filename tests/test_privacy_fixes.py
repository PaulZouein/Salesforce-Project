"""Personal data that used to get past the redaction, found by three independent reviews.

The CLI answers below are in the shapes Salesforce really returns (the simulator labels related records with
their real object type, so these cases never came up against it).
"""

from __future__ import annotations

import json

import pytest

from core.app import Investigator
from core.log_analysis import analyze_log
from core.logs import TRUNCATION_MARKER, _soql_datetime
from core.query import REDACTED
from core.report import _personal_data
from core.sf_cli import ProcessResult, SfCli

KEY = "PRIV-1"


class Canned:
    """Answers each command from a list of (match, result) pairs; records the queries it was asked."""

    def __init__(self, *answers):
        self.answers, self.queries = list(answers), []

    def __call__(self, argv, cwd, timeout, env, **kw):
        if "query" in argv and cwd:
            from pathlib import Path
            self.queries.append((Path(cwd) / "query.soql").read_text(encoding="utf-8"))
        for match, result in self.answers:
            if match in " ".join(argv) + " " + (self.queries[-1] if self.queries else ""):
                if isinstance(result, BaseException):
                    raise result
                return ProcessResult(0, json.dumps({"status": 0, "result": result}), "")
        return ProcessResult(0, json.dumps({"status": 0, "result": {"records": [], "totalSize": 0}}), "")


def investigator(project, *answers):
    return Investigator(root=project.root, cli=SfCli(executable="sf-test", runner=Canned(*answers)))


def saved(project, out):
    return (project.ticket(KEY) / out["saved_to"]).read_text(encoding="utf-8")


# --- queries ------------------------------------------------------------------------------------


def test_the_owner_behind_a_polymorphic_lookup_is_redacted(project):
    """Case.Owner can be a user or a queue: Salesforce types the related record "Name", not "User"."""
    inv = investigator(project, ("FROM Case", {"totalSize": 1, "records": [{
        "attributes": {"type": "Case"}, "Id": "500Ws00000C0003IAA",
        "Owner": {"attributes": {"type": "Name"}, "Name": "Jane Doe"}}]}))
    inv.init_ticket(KEY)
    out = inv.soql_query("prod", "SELECT Id, Owner.Name FROM Case", ticket_key=KEY, save_as="owners")
    assert out["records"][0]["Owner"]["Name"] == REDACTED
    assert "Jane Doe" not in json.dumps(out) and "Jane Doe" not in saved(project, out)


def test_a_grouped_owner_name_is_redacted_and_a_grouped_status_is_not(project):
    inv = investigator(project,
                       ("GROUP BY Owner.Name", {"totalSize": 1, "records": [
                           {"attributes": {"type": "AggregateResult"}, "Name": "Jane Doe", "expr0": 7}]}),
                       ("GROUP BY Status", {"totalSize": 1, "records": [
                           {"attributes": {"type": "AggregateResult"}, "Status": "New", "expr0": 7}]}))
    inv.init_ticket(KEY)
    owners = inv.soql_query("prod", "SELECT Owner.Name, COUNT(Id) FROM Case GROUP BY Owner.Name",
                            ticket_key=KEY, save_as="by-owner")
    assert owners["records"] == [{"Name": REDACTED, "expr0": 7}] and "Jane Doe" not in saved(project, owners)
    statuses = inv.soql_query("prod", "SELECT Status, COUNT(Id) FROM Case GROUP BY Status")
    assert statuses["records"] == [{"Status": "New", "expr0": 7}]


def test_an_aliased_aggregate_of_a_personal_field_is_redacted(project):
    inv = investigator(project, ("FROM Contact", {"totalSize": 1, "records": [
        {"attributes": {"type": "AggregateResult"}, "lastMail": "jane.doe@customer.test"}]}))
    out = inv.soql_query("prod", "SELECT MAX(Email) lastMail FROM Contact")
    assert out["records"] == [{"lastMail": REDACTED}]


def test_a_person_account_name_is_redacted_and_a_company_name_is_not(project):
    inv = investigator(project, ("FROM Account", {"totalSize": 2, "records": [
        {"attributes": {"type": "Account"}, "Id": "001A", "Name": "John Smith", "IsPersonAccount": True},
        {"attributes": {"type": "Account"}, "Id": "001B", "Name": "Acme Corp", "IsPersonAccount": False}]}))
    out = inv.soql_query("prod", "SELECT Id, Name, IsPersonAccount FROM Account")
    assert [r["Name"] for r in out["records"]] == [REDACTED, "Acme Corp"]


# --- field history ------------------------------------------------------------------------------


def test_the_names_row_of_a_lookup_change_is_redacted_and_the_ids_row_kept(project):
    """A lookup change is recorded twice, with Ids and with display names (people's names for owners)."""
    when = "2026-11-10T02:00:14.000+0000"
    inv = investigator(project, ("FROM CaseHistory", {"totalSize": 4, "records": [
        {"Field": "Owner", "OldValue": "005Ws000001IntgIAB", "NewValue": "00GWs000000QueuIAB", "CreatedDate": when},
        {"Field": "Owner", "OldValue": "Jane Doe", "NewValue": "Support Queue", "CreatedDate": when},
        {"Field": "Owner", "OldValue": "John Smith", "NewValue": None, "CreatedDate": "2026-11-11T08:00:00.000+0000"},
        {"Field": "Priority", "OldValue": "Medium", "NewValue": "High", "CreatedDate": when}]}))
    inv.init_ticket(KEY)
    out = inv.record_history("prod", "Case", "500Ws00000C0003IAA", ticket_key=KEY)
    values = [(c["old_value"], c["new_value"]) for c in out["changes"]]
    assert values == [("005Ws000001IntgIAB", "00GWs000000QueuIAB"), (REDACTED, REDACTED), (REDACTED, None),
                      ("Medium", "High")]
    text = json.dumps(out) + saved(project, out)
    assert "Jane Doe" not in text and "John Smith" not in text


# --- debug logs ---------------------------------------------------------------------------------

LOG = "\n".join([
    "62.0 APEX_CODE,FINEST",
    "08:00:00.0 (1)|USER_DEBUG|[12]|DEBUG|running as integration@demo.example.com for jane.doe@customer.test",
    "08:00:00.0 (2)|LIMIT_USAGE_FOR_NS|(default)|",
    "  Number of SOQL queries: 99 out of 100",
    "08:00:00.0 (3)|LIMIT_USAGE_FOR_NS|SBQQ|",
    "  Number of SOQL queries: 3 out of 100",
])


def test_a_managed_package_section_does_not_hide_the_orgs_own_limits():
    out = analyze_log(LOG)
    assert out["limits"]["SOQL queries"] == {"used": 99, "limit": 100}
    assert out["limits_by_namespace"]["SBQQ"]["SOQL queries"]["used"] == 3
    assert "SOQL queries" in out["limits_at_or_above_80_percent"]


def test_text_from_a_log_has_its_email_addresses_masked():
    shown = json.dumps(analyze_log(LOG))
    assert "@customer.test" not in shown and "integration@" not in shown and "[email]" in shown


def test_a_truncated_log_is_flagged_and_absence_is_not_evidence():
    out = analyze_log(LOG + f"\n08:00:01.0 (9)|USER_DEBUG|[1]|DEBUG|*** {TRUNCATION_MARKER} ***")
    assert out["truncated"] is True and "do not conclude from absence" in out["warning"]
    assert "absent may still have run" in out["apex_statements"]["note"]


def test_the_log_text_returned_by_the_cli_is_saved_and_a_deleted_log_says_so(project):
    """With --output-dir, CLI 2.158.7 returns the log's text; the old fallback took it for a file name."""
    from core.errors import SfCliError, WorkspaceError

    inv = investigator(project, ("07LWs00000L0001MAA", [LOG]))
    inv.init_ticket(KEY)
    out = inv.get_debug_log("prod", "07LWs00000L0001MAA", KEY)
    assert out["total_lines"] == 6 and "jane.doe@customer.test" not in out["excerpt"]
    assert (project.ticket(KEY) / "logs" / "07LWs00000L0001MAA.log").read_text(encoding="utf-8") == LOG
    gone = investigator(project, ("07LWs00000L0002MAA", SfCliError("NOT_FOUND: The requested resource does not exist",
                                                                   name="NOT_FOUND")))
    with pytest.raises(WorkspaceError) as caught:
        gone.get_debug_log("prod", "07LWs00000L0002MAA", KEY)
    assert "no longer in the org" in caught.value.message


@pytest.mark.parametrize("given, sent", [
    ("2026-11-10T08:00", "2026-11-10T08:00:00Z"),
    ("2026-11-10T08:00:30Z", "2026-11-10T08:00:30Z"),
    ("2026-11-10T08:00+0100", "2026-11-10T08:00:00+01:00"),
    ("2026-11-10", "2026-11-10T00:00:00Z"),
])
def test_dates_are_sent_in_the_form_soql_accepts(given, sent):
    assert _soql_datetime(given) == sent


# --- the personal-data check of reports and comments --------------------------------------------


@pytest.mark.parametrize("line", [
    "Contact jane.doe@acme.com.invalid",      # how a sandbox refresh rewrites a real user's address
    "Mail a@acme-example.com",
    "From jane.noreply@acme.com",
    "Customer bo.kim@corp.test",
])
def test_real_looking_addresses_are_flagged(line):
    assert _personal_data([line])[0]


@pytest.mark.parametrize("line", ["admin@test.example.com", "x@example.org", "noreply@salesforce.com"])
def test_example_and_system_addresses_are_not(line):
    assert not _personal_data([line])[0]


@pytest.mark.parametrize("line, flagged", [
    ("Call (415) 555-0132", True), ("+1 (415) 555-0132", True), ("+44 20 7946 0958", True),
    ("case 00001003", False), ("Created 2026-11-12 09:40", False), ("see report.md:1200-1234", False),
])
def test_phone_numbers_in_the_usual_formats_are_flagged(line, flagged):
    assert bool(_personal_data([line])[1]) is flagged


def test_usernames_and_aliases_are_redacted_when_grouped_and_behind_an_owner(project):
    """Second review: GROUP BY Owner.Username returned the usernames; Owner is typed "Name", not "User"."""
    inv = investigator(project,
                       ("GROUP BY Owner.Username", {"totalSize": 1, "records": [
                           {"attributes": {"type": "AggregateResult"}, "Username": "jdoe@acme.com", "expr0": 3}]}),
                       ("GROUP BY Owner.Alias", {"totalSize": 1, "records": [
                           {"attributes": {"type": "AggregateResult"}, "Alias": "jdoe", "expr0": 3}]}),
                       ("SELECT Id, Owner.Username", {"totalSize": 1, "records": [
                           {"attributes": {"type": "Case"}, "Id": "500Ws00000C0003IAA",
                            "Owner": {"attributes": {"type": "Name"}, "Username": "jdoe@acme.com"}}]}))
    grouped = inv.soql_query("prod", "SELECT Owner.Username, COUNT(Id) FROM Case GROUP BY Owner.Username")
    assert grouped["records"] == [{"Username": REDACTED, "expr0": 3}]
    aliases = inv.soql_query("prod", "SELECT Owner.Alias, COUNT(Id) FROM Case GROUP BY Owner.Alias")
    assert aliases["records"] == [{"Alias": REDACTED, "expr0": 3}]
    owner = inv.soql_query("prod", "SELECT Id, Owner.Username FROM Case")
    assert owner["records"][0]["Owner"]["Username"] == REDACTED and "jdoe" not in json.dumps(owner)


def test_an_address_in_the_query_text_is_not_saved_and_a_saved_log_is_read_masked(project):
    """Second review: a WHERE literal reached queries/*.json, and read_file showed a saved log unmasked."""
    inv = investigator(project, ("07LWs00000L0001MAA", [LOG]))
    inv.init_ticket(KEY)
    out = inv.soql_query("prod", "SELECT Id FROM Contact WHERE Email = 'jane.doe@customer.test'", ticket_key=KEY,
                         save_as="by-mail")
    assert "jane.doe@customer.test" not in saved(project, out)
    inv.get_debug_log("prod", "07LWs00000L0001MAA", KEY)
    shown = inv.read_file(KEY, "logs/07LWs00000L0001MAA.log")["content"]
    assert "jane.doe@customer.test" not in shown and "[email]" in shown
