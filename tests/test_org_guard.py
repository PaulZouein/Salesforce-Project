from __future__ import annotations

import itertools
import json

import pytest

from core.audit import AuditLog
from core.config import Settings
from core.errors import (
    DeveloperEditionWriteBlockedError,
    ProductionTestRunBlockedError,
    ProductionWriteBlockedError,
    SfCliError,
    WriteModeDisabledError,
)
from core.org_guard import OrgGuard, classify_org
from core.sf_cli import KIND_TEST, KIND_WRITE, ProcessResult, SfCli

from .conftest import SIMULATED_TOKEN


# --- classification table ---------------------------------------------------------------


@pytest.mark.parametrize("is_sandbox,org_type,is_scratch,expected", [
    (True, "Enterprise Edition", False, "sandbox"),
    (True, "Developer Edition", False, "sandbox"),
    (True, "Developer Edition", True, "scratch"),
    (True, None, True, "scratch"),
    (False, "Developer Edition", False, "developer"),
    (False, "Developer Edition", True, "developer"),
    (False, "Enterprise Edition", False, "production"),
    (False, "Unlimited Edition", False, "production"),
    (False, "Professional Edition", True, "production"),
    (False, None, False, "production"),
    (False, "developer edition", False, "production"),
    (False, "", False, "production"),
    # fail closed
    (None, "Developer Edition", False, "production"),
    (None, None, True, "production"),
    ("true", "Enterprise Edition", False, "production"),
    ("false", "Developer Edition", False, "production"),
    (1, "Enterprise Edition", False, "production"),
    (0, "Developer Edition", False, "production"),
    ([], None, False, "production"),
])
def test_classify_org(is_sandbox, org_type, is_scratch, expected):
    assert classify_org(is_sandbox, org_type, is_scratch) == expected


@pytest.mark.parametrize("alias,classification,writes", [
    ("sandbox", "sandbox", True),
    ("scratch", "scratch", True),
    ("dev", "developer", False),
    ("prod", "production", False),
    ("broken", "production", False),
])
def test_org_info_against_the_simulator(inv, alias, classification, writes):
    info = inv.org_info(alias)
    assert info["classification"] == classification
    assert info["writes_allowed"] is writes
    assert info["org_id"].startswith("00D")
    assert SIMULATED_TOKEN not in json.dumps(info)
    if alias == "broken":
        assert "no row" in info["classification_reason"]
        assert info["is_sandbox"] is None


def test_developer_org_reports_writes_allowed_only_with_the_setting(inv, project):
    project.write_config(allow_developer_edition_writes=True)
    assert inv.org_info("dev")["writes_allowed"] is True
    assert inv.org_info("prod")["writes_allowed"] is False


def fake_cli(organization_response):
    """SfCli whose ``org display`` succeeds and whose Organization query returns the given response."""

    def runner(argv, cwd, timeout, env):
        if argv[1:3] == ["org", "display"]:
            return ProcessResult(0, json.dumps({"status": 0, "result": {
                "id": "00DFAKE", "username": "u@example.com", "apiVersion": "62.0", "accessToken": "T"}}), "")
        if isinstance(organization_response, BaseException):
            raise organization_response
        return ProcessResult(int(organization_response.get("status", 0)), json.dumps(organization_response), "")

    return SfCli(executable="sf-test", runner=runner)


def org_row(**fields):
    return {"status": 0, "result": {"records": [{"attributes": {"type": "Organization"}, "Id": "00DFAKE", **fields}],
                                    "totalSize": 1}}


@pytest.mark.parametrize("response,fragment", [
    (org_row(IsSandbox="true", OrganizationType="Enterprise Edition"), "not a boolean"),
    (org_row(IsSandbox=None, OrganizationType="Developer Edition"), "not a boolean"),
    (org_row(OrganizationType="Developer Edition"), "not a boolean"),
    ({"status": 0, "result": {"records": [], "totalSize": 0}}, "no row"),
    ({"status": 1, "name": "INVALID_TYPE", "message": "sObject type 'Organization' is not supported."}, "could not be read"),
    (TimeoutError(), "could not be read"),
    (ProcessResult(0, "garbage", ""), "could not be read"),
])
def test_org_info_fails_closed(response, fragment):
    if isinstance(response, ProcessResult):
        def runner(argv, cwd, timeout, env):
            if argv[1:3] == ["org", "display"]:
                return ProcessResult(0, '{"status":0,"result":{"id":"00DFAKE"}}', "")
            return response
        cli = SfCli(executable="sf-test", runner=runner)
    else:
        cli = fake_cli(response)
    info = OrgGuard(cli, settings_provider=lambda: Settings(mode="apply", allow_developer_edition_writes=True)).org_info("x")
    assert info.classification == "production"
    assert info.writes_allowed is False
    assert fragment in info.classification_reason


def test_a_scratch_org_is_recognised_from_its_own_record():
    """The CLI's rule: a sandbox with a trial expiration date is a scratch org, whatever org display says."""
    info = OrgGuard(fake_cli(org_row(IsSandbox=True, OrganizationType="Developer Edition",
                                     TrialExpirationDate="2026-12-01T00:00:00.000+0000"))).org_info("x")
    assert info.classification == "scratch" and info.is_scratch is True and info.writes_allowed is True


def display_fails_runner(organization_response, error_name="NoScratchInfoError"):
    """``org display`` of a scratch org whose Dev Hub is not connected here fails; the org may still answer."""

    def runner(argv, cwd, timeout, env):
        if argv[1:3] == ["org", "display"]:
            return ProcessResult(1, json.dumps({"status": 1, "name": error_name,
                                                "message": "Could not find the scratch org information."}), "")
        return ProcessResult(int(organization_response.get("status", 0)), json.dumps(organization_response), "")

    return SfCli(executable="sf-test", runner=runner)


def test_a_scratch_org_whose_dev_hub_is_not_connected_is_classified_but_not_writable():
    info = OrgGuard(display_fails_runner(org_row(
        IsSandbox=True, OrganizationType="Developer Edition",
        TrialExpirationDate="2026-12-01T00:00:00.000+0000"))).org_info("x")
    assert info.classification == "scratch" and info.username is None
    assert info.writes_allowed is False and "'org display' failed" in info.classification_reason


def test_an_org_that_cannot_be_reached_at_all_is_still_an_error():
    cli = display_fails_runner({"status": 1, "name": "NamedOrgNotFoundError", "message": "No authorization found."},
                               error_name="NamedOrgNotFoundError")
    with pytest.raises(SfCliError) as caught:
        OrgGuard(cli).org_info("typo")
    assert caught.value.name == "NamedOrgNotFoundError"


def test_a_developer_org_that_holds_a_package_namespace_is_read_only_even_when_opted_in():
    guard = OrgGuard(fake_cli(org_row(IsSandbox=False, OrganizationType="Developer Edition", NamespacePrefix="acme")),
                     settings_provider=lambda: Settings(mode="apply", allow_developer_edition_writes=True))
    info = guard.org_info("x")
    assert info.classification == "developer" and info.writes_allowed is False and info.namespace_prefix == "acme"
    with pytest.raises(DeveloperEditionWriteBlockedError) as caught:
        guard.authorize_write("x", "deploy_component")
    assert "managed package" in caught.value.message


# --- authorize_write --------------------------------------------------------------------


def guard_for(inv, **settings):
    return OrgGuard(inv.cli, settings_provider=lambda: Settings(**settings), audit=inv.audit)


ALL_SETTINGS = [
    dict(mode=mode, allow_developer_edition_writes=dev, allow_apex_tests_on_production=tests)
    for mode, dev, tests in itertools.product(("advise", "apply"), (False, True), (False, True))
]


@pytest.mark.parametrize("settings", ALL_SETTINGS)
@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("alias", ["prod", "broken"])
def test_production_is_refused_in_every_combination(inv, sim, settings, diagnostic, alias):
    before = sim.fingerprint(alias)
    with pytest.raises(ProductionWriteBlockedError) as err:
        guard_for(inv, **settings).authorize_write(alias, "deploy_component", diagnostic=diagnostic)
    assert "cannot be overridden" in (err.value.hint or "")
    assert sim.fingerprint(alias) == before


def test_unknown_org_is_treated_as_production(inv):
    with pytest.raises(ProductionWriteBlockedError):
        guard_for(inv, mode="apply").authorize_write("nobody", "deploy_component")
    entry = inv.audit.entries(limit=1)[0]
    assert entry["outcome"] == "blocked" and entry["org_target"] == "nobody"


def test_no_environment_variable_or_argument_unlocks_production(inv, monkeypatch):
    for name in ("SFTI_ALLOW_PRODUCTION", "SFTI_FORCE", "SFTI_OVERRIDE", "SFTI_MODE", "FORCE"):
        monkeypatch.setenv(name, "1")
    with pytest.raises(ProductionWriteBlockedError):
        guard_for(inv, mode="apply").authorize_write("prod", "deploy_component")
    import inspect
    params = set(inspect.signature(OrgGuard.authorize_write).parameters)
    assert params == {"self", "target", "operation", "commands", "diagnostic", "ticket_key", "approved_by"}


def test_developer_edition_refused_unless_enabled(inv):
    with pytest.raises(DeveloperEditionWriteBlockedError):
        guard_for(inv, mode="apply").authorize_write("dev", "deploy_component")
    with pytest.raises(DeveloperEditionWriteBlockedError):
        guard_for(inv, mode="advise").authorize_write("dev", "set_trace_flag", diagnostic=True)
    permit, info = guard_for(inv, mode="apply", allow_developer_edition_writes=True).authorize_write(
        "dev", "deploy_component")
    assert permit.kind == KIND_WRITE and info.classification == "developer"
    with pytest.raises(WriteModeDisabledError):
        guard_for(inv, mode="advise", allow_developer_edition_writes=True).authorize_write("dev", "deploy_component")


@pytest.mark.parametrize("alias", ["sandbox", "scratch"])
def test_sandbox_modes(inv, alias):
    with pytest.raises(WriteModeDisabledError) as err:
        guard_for(inv, mode="advise").authorize_write(alias, "deploy_component")
    assert "apply" in err.value.hint
    permit, _ = guard_for(inv, mode="advise").authorize_write(alias, "set_trace_flag", diagnostic=True, commands=3)
    assert permit.uses_left == 3 and permit.target == alias
    permit, info = guard_for(inv, mode="apply").authorize_write(alias, "deploy_component")
    assert permit.uses_left == 1
    assert permit.org_id == info.org_id


def test_audit_rows_for_allowed_and_blocked(inv):
    guard = guard_for(inv, mode="apply")
    guard.authorize_write("sandbox", "deploy_component", ticket_key="T-1", approved_by="tester")
    with pytest.raises(ProductionWriteBlockedError):
        guard.authorize_write("prod", "deploy_component", ticket_key="T-1", approved_by="tester")
    blocked, allowed = inv.audit.entries(limit=2)
    assert (allowed["outcome"], allowed["classification"], allowed["org_target"]) == ("allowed", "sandbox", "sandbox")
    assert allowed["approved_by"] == "tester" and allowed["ticket_key"] == "T-1"
    assert (blocked["outcome"], blocked["classification"]) == ("blocked", "production")
    assert blocked["detail"]["code"] == "production_write_blocked"
    assert inv.audit.entries(ticket_key="other") == []


def test_classification_is_read_live_not_cached(inv, sim):
    guard = guard_for(inv, mode="apply")
    guard.authorize_write("sandbox", "deploy_component")
    org_file = sim.org_dir("sandbox") / "org.json"
    org = json.loads(org_file.read_text())
    org["isSandbox"] = False  # the alias now points at a production org
    org_file.write_text(json.dumps(org))
    with pytest.raises(ProductionWriteBlockedError):
        guard.authorize_write("sandbox", "deploy_component")


# --- authorize_test_run -----------------------------------------------------------------


@pytest.mark.parametrize("alias", ["sandbox", "scratch", "dev"])
def test_test_runs_allowed_off_production(inv, alias):
    permit, _ = guard_for(inv).authorize_test_run(alias)
    assert permit.kind == KIND_TEST and permit.uses_left == 1


@pytest.mark.parametrize("setting,confirmed,allowed", [
    (False, False, False),
    (False, True, False),
    (True, False, False),
    (True, True, True),
])
@pytest.mark.parametrize("alias", ["prod", "broken"])
def test_test_run_matrix_on_production(inv, alias, setting, confirmed, allowed):
    guard = guard_for(inv, mode="apply", allow_apex_tests_on_production=setting)
    if allowed:
        permit, info = guard.authorize_test_run(alias, production_confirmed=confirmed)
        assert permit.kind == KIND_TEST and info.classification == "production"
        assert inv.audit.entries(limit=1)[0]["outcome"] == "allowed"
    else:
        with pytest.raises(ProductionTestRunBlockedError):
            guard.authorize_test_run(alias, production_confirmed=confirmed)
        assert inv.audit.entries(limit=1)[0]["outcome"] == "blocked"


def test_a_test_permit_cannot_run_a_write(inv):
    permit, _ = guard_for(inv, allow_apex_tests_on_production=True).authorize_test_run("prod", production_confirmed=True)
    from core.errors import WritePermitError
    with pytest.raises(WritePermitError):
        inv.cli.run(["project", "deploy", "start", "--metadata", "ApexClass:CaseTriggerHandler",
                     "--target-org", "prod"], permit=permit)


def test_unknown_org_test_run_blocked(inv):
    with pytest.raises(ProductionTestRunBlockedError):
        guard_for(inv, allow_apex_tests_on_production=True).authorize_test_run("nobody", production_confirmed=True)


def test_list_orgs_deduplicates_and_hides_tokens(inv):
    out = inv.list_orgs()
    assert out["count"] == 5
    assert SIMULATED_TOKEN not in json.dumps(out)
    sandbox = next(o for o in out["orgs"] if o["alias"] == "sandbox")
    assert set(sandbox["listed_under"]) == {"nonScratchOrgs", "sandboxes"}


def test_org_display_failure_propagates_for_reads(inv):
    with pytest.raises(SfCliError):
        inv.org_info("nobody")
