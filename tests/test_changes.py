"""Apply-mode write path, exercised on the simulated sandbox through the real subprocess path."""

from __future__ import annotations

import json

import pytest

from core.errors import (
    ChangeError,
    DeployFailedError,
    DeveloperEditionWriteBlockedError,
    DriftError,
    InputValidationError,
    ProductionWriteBlockedError,
    WriteModeDisabledError,
)
from core.gitrepo import GitRepo

from .conftest import FIX_COMPONENTS, SERVICE, SERVICE_TEST, apply_planted_bug_fix

KEY = "T-1"
SERVICE_SRC = "classes/CaseEscalationService.cls"
TEST_SRC = "classes/CaseEscalationServiceTest.cls"
REASON = "T-1: escalate every Premium case in a mixed batch"
CASE_3 = "500Ws00000C0003IAA"
CASE_4 = "500Ws00000C0004IAA"


def retrieve(inv, org="sandbox"):
    return inv.retrieve_components(org, KEY, FIX_COMPONENTS + ["ApexClass:CaseTriggerHandler"])


def outcomes(inv, operation):
    return [e["outcome"] for e in inv.audit.entries(limit=100) if e["operation"] == operation]


# --- the planted-bug fix, end to end ----------------------------------------------------


def test_planted_bug_fix_validate_deploy_test_rollback(apply_inv, sim, project):
    inv = apply_inv
    original_service = sim.source("sandbox", SERVICE_SRC).read_bytes()
    original_test = sim.source("sandbox", TEST_SRC).read_bytes()
    retrieve(inv)
    apply_planted_bug_fix(project.ticket(KEY))

    diff = inv.workspace_diff(KEY)
    assert {f["path"] for f in diff["files"]} == {SERVICE, SERVICE_TEST}
    assert "-                return;\n+                continue;" in diff["diff"]
    assert "mixedBatchEscalatesEveryPremiumCase" in diff["diff"]

    # 1. validate only: tests run against the would-be state, the org is untouched
    before = sim.fingerprint("sandbox")
    validated = inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON, validate_only=True,
                                     test_level="RunSpecifiedTests", tests=["CaseEscalationServiceTest"])
    assert validated["status"] == "validated" and validated["tests_run"] == 3
    assert sim.fingerprint("sandbox") == before
    assert "continue;" in (project.ticket(KEY) / SERVICE).read_text()  # the edit is still in the working tree

    # 2. real deploy
    deployed = inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON,
                                    test_level="RunSpecifiedTests", tests=["CaseEscalationServiceTest"],
                                    approved_by="tester")
    assert deployed["status"] == "deployed" and deployed["verified"] is True
    assert deployed["classification"] == "sandbox" and deployed["tests_run"] == 3
    assert deployed["change_id"].startswith("CHG-")
    assert "continue;" in deployed["diff"]
    assert b"continue;" in sim.source("sandbox", SERVICE_SRC).read_bytes()
    assert b"mixedBatchEscalatesEveryPremiumCase" in sim.source("sandbox", TEST_SRC).read_bytes()
    assert sim.source("sandbox", SERVICE_SRC).read_bytes() == (project.ticket(KEY) / SERVICE).read_bytes()

    repo = GitRepo(project.ticket(KEY) / "metadata")
    assert repo.is_clean()
    assert "return;" in repo.show(deployed["pre_commit"], SERVICE[len("metadata/"):]).splitlines()[29]
    assert "continue;" in repo.show(deployed["post_commit"], SERVICE[len("metadata/"):]).splitlines()[29]
    assert "No local edits" in inv.workspace_diff(KEY)["note"]

    # 3. tests pass in the org, including the new one
    tests = inv.run_apex_tests("sandbox", ["CaseEscalationServiceTest"], ticket_key=KEY)
    assert (tests["outcome"], tests["passing"], tests["failing"]) == ("Passed", 3, 0)

    listed = inv.list_changes(KEY)["changes"]
    assert [(c["change_id"], c["status"]) for c in listed] == [
        (deployed["change_id"], "deployed"), (validated["change_id"], "validated")]
    assert listed[0]["components"] == FIX_COMPONENTS

    # 4. rollback restores the org byte for byte
    rolled = inv.rollback_change(deployed["change_id"], reason="demonstrate rollback", approved_by="tester")
    assert rolled["status"] == "rolled_back" and rolled["verified"] is True
    assert sorted(rolled["restored_components"]) == sorted(FIX_COMPONENTS) and rolled["deleted_components"] == []
    assert sim.source("sandbox", SERVICE_SRC).read_bytes() == original_service
    assert sim.source("sandbox", TEST_SRC).read_bytes() == original_test
    assert sim.fingerprint("sandbox") == before
    assert inv.list_changes(KEY)["changes"][0]["status"] == "rolled_back"

    assert outcomes(inv, "validate_component") == ["succeeded", "allowed"]
    assert outcomes(inv, "deploy_component") == ["succeeded", "allowed"]
    assert outcomes(inv, "rollback_change") == ["succeeded", "allowed"]

    with pytest.raises(ChangeError):  # a change is rolled back once
        inv.rollback_change(deployed["change_id"], reason="twice is refused")
    with pytest.raises(ChangeError):  # validation-only changes never modified the org
        inv.rollback_change(validated["change_id"], reason="nothing to undo")


def test_fix_without_its_test_fails_validation_and_leaves_org_unchanged(apply_inv, sim, project):
    """Deploying only the new test (service still buggy) fails: the simulated test rule fires."""
    inv = apply_inv
    retrieve(inv)
    apply_planted_bug_fix(project.ticket(KEY))
    service = project.ticket(KEY) / SERVICE
    service.write_text(service.read_text().replace("continue;", "return;"))  # undo the fix, keep the new test
    before = sim.fingerprint("sandbox")
    with pytest.raises(DeployFailedError) as err:
        inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationServiceTest"], reason=REASON,
                             test_level="RunSpecifiedTests", tests=["CaseEscalationServiceTest"])
    failure = err.value.details["test_failures"][0]
    assert failure["name"] == "CaseEscalationServiceTest.mixedBatchEscalatesEveryPremiumCase"
    assert "Expected: High, Actual: Medium" in failure["message"]
    assert sim.fingerprint("sandbox") == before
    assert "mixedBatchEscalatesEveryPremiumCase" in (project.ticket(KEY) / SERVICE_TEST).read_text()
    assert inv.list_changes(KEY)["changes"][0]["status"] == "failed"
    assert outcomes(inv, "deploy_component") == ["failed", "allowed"]


def test_compile_failure_leaves_org_unchanged_and_keeps_the_edit(apply_inv, sim, project):
    inv = apply_inv
    retrieve(inv)
    service = project.ticket(KEY) / SERVICE
    broken = service.read_text().replace("c.Priority = HIGH_PRIORITY;", "c.Priority = HIGH_PRIORITY; }")
    service.write_text(broken)
    before = sim.fingerprint("sandbox")
    with pytest.raises(DeployFailedError) as err:
        inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)
    details = err.value.details
    assert details["component_failures"][0]["component"] == "ApexClass:CaseEscalationService"
    assert details["component_failures"][0]["line"]
    assert "not changed" in err.value.message
    assert sim.fingerprint("sandbox") == before
    assert service.read_text() == broken
    change = inv.audit.get_change(details["change_id"])
    assert change["status"] == "failed" and change["post_commit"] is None
    with pytest.raises(ChangeError):
        inv.rollback_change(details["change_id"], reason="failed deploys cannot be rolled back")


def test_drift_is_detected_and_the_local_edit_survives(apply_inv, sim, project):
    inv = apply_inv
    retrieve(inv)
    apply_planted_bug_fix(project.ticket(KEY))
    local_edit = (project.ticket(KEY) / SERVICE).read_text()

    # Someone else changes the class in the org after our retrieve.
    org_file = sim.source("sandbox", SERVICE_SRC)
    org_file.write_text(org_file.read_text().replace("PREMIUM_TIER = 'Premium'", "PREMIUM_TIER = 'Platinum'"))
    drifted_org = sim.fingerprint("sandbox")

    with pytest.raises(DriftError) as err:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    assert err.value.details["drifted"] == [SERVICE[len("metadata/"):]]
    assert "Platinum" in err.value.details["org_changes"]
    assert "Nothing was deployed" in err.value.message
    assert sim.fingerprint("sandbox") == drifted_org
    assert (project.ticket(KEY) / SERVICE).read_text() == local_edit
    assert outcomes(inv, "deploy_component") == ["failed", "allowed"]
    assert inv.list_changes(KEY)["changes"] == []

    # The org's current version is now in the repository history for review.
    repo = GitRepo(project.ticket(KEY) / "metadata")
    assert "Platinum" in repo.show("HEAD", SERVICE[len("metadata/"):])
    # The diff is now relative to the org's current version, so the reviewer sees what a deploy would overwrite.
    diff = inv.workspace_diff(KEY)["diff"]
    assert "-    public static final String PREMIUM_TIER = 'Platinum';" in diff


def test_never_retrieved_component_counts_as_drift(apply_inv, sim, project):
    inv = apply_inv
    inv.init_ticket(KEY)
    repo = inv.workspace.metadata_repo(KEY)
    target = repo.path / "force-app/main/default/classes/CaseTriggerHandler.cls"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("public class CaseTriggerHandler { /* written blind */ }\n")
    before = sim.fingerprint("sandbox")
    with pytest.raises(DriftError) as err:
        inv.deploy_component("sandbox", KEY, ["ApexClass:CaseTriggerHandler"], reason=REASON)
    assert err.value.details["never_retrieved"]
    assert sim.fingerprint("sandbox") == before
    assert "written blind" in target.read_text()


def test_nothing_to_deploy(apply_inv, sim):
    inv = apply_inv
    retrieve(inv)
    before = sim.fingerprint("sandbox")
    with pytest.raises(ChangeError) as err:
        inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)
    assert "Nothing to deploy" in err.value.message
    assert sim.fingerprint("sandbox") == before


def test_new_component_is_created_and_rollback_deletes_it(apply_inv, sim, project):
    inv = apply_inv
    retrieve(inv)
    before = sim.fingerprint("sandbox")
    classes = project.ticket(KEY) / "metadata/force-app/main/default/classes"
    (classes / "CasePriorityHelper.cls").write_text("public class CasePriorityHelper {\n}\n")
    (classes / "CasePriorityHelper.cls-meta.xml").write_text(
        (classes / "CaseTriggerHandler.cls-meta.xml").read_text())

    deployed = inv.deploy_component("sandbox", KEY, ["ApexClass:CasePriorityHelper"], reason=REASON)
    assert deployed["status"] == "deployed" and deployed["verified"] is True
    assert {f["state"] for f in deployed["deployed_files"]} == {"Created"}
    assert sim.source("sandbox", "classes/CasePriorityHelper.cls").is_file()

    rolled = inv.rollback_change(deployed["change_id"], reason="remove the helper again")
    assert rolled["deleted_components"] == ["ApexClass:CasePriorityHelper"] and rolled["restored_components"] == []
    assert not sim.source("sandbox", "classes/CasePriorityHelper.cls").exists()
    assert not sim.source("sandbox", "classes/CasePriorityHelper.cls-meta.xml").exists()
    assert sim.fingerprint("sandbox") == before
    assert not (classes / "CasePriorityHelper.cls").exists()
    assert GitRepo(project.ticket(KEY) / "metadata").is_clean()


def test_mixed_change_and_creation_roll_back_together(apply_inv, sim, project):
    inv = apply_inv
    retrieve(inv)
    before = sim.fingerprint("sandbox")
    apply_planted_bug_fix(project.ticket(KEY))
    classes = project.ticket(KEY) / "metadata/force-app/main/default/classes"
    (classes / "CasePriorityHelper.cls").write_text("public class CasePriorityHelper {\n}\n")
    (classes / "CasePriorityHelper.cls-meta.xml").write_text(
        (classes / "CaseTriggerHandler.cls-meta.xml").read_text())
    deployed = inv.deploy_component(
        "sandbox", KEY, ["ApexClass:CaseEscalationService", "ApexClass:CasePriorityHelper"], reason=REASON)
    rolled = inv.rollback_change(deployed["change_id"], reason="undo both")
    assert rolled["restored_components"] == ["ApexClass:CaseEscalationService"]
    assert rolled["deleted_components"] == ["ApexClass:CasePriorityHelper"]
    assert sim.fingerprint("sandbox") == before


def test_rollback_refuses_when_the_org_changed_after_the_deploy_unless_forced(apply_inv, sim, project):
    inv = apply_inv
    original = sim.source("sandbox", SERVICE_SRC).read_bytes()
    retrieve(inv)
    apply_planted_bug_fix(project.ticket(KEY))
    deployed = inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)

    org_file = sim.source("sandbox", SERVICE_SRC)
    org_file.write_text(org_file.read_text() + "// later change by someone else\n")
    later = sim.fingerprint("sandbox")
    with pytest.raises(DriftError) as err:
        inv.rollback_change(deployed["change_id"], reason="undo the fix")
    assert "later change" in err.value.details["org_changes"]
    assert sim.fingerprint("sandbox") == later
    assert inv.list_changes(KEY)["changes"][0]["status"] == "deployed"

    forced = inv.rollback_change(deployed["change_id"], reason="undo the fix anyway", force=True)
    assert forced["forced_over_drift"] and forced["verified"] is True
    assert sim.source("sandbox", SERVICE_SRC).read_bytes() == original


def test_rollback_refuses_when_the_alias_points_at_another_org(apply_inv, sim, project):
    inv = apply_inv
    retrieve(inv)
    apply_planted_bug_fix(project.ticket(KEY))
    deployed = inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)
    org_file = sim.org_dir("sandbox") / "org.json"
    org = json.loads(org_file.read_text())
    org["orgId"] = "00DWs000000OtherMAS"
    org_file.write_text(json.dumps(org))
    before = sim.fingerprint("sandbox")
    with pytest.raises(ChangeError) as err:
        inv.rollback_change(deployed["change_id"], reason="undo the fix")
    assert "now points at org" in err.value.message
    assert sim.fingerprint("sandbox") == before


@pytest.mark.parametrize("kwargs", [
    {"components": []},
    {"components": ["ApexClass:*"]},
    {"components": ["ApexClass"]},
    {"components": [f"ApexClass:C{i}" for i in range(6)]},
    {"reason": ""},
    {"reason": "  x "},
    {"test_level": "RunAllTestsInOrg"},
    {"test_level": "RunSpecifiedTests"},
    {"tests": ["CaseEscalationServiceTest"]},
    {"test_level": "RunSpecifiedTests", "tests": ["bad name"]},
    {"wait_minutes": 0},
])
def test_deploy_input_validation_happens_before_the_guard(apply_inv, sim, kwargs):
    inv = apply_inv
    inv.init_ticket(KEY)
    args = {"components": ["ApexClass:CaseEscalationService"], "reason": REASON, **kwargs}
    components = args.pop("components")
    with pytest.raises(InputValidationError):
        inv.deploy_component("sandbox", KEY, components, **args)
    assert inv.audit.entries() == []


def test_rollback_input_validation(apply_inv):
    for bad in ("", "CHG", "1; DROP TABLE changes", None):
        with pytest.raises(InputValidationError):
            apply_inv.rollback_change(bad, reason="because")
    with pytest.raises(ChangeError):
        apply_inv.rollback_change("CHG-20260101-000000-abcdef", reason="unknown change")
    with pytest.raises(InputValidationError):
        apply_inv.rollback_change("CHG-20260101-000000-abcdef", reason="")


# --- record updates ---------------------------------------------------------------------


def priorities(sim, org="sandbox"):
    return {c["Id"]: c["Priority"] for c in sim.records(org)["Case"]}


def test_update_records_before_values_verification_and_rollback(apply_inv, sim):
    inv = apply_inv
    inv.init_ticket(KEY)
    start = sim.fingerprint("sandbox")
    updates = [{"id": CASE_3, "fields": {"Priority": "High"}}, {"id": CASE_4, "fields": {"Priority": "High"}}]
    out = inv.update_records("sandbox", KEY, "Case", updates, reason="correct cases hit by the bug",
                             approved_by="tester")
    assert out["status"] == "applied" and out["verified"] is True
    assert out["records"][0] == {"id": CASE_3, "fields": {"Priority": {
        "before": "Medium", "after": "High", "matches_requested_value": True}}}
    now = priorities(sim)
    assert now[CASE_3] == now[CASE_4] == "High"

    change = inv.audit.get_change(out["change_id"])
    assert change["kind"] == "records" and change["approved_by"] == "tester"
    assert [u["before"] for u in change["detail"]["updates"]] == [{"Priority": "Medium"}, {"Priority": "Medium"}]
    listed = inv.list_changes(KEY)["changes"][0]
    assert listed["record_ids"] == [CASE_3, CASE_4] and listed["fields"] == ["Priority"]

    rolled = inv.rollback_change(out["change_id"], reason="restore the previous values")
    assert rolled["status"] == "rolled_back" and rolled["restored_record_ids"] == [CASE_3, CASE_4]
    assert sim.fingerprint("sandbox") == start
    assert outcomes(inv, "update_records") == ["succeeded", "allowed"]
    with pytest.raises(ChangeError):
        inv.rollback_change(out["change_id"], reason="only once")


def test_update_records_cap_and_validation(apply_inv, sim):
    inv = apply_inv
    inv.init_ticket(KEY)
    before = sim.fingerprint("sandbox")
    eleven = [{"id": f"500Ws00000C00{i:02d}IAA", "fields": {"Priority": "High"}} for i in range(11)]
    with pytest.raises(InputValidationError) as err:
        inv.update_records("sandbox", KEY, "Case", eleven, reason="too many records")
    assert "At most 10" in err.value.message
    bad_updates = [
        [],
        [{"id": "not-an-id", "fields": {"Priority": "High"}}],
        [{"id": CASE_3, "fields": {}}],
        [{"id": CASE_3, "fields": {"Id": "500Ws00000C0004IAA"}}],
        [{"id": CASE_3, "fields": {"Account.Name": "x"}}],
        [{"id": CASE_3, "fields": {"Priority": ["High"]}}],
        [{"id": CASE_3, "fields": {"Priority; DROP": "x"}}],
        [{"id": CASE_3, "fields": {"Priority": "High"}}, {"id": CASE_3, "fields": {"Status": "Closed"}}],
        ["nope"],
    ]
    for updates in bad_updates:
        with pytest.raises(InputValidationError):
            inv.update_records("sandbox", KEY, "Case", updates, reason="invalid input")
    with pytest.raises(InputValidationError):
        inv.update_records("sandbox", KEY, "Case", [{"id": CASE_3, "fields": {"Priority": "High"}}], reason="")
    assert inv.audit.entries() == [] and sim.fingerprint("sandbox") == before


def test_update_records_missing_record_changes_nothing(apply_inv, sim):
    inv = apply_inv
    inv.init_ticket(KEY)
    before = sim.fingerprint("sandbox")
    with pytest.raises(ChangeError) as err:
        inv.update_records("sandbox", KEY, "Case", [
            {"id": CASE_3, "fields": {"Priority": "High"}},
            {"id": "500Ws00000C9999IAA", "fields": {"Priority": "High"}}], reason="one record does not exist")
    assert "Nothing was updated" in err.value.message
    assert sim.fingerprint("sandbox") == before
    assert outcomes(inv, "update_records") == ["failed", "allowed"]


def test_update_records_partial_failure_is_recorded_and_reversible(apply_inv, sim):
    inv = apply_inv
    inv.init_ticket(KEY)
    start = sim.fingerprint("sandbox")
    with pytest.raises(ChangeError) as err:
        inv.update_records("sandbox", KEY, "Case", [
            {"id": CASE_3, "fields": {"Priority": "High"}},
            {"id": CASE_4, "fields": {"NoSuchField": "x"}},  # the org rejects this one
        ], reason="second record fails")
    assert "INVALID_FIELD" in err.value.message
    assert err.value.details["updated"] == [CASE_3]
    change_id = err.value.details["change_id"]
    assert change_id and change_id in err.value.hint
    assert priorities(sim)[CASE_3] == "High" and priorities(sim)[CASE_4] == "Medium"
    assert outcomes(inv, "update_records") == ["failed", "allowed"]
    # The record that was updated before the failure can still be restored.
    rolled = inv.rollback_change(change_id, reason="restore after the partial failure")
    assert rolled["restored_record_ids"] == [CASE_3]
    assert sim.fingerprint("sandbox") == start


def test_personal_data_in_record_updates_is_redacted_in_results(apply_inv, sim):
    inv = apply_inv
    inv.init_ticket(KEY)
    case = next(c for c in sim.records("sandbox")["Case"] if c.get("SuppliedEmail"))
    old_email = case["SuppliedEmail"]
    out = inv.update_records("sandbox", KEY, "Case", [
        {"id": case["Id"], "fields": {"SuppliedEmail": "new.person@customer.test"}}], reason="email is the bug")
    text = json.dumps(out) + json.dumps(inv.list_changes(KEY)) + json.dumps(inv.audit_entries())
    assert old_email not in text and "new.person@customer.test" not in text
    assert out["records"][0]["fields"]["SuppliedEmail"]["before"] == "[REDACTED]"
    assert out["verified"] is True
    inv.rollback_change(out["change_id"], reason="restore the email")
    assert next(c for c in sim.records("sandbox")["Case"] if c["Id"] == case["Id"])["SuppliedEmail"] == old_email


# --- anonymous Apex ---------------------------------------------------------------------


def test_anonymous_apex_allowed_in_apply(apply_inv, project):
    inv = apply_inv
    inv.init_ticket(KEY)
    out = inv.execute_anonymous_apex("sandbox", KEY, "System.debug('hello from the sandbox');",
                                     reason="read-only probe", approved_by="tester")
    assert out["success"] is True and out["compiled"] is True
    assert "USER_DEBUG" in out["log_excerpt"] and "hello from the sandbox" in out["log_excerpt"]
    assert (project.ticket(KEY) / out["script"]).read_text() == "System.debug('hello from the sandbox');"
    assert (project.ticket(KEY) / out["log"]).is_file()
    assert len(out["script_sha256"]) == 64
    assert outcomes(inv, "execute_anonymous_apex") == ["succeeded", "allowed"]


def test_anonymous_apex_compile_error_is_reported(apply_inv):
    inv = apply_inv
    inv.init_ticket(KEY)
    out = inv.execute_anonymous_apex("sandbox", KEY, "System.debug('x'); }", reason="broken script")
    assert out["success"] is False and out["compiled"] is False
    assert "Unexpected token" in out["compileProblem"] and "Compilation failed" in out["error"]
    assert outcomes(inv, "execute_anonymous_apex") == ["failed", "allowed"]


def test_anonymous_apex_refused_in_advise(inv, sim, project):
    inv.init_ticket(KEY)
    before = sim.fingerprint("sandbox")
    with pytest.raises(WriteModeDisabledError):
        inv.execute_anonymous_apex("sandbox", KEY, "System.debug('x');", reason="should not run")
    assert list((project.ticket(KEY) / "apex").iterdir()) == []
    assert sim.fingerprint("sandbox") == before
    for apex in ("", "   ", "x" * 20001, None):
        with pytest.raises(InputValidationError):
            inv.execute_anonymous_apex("sandbox", KEY, apex, reason="invalid script")


# --- every write tool, on production and in the wrong mode ------------------------------


def write_calls(inv, org, change_id="CHG-20260101-000000-abcdef"):
    return {
        "deploy_component": lambda: inv.deploy_component(org, KEY, ["ApexClass:CaseEscalationService"], reason=REASON),
        "validate_component": lambda: inv.deploy_component(org, KEY, ["ApexClass:CaseEscalationService"],
                                                           reason=REASON, validate_only=True),
        "update_records": lambda: inv.update_records(org, KEY, "Case", [{"id": CASE_3, "fields": {"Priority": "High"}}],
                                                     reason="correct the priority"),
        "execute_anonymous_apex": lambda: inv.execute_anonymous_apex(org, KEY, "System.debug('x');",
                                                                     reason="should never run"),
        "set_trace_flag": lambda: inv.set_trace_flag(org, "005Ws000001IntgIAB"),
    }


@pytest.mark.parametrize("alias", ["prod", "broken"])
@pytest.mark.parametrize("operation", ["deploy_component", "validate_component", "update_records",
                                       "execute_anonymous_apex", "set_trace_flag"])
def test_every_write_is_refused_on_production_and_the_org_is_untouched(sim, project, alias, operation):
    from core.app import Investigator
    project.write_config(mode="apply", allow_developer_edition_writes=True, allow_apex_tests_on_production=True)
    inv = Investigator(root=project.root, cli=sim.cli())
    inv.retrieve_components(alias, KEY, FIX_COMPONENTS)
    apply_planted_bug_fix(project.ticket(KEY))
    edited = (project.ticket(KEY) / SERVICE).read_text()
    before = sim.fingerprint(alias)

    with pytest.raises(ProductionWriteBlockedError) as err:
        write_calls(inv, alias)[operation]()
    assert err.value.code == "production_write_blocked"
    assert sim.fingerprint(alias) == before
    assert (project.ticket(KEY) / SERVICE).read_text() == edited
    entry = inv.audit.entries(limit=1)[0]
    assert (entry["operation"], entry["outcome"], entry["classification"]) == (operation, "blocked", "production")
    assert inv.list_changes()["changes"] == []


def test_rollback_is_refused_when_the_org_became_production(apply_inv, sim, project):
    """A change made in a sandbox cannot be 'rolled back' into an org that now classifies as production."""
    inv = apply_inv
    retrieve(inv)
    apply_planted_bug_fix(project.ticket(KEY))
    deployed = inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)
    updated = inv.update_records("sandbox", KEY, "Case", [{"id": CASE_3, "fields": {"Priority": "High"}}],
                                 reason="correct the priority")
    org_file = sim.org_dir("sandbox") / "org.json"
    org = json.loads(org_file.read_text())
    org["isSandbox"] = False
    org_file.write_text(json.dumps(org))
    before = sim.fingerprint("sandbox")
    for change_id in (deployed["change_id"], updated["change_id"]):
        with pytest.raises(ProductionWriteBlockedError):
            inv.rollback_change(change_id, reason="must be refused")
    assert sim.fingerprint("sandbox") == before


@pytest.mark.parametrize("operation", ["deploy_component", "validate_component", "update_records",
                                       "execute_anonymous_apex"])
def test_fix_applying_writes_are_refused_in_advise_mode(inv, sim, project, operation):
    inv.retrieve_components("sandbox", KEY, FIX_COMPONENTS)
    apply_planted_bug_fix(project.ticket(KEY))
    before = sim.fingerprint("sandbox")
    with pytest.raises(WriteModeDisabledError) as err:
        write_calls(inv, "sandbox")[operation]()
    assert err.value.code == "write_mode_disabled" and "sfti.config.json" in err.value.hint
    assert sim.fingerprint("sandbox") == before
    assert inv.audit.entries(limit=1)[0]["outcome"] == "blocked"


def test_developer_edition_writes_follow_the_setting(sim, project):
    from core.app import Investigator
    project.write_config(mode="apply")
    inv = Investigator(root=project.root, cli=sim.cli())
    inv.retrieve_components("dev", KEY, FIX_COMPONENTS)
    apply_planted_bug_fix(project.ticket(KEY))
    before = sim.fingerprint("dev")
    with pytest.raises(DeveloperEditionWriteBlockedError):
        inv.deploy_component("dev", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)
    assert sim.fingerprint("dev") == before
    project.write_config(mode="apply", allow_developer_edition_writes=True)
    assert inv.deploy_component("dev", KEY, ["ApexClass:CaseEscalationService"], reason=REASON)["verified"] is True


def test_audit_view_hides_stored_values(apply_inv):
    inv = apply_inv
    inv.init_ticket(KEY)
    inv.update_records("sandbox", KEY, "Case", [{"id": CASE_3, "fields": {"Subject": "sensitive subject text"}}],
                       reason="subject correction")
    text = json.dumps(inv.audit_entries(KEY)) + json.dumps(inv.list_changes(KEY))
    assert "sensitive subject text" not in text and "Demo case 3" not in text
