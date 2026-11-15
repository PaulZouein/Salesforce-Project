from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import prereqs
from core.sf_cli import SfCli
from simulator import fake_sf
from simulator.build import DEFAULT_ORGS

ROOT = Path(__file__).resolve().parent.parent


# --- prerequisites ----------------------------------------------------------------------


def test_prereqs_pass_against_the_simulator(sim, monkeypatch, capsys):
    monkeypatch.setenv("SFTI_SF_EXECUTABLE", str(sim.launcher))
    checks = {c["name"]: c for c in prereqs.run_checks()}
    assert checks["Python"]["ok"] and checks["git"]["ok"] and checks["mcp Python package"]["ok"]
    assert "simulated" in checks["Salesforce CLI (sf)"]["detail"]
    assert checks["Authenticated org"]["ok"] and "5 found" in checks["Authenticated org"]["detail"]
    assert prereqs.main([]) == 0
    assert "All prerequisites are met." in capsys.readouterr().out
    assert prereqs.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_prereqs_fail_with_exit_code_1_when_the_cli_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SFTI_SF_EXECUTABLE", str(tmp_path / "no-sf-here"))
    assert prereqs.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Salesforce CLI (sf)" in out and "required prerequisite(s) missing" in out
    checks = {c["name"]: c for c in prereqs.run_checks(SfCli(executable=str(tmp_path / "no-sf-here")))}
    assert checks["Authenticated org"]["ok"] is False and "skipped" in checks["Authenticated org"]["detail"]


def test_prereqs_as_a_module(sim):
    import os
    result = subprocess.run([sys.executable, "-m", "core.prereqs", "--json"], capture_output=True, text=True,
                            cwd=ROOT, env={**os.environ, "SFTI_SF_EXECUTABLE": str(sim.launcher)}, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "accessToken" not in result.stdout and "SIMULATED-TOKEN" not in result.stdout


# --- simulator --------------------------------------------------------------------------

RECORDS = {
    "Account": [{"Id": "001000000000001AAA", "Name": "A", "Tier__c": "Premium"},
                {"Id": "001000000000002AAA", "Name": "B", "Tier__c": "Standard"}],
    "Case": [
        {"Id": "500000000000001AAA", "Priority": "High", "AccountId": "001000000000001AAA", "N__c": 3,
         "CreatedDate": "2026-11-10T02:00:14.000+0000", "Email__c": None},
        {"Id": "500000000000002AAA", "Priority": "Medium", "AccountId": "001000000000002AAA", "N__c": 10,
         "CreatedDate": "2026-11-11T02:00:09.000+0000", "Email__c": "x@customer.test"},
        {"Id": "500000000000003AAA", "Priority": "medium", "AccountId": None, "N__c": 7,
         "CreatedDate": "2026-11-12T02:00:11.000+0000", "Email__c": None},
    ],
}


def ids(query):
    return [r["Id"][12:15] for r in fake_sf.run_query(query, RECORDS)["records"]]


@pytest.mark.parametrize("query,expected", [
    ("SELECT Id FROM Case", ["001", "002", "003"]),
    ("select Id from case where Priority = 'Medium'", ["002", "003"]),  # string comparison ignores case
    ("SELECT Id FROM Case WHERE Priority != 'High' AND N__c > 7", ["002"]),
    ("SELECT Id FROM Case WHERE N__c >= 7 ORDER BY N__c DESC", ["002", "003"]),
    ("SELECT Id FROM Case WHERE AccountId = null", ["003"]),
    ("SELECT Id FROM Case WHERE Email__c != null", ["002"]),
    ("SELECT Id FROM Case WHERE Account.Tier__c = 'Premium'", ["001"]),
    ("SELECT Id FROM Case WHERE Id IN ('500000000000001AAA', '500000000000003')", ["001", "003"]),
    ("SELECT Id FROM Case WHERE Id NOT IN ('500000000000001AAA')", ["002", "003"]),
    ("SELECT Id FROM Case WHERE Priority LIKE 'me%'", ["002", "003"]),
    ("SELECT Id FROM Case WHERE CreatedDate >= 2026-11-11T00:00:00Z", ["002", "003"]),
    ("SELECT Id FROM Case WHERE CreatedDate < 2026-11-11T00:00:00Z", ["001"]),
    ("SELECT Id FROM Case ORDER BY CreatedDate DESC LIMIT 2", ["003", "002"]),
    ("SELECT Id FROM Case WHERE Priority = 'it''s' OR Priority = 'x'", None),
])
def test_simulator_soql_subset(query, expected):
    if expected is None:
        # Honest about its limits: what it cannot evaluate is an error, never a silent wrong answer.
        with pytest.raises(fake_sf.CliError):
            fake_sf.run_query(query, RECORDS)
    else:
        assert ids(query) == expected


def test_simulator_relationship_projection_and_count():
    result = fake_sf.run_query("SELECT Id, Account.Name, Account.Tier__c FROM Case ORDER BY Id", RECORDS)
    first, _, third = result["records"]
    assert first["Account"] == {"attributes": {"type": "Account"}, "Name": "A", "Tier__c": "Premium"}
    assert third["Account"] is None
    assert fake_sf.run_query("SELECT COUNT() FROM Case WHERE Priority = 'High'", RECORDS) == {
        "records": [], "totalSize": 1, "done": True}


@pytest.mark.parametrize("query", [
    "SELECT Id, (SELECT Id FROM Cases) FROM Account",
    "SELECT COUNT(Id), Priority FROM Case GROUP BY Priority",
    "SELECT Id FROM Nope__c",
    "UPDATE Case SET Priority = 'High'",
])
def test_simulator_rejects_what_it_cannot_do(query):
    with pytest.raises(fake_sf.CliError):
        fake_sf.run_query(query, RECORDS)


def test_simulator_unknown_command_and_envelope(sim):
    proc = subprocess.run([sys.executable, str(ROOT / "simulator" / "fake_sf.py"), "org", "login", "web", "--json"],
                          capture_output=True, text=True, timeout=60)
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 127 and "not implemented by the simulator" in envelope["message"]
    assert {"name", "message", "exitCode", "status"} <= set(envelope)


def test_simulator_does_not_enforce_org_type_so_the_guard_is_what_is_tested(sim):
    """The simulator would happily deploy to its 'production' org; only core/org_guard prevents it."""
    source = (ROOT / "simulator" / "fake_sf.py").read_text(encoding="utf-8")
    deploy = source[source.index("def cmd_deploy"):source.index("def cmd_delete_source")]
    assert "isSandbox" not in deploy and "organizationType" not in deploy


def test_setup_command_builds_the_demo_orgs(tmp_path, monkeypatch, capsys):
    import simulator.__main__ as entry
    monkeypatch.setattr(entry, "DEFAULT_STATE", tmp_path / "state")
    assert entry.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "SFTI_SF_EXECUTABLE" in out and "demo-prod (production)" in out
    assert sorted(p.name for p in (tmp_path / "state" / "orgs").iterdir()) == sorted(o["alias"] for o in DEFAULT_ORGS)
    assert entry.main([]) == 2 and entry.main(["nope"]) == 2
