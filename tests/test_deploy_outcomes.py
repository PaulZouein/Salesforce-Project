"""Deploys and retrieves that do not end cleanly, answered the way the real Salesforce CLI answers them.

The simulator only ever succeeds or fails outright. These tests run it through a runner that
replaces chosen commands with the CLI's own answers (plugin-deploy-retrieve 2.158.7):

- a retrieve that failed in the org exits 0 with ``--json`` and ``{status: 'Failed', success: false}``;
- a deploy that outlives ``--wait`` ends with ``ClientTimeoutError`` while it carries on in the org;
- a deploy reported while still running exits 69 with ``{status: 'InProgress'}``.
"""

from __future__ import annotations

import json

import pytest

from core.app import Investigator
from core.errors import ChangeError, DeployFailedError, DeployInProgressError, SfCliError
from core.sf_cli import ProcessResult, default_runner

from .conftest import FIX_COMPONENTS, apply_planted_bug_fix

KEY = "T-9"
REASON = "T-9: escalate every Premium case in a mixed batch"
SERVICE_SRC = "classes/CaseEscalationService.cls"

FAILED_RETRIEVE = {"status": 0, "result": {
    "status": "Failed", "success": False, "done": True, "files": [], "messages": [],
    "errorStatusCode": "UNKNOWN_EXCEPTION", "errorMessage": "An unexpected error occurred (ErrorId 1234-5678)."}}
CLIENT_TIMEOUT = {"status": 1, "exitCode": 1, "name": "ClientTimeoutError", "context": "DeployMetadata",
                  "message": "The command has timed out, although the deployment is still running. Use "
                             "\"sf project deploy resume\" to resume watching the deployment."}
STILL_RUNNING = {"status": 69, "result": {"status": "InProgress", "success": False, "done": False,
                                          "id": "0AfWs00000Dep001AAA", "files": []}}


class Interceptor:
    """Runs the simulator, except for the commands a test answers itself."""

    def __init__(self):
        self.rules: list[list] = []
        self.calls: list[str] = []

    def answer(self, words: str, envelope: dict, *, run_first: bool = False, skip: int = 0) -> None:
        """Answer the next command containing ``words`` (after ``skip`` of them); ``run_first`` lets the
        simulator do it first."""
        self.rules.append([words, envelope, run_first, skip])

    def count(self, words: str) -> int:
        return sum(1 for line in self.calls if words in line)

    def __call__(self, argv, cwd, timeout, env, **kw):
        line = " ".join(argv)
        self.calls.append(line)
        for rule in self.rules:
            words, envelope, run_first, skip = rule
            if words in line:
                if skip:
                    rule[3] -= 1
                    break
                self.rules.remove(rule)
                if run_first:
                    default_runner(argv, cwd, timeout, env, **kw)
                return ProcessResult(int(envelope["status"]), json.dumps(envelope), "")
        return default_runner(argv, cwd, timeout, env, **kw)


@pytest.fixture
def setup(sim, project):
    project.write_config(mode="apply")
    runner = Interceptor()
    inv = Investigator(root=project.root, cli=sim.cli(runner=runner))
    inv.retrieve_components("sandbox", KEY, FIX_COMPONENTS)
    apply_planted_bug_fix(project.ticket(KEY))
    return inv, runner


def statuses(inv):
    return [c["status"] for c in inv.list_changes(KEY)["changes"]]


def test_a_failed_retrieve_is_an_error_and_not_an_empty_result(setup):
    inv, runner = setup
    runner.answer("retrieve start", FAILED_RETRIEVE)
    with pytest.raises(SfCliError) as caught:
        inv.retrieve_components("sandbox", KEY, ["ApexClass:CaseTriggerHandler"])
    assert caught.value.name == "RetrieveFailed"
    assert "UNKNOWN_EXCEPTION" in caught.value.message and "unexpected error" in caught.value.message


def test_a_deploy_stops_when_the_snapshot_cannot_be_taken(setup, sim):
    inv, runner = setup
    before = sim.fingerprint("sandbox")
    runner.answer("retrieve start", FAILED_RETRIEVE)
    with pytest.raises(DeployFailedError) as caught:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    assert "Nothing was deployed" in caught.value.message
    assert runner.count("deploy start") == 0 and sim.fingerprint("sandbox") == before


def test_a_deploy_that_outlives_its_wait_is_in_progress_and_rolls_back_once_finished(setup, sim):
    inv, runner = setup
    original = sim.source("sandbox", SERVICE_SRC).read_bytes()
    runner.answer("deploy start", CLIENT_TIMEOUT, run_first=True)  # the deploy finishes in the org regardless
    with pytest.raises(DeployInProgressError) as caught:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    change_id = caught.value.details["change_id"]
    assert statuses(inv) == ["in_progress"] and b"continue;" in sim.source("sandbox", SERVICE_SRC).read_bytes()
    assert "in_progress" in [e["outcome"] for e in inv.audit.entries(limit=50) if e["operation"] == "deploy_component"]

    undone = inv.rollback_change(change_id, reason="T-9: undo the deploy that outlived its wait")
    assert undone["status"] == "rolled_back" and undone["verified"] is True
    assert sim.source("sandbox", SERVICE_SRC).read_bytes() == original
    assert statuses(inv) == ["rolled_back"]


def test_rolling_back_a_deploy_that_has_not_changed_the_org_does_nothing(setup, sim):
    inv, runner = setup
    runner.answer("deploy start", CLIENT_TIMEOUT)  # still running: the org has not changed yet
    with pytest.raises(DeployInProgressError) as caught:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    before, deploys = sim.fingerprint("sandbox"), runner.count("deploy start")
    with pytest.raises(ChangeError) as refused:
        inv.rollback_change(caught.value.details["change_id"], reason="T-9: undo the deploy")
    assert "still holds the version from before" in refused.value.message
    assert statuses(inv) == ["in_progress"]  # it can be rolled back once it has finished
    assert runner.count("deploy start") == deploys and sim.fingerprint("sandbox") == before


def test_a_deploy_reported_as_still_running_keeps_its_deploy_id(setup):
    inv, runner = setup
    runner.answer("deploy start", STILL_RUNNING)
    with pytest.raises(DeployInProgressError) as caught:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    assert caught.value.details["deploy_id"] == "0AfWs00000Dep001AAA" and statuses(inv) == ["in_progress"]


def test_a_validation_that_outlives_its_wait_did_not_change_the_org(setup, sim):
    inv, runner = setup
    runner.answer("deploy start", CLIENT_TIMEOUT)
    with pytest.raises(DeployFailedError) as caught:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON, validate_only=True)
    assert "validation did not complete" in caught.value.message and statuses(inv) == ["failed"]


def test_a_change_without_recorded_file_paths_is_not_rolled_back(setup):
    """Restoring no file and redeploying the working tree would deploy the fix again and call it a rollback."""
    inv, runner = setup
    inv.audit.save_change(
        change_id="CHG-20261119-nopath", kind="deploy", ticket_key=KEY, org_target="sandbox", org_id="00DWs0000000001",
        status="deployed", pre_commit="a" * 40, proposed_commit="b" * 40, post_commit="c" * 40,
        detail={"deployed": [{"type": "ApexClass", "name": "CaseEscalationService", "state": "Changed",
                              "path": "force-app/main/default/classes/CaseEscalationService.cls"}],
                "modified_paths": []})
    calls = len(runner.calls)
    with pytest.raises(ChangeError) as refused:
        inv.rollback_change("CHG-20261119-nopath", reason="T-9: undo an old change")
    assert "recorded no file paths" in refused.value.message and len(runner.calls) == calls


def test_a_deploy_whose_read_back_fails_is_recorded_and_can_be_rolled_back(setup, sim):
    """The deploy succeeded; only the verify retrieve failed. It used to leave the org changed with no record."""
    inv, runner = setup
    original = sim.source("sandbox", SERVICE_SRC).read_bytes()
    runner.answer("retrieve start", FAILED_RETRIEVE, skip=1)  # the snapshot works, the read-back fails
    out = inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    assert out["status"] == "deployed" and out["verified"] is False and "reading the components back failed" in \
        out["verify_note"]
    assert statuses(inv) == ["deployed"] and b"continue;" in sim.source("sandbox", SERVICE_SRC).read_bytes()
    undone = inv.rollback_change(out["change_id"], reason="T-9: undo the deploy that was never read back")
    assert undone["status"] == "rolled_back" and sim.source("sandbox", SERVICE_SRC).read_bytes() == original


def test_a_refused_rollback_leaves_the_edit_in_the_working_tree(setup, project):
    inv, runner = setup
    runner.answer("deploy start", CLIENT_TIMEOUT)
    with pytest.raises(DeployInProgressError) as caught:
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    with pytest.raises(ChangeError):
        inv.rollback_change(caught.value.details["change_id"], reason="T-9: undo the deploy")
    edited = project.ticket(KEY) / "metadata" / "force-app" / "main" / "default" / SERVICE_SRC
    assert "continue;" in edited.read_text(encoding="utf-8")  # the retrieve no longer leaves the org's version


def test_a_rollback_that_stopped_half_way_can_be_retried(setup, sim):
    """The restore landed, then reading it back failed: retrying is not 'drift'."""
    inv, runner = setup
    original = sim.source("sandbox", SERVICE_SRC).read_bytes()
    change_id = inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)["change_id"]
    runner.answer("retrieve start", FAILED_RETRIEVE, skip=1)  # the rollback's own read-back fails
    with pytest.raises(ChangeError) as stopped:
        inv.rollback_change(change_id, reason="T-9: undo the deploy")
    assert "can be called again" in (stopped.value.hint or "") and statuses(inv) == ["rollback_failed"]
    assert sim.source("sandbox", SERVICE_SRC).read_bytes() == original  # the restore itself landed
    again = inv.rollback_change(change_id, reason="T-9: finish undoing the deploy")
    assert again["status"] == "rolled_back" and again["verified"] is True


def test_another_deploy_still_running_is_a_failure_not_an_unfinished_deploy(setup):
    """Only the CLI's own timeout means "this deploy is still running"; this deploy never started."""
    inv, runner = setup
    runner.answer("deploy start", {"status": 1, "exitCode": 1, "name": "DeployFailed",
                                   "message": "Another deployment is still running in this org. Try again later."})
    with pytest.raises(DeployFailedError):
        inv.deploy_component("sandbox", KEY, FIX_COMPONENTS, reason=REASON)
    assert statuses(inv) == ["failed"]
