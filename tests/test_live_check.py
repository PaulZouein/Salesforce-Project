"""The kit for a real Developer Edition org: the read-only live check and the sample that reproduces the bug."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import live_check  # noqa: E402

from mcp_server import server  # noqa: E402

from .conftest import TEST_ORGS  # noqa: E402

LIVE = ROOT / "samples" / "live-org"
PLANTED = ROOT / "samples" / "planted-bug" / "force-app" / "main" / "default"


@pytest.fixture
def against_the_simulator(sim, project, monkeypatch):
    monkeypatch.setenv("SFTI_SF_EXECUTABLE", str(sim.launcher))
    monkeypatch.setenv("SFTI_PROJECT_ROOT", str(project.root))
    monkeypatch.setattr(server, "_investigator", None)
    yield
    server._investigator = None


def test_live_check_runs_every_read_only_check_and_writes_a_shareable_report(against_the_simulator, tmp_path, capsys):
    code = live_check.main(["--org", "prod", "--out", str(tmp_path / "out")])
    assert code == 0
    reports = sorted((tmp_path / "out").glob("live-check-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    results = json.loads(reports[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert "**21 worked, 0 failed, 1 skipped.**" in text and "Read-only: nothing was changed" in text
    tools = [r["tool"] for r in results if r.get("ok")]
    for tool in ("org_info", "describe_object", "soql_query", "tooling_query", "recent_org_changes",
                 "find_dependencies", "retrieve_components", "summarize_flow", "list_debug_logs", "get_debug_log",
                 "analyze_debug_log", "record_history", "verify_org_knowledge", "recall_org_knowledge"):
        assert tool in tools, tool
    assert all(tool in server.READ_ONLY_TOOLS for tool in tools)
    # Nothing that identifies the org or a person: no usernames, org Id, web address, record values.
    for org in TEST_ORGS:
        for secret in (org["username"], org["orgId"], org["orgId"][:15], org["instanceUrl"], org["name"]):
            assert secret not in text and secret not in json.dumps(results), secret
    assert "@" not in text
    assert "Report:" in capsys.readouterr().out


def test_live_check_runs_apex_tests_only_when_asked_and_never_on_production(against_the_simulator, tmp_path):
    results = live_check.run("sandbox", ["CaseEscalationServiceTest"])
    tests = next(r for r in results if r["tool"] == "run_apex_tests")
    assert tests["ok"] is True and "2 passing, 0 failing" in tests["summary"]
    results = live_check.run("prod", ["CaseEscalationServiceTest"])
    tests = next(r for r in results if r["tool"] == "run_apex_tests")
    assert tests["ok"] is False and tests["error"]["code"] == "production_test_run_blocked"
    skipped = next(r for r in live_check.run("prod") if r["tool"] == "run_apex_tests")
    assert skipped["ok"] is None and "--tests" in skipped["skipped"]


def test_an_org_that_cannot_be_reached_stops_the_check_early_with_the_reason(against_the_simulator, tmp_path):
    code = live_check.main(["--org", "nobody", "--out", str(tmp_path)])
    assert code == 1
    results = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert [r["check"] for r in results][-1] == "Everything else"
    assert next(r for r in results if r["tool"] == "org_info")["ok"] is False


def test_the_live_check_can_only_call_read_only_tools(against_the_simulator):
    checker = live_check.Checker("prod")
    for name in server.WRITE_TOOLS:
        with pytest.raises(RuntimeError, match="not a read-only tool"):
            checker.call(name, org="prod")
    source = (ROOT / "scripts" / "live_check.py").read_text(encoding="utf-8")
    for name in server.WRITE_TOOLS:
        assert f'"{name}"' not in source


@pytest.mark.parametrize("raw,expected", [
    ("No authorization for jane.doe@acme.example", "No authorization for [email]"),
    ("see https://acme.my.salesforce.com/services/data", "see [url]"),
    ("org 00DWs000000PrdAMAS and 00DWs000000PrdA", "org 00D[org] and 00D[org]"),
    ("file C:\\Users\\jane\\project\\tickets\\x.json missing", "file [path] missing"),
    ("file /home/jane/project/tickets/x.json missing", "file [path] missing"),
])
def test_what_goes_into_the_report_is_scrubbed(raw, expected):
    assert live_check.scrub(raw) == expected


# --- the sample for a real org ----------------------------------------------------------------


def test_the_sample_data_reproduces_the_bug_in_one_batch():
    plan = json.loads((LIVE / "data" / "plan.json").read_text(encoding="utf-8"))
    assert [step["sobject"] for step in plan] == ["Account", "Case"]
    for step in plan:
        for name in step["files"]:
            assert (LIVE / "data" / name).is_file(), name
    accounts = json.loads((LIVE / "data" / "Account.json").read_text(encoding="utf-8"))["records"]
    cases = json.loads((LIVE / "data" / "Case.json").read_text(encoding="utf-8"))["records"]
    field = (PLANTED / "objects" / "Account" / "fields" / "Support_Tier__c.field-meta.xml").read_text(encoding="utf-8")
    values = set(re.findall(r"<fullName>(\w+)</fullName>\s*<default>", field))
    assert values == {"Standard", "Premium"}
    tiers = {a["attributes"]["referenceId"]: a["Support_Tier__c"] for a in accounts}
    assert set(tiers.values()) <= values
    order = [tiers[c["AccountId"].lstrip("@")] for c in cases]
    # A Premium case, then a Standard one, then Premium cases: the ones after the Standard case show the bug.
    assert order == ["Premium", "Standard", "Premium", "Premium"]
    assert all("Priority" not in c for c in cases)          # left to the org's default, like the import in the ticket
    assert len(cases) <= 200                                 # one request, so one run of the trigger


def test_the_permission_set_grants_the_sample_field_and_the_ticket_is_ready():
    permset = (LIVE / "force-app" / "main" / "default" / "permissionsets" /
               "Ticket_Investigator_Sample.permissionset-meta.xml").read_text(encoding="utf-8")
    assert "<field>Account.Support_Tier__c</field>" in permset and "<readable>true</readable>" in permset
    assert json.loads((LIVE / "sfdx-project.json").read_text(encoding="utf-8"))["packageDirectories"][0]["path"] == "force-app"
    from webapp.tickets import detect_key, detect_title
    ticket = (LIVE / "ticket.txt").read_text(encoding="utf-8")
    assert detect_key(ticket) == "LIVE-1" and detect_title(ticket).startswith("Some Premium cases")
    guide = (ROOT / "docs" / "LIVE-ORG.md").read_text(encoding="utf-8")
    for name in ("Ticket_Investigator_Sample", "data/plan.json", "live_check.py", "allow_developer_edition_writes"):
        assert name in guide, name
    # The deploy commands name folders that exist.
    for folder in re.findall(r"--source-dir (\S+)", guide):
        base = LIVE if folder == "force-app" else ROOT / "samples" / "planted-bug"
        assert (base / folder).is_dir(), folder
