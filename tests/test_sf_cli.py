from __future__ import annotations

import json
import os
import sys

import pytest

from core import sf_cli
from core.errors import (
    InputValidationError,
    ProductionWriteBlockedError,
    SfCliError,
    SfCliNotFoundError,
    SfCliOutputError,
    SfCliTimeoutError,
    UnsafeArgumentError,
    WritePermitError,
)
from core.sf_cli import (
    KIND_READ,
    KIND_TEST,
    KIND_WRITE,
    Permit,
    ProcessResult,
    SfCli,
    _issue_permit,
    classify_command,
    parse_json_output,
    scrub_secrets,
)

from .conftest import SIMULATED_TOKEN, RecordingRunner


def cli_with(runner, **kw):
    return SfCli(executable="sf-test", runner=runner, **kw)


# --- JSON parsing -----------------------------------------------------------------------


def test_parses_plain_envelope():
    assert parse_json_output('{"status": 0, "result": {"a": 1}}') == {"status": 0, "result": {"a": 1}}


def test_parses_envelope_after_warning_lines():
    noisy = (
        " ›   Warning: @salesforce/cli update available from 2.158.7 to 2.160.0.\n"
        "Warning: something {odd} happened\n"
        '{\n  "status": 0,\n  "result": {"records": []}\n}\n'
    )
    assert parse_json_output(noisy) == {"status": 0, "result": {"records": []}}


def test_non_json_output_is_none_and_raises_typed_error():
    assert parse_json_output("") is None
    assert parse_json_output("not json at all") is None
    assert parse_json_output("[1, 2]") is None
    runner = RecordingRunner([ProcessResult(1, "Segmentation fault", "boom")])
    with pytest.raises(SfCliOutputError) as err:
        cli_with(runner).run(["org", "list"])
    assert err.value.details["stderr"] == "boom"


def test_result_is_returned_and_json_flag_added():
    runner = RecordingRunner([{"status": 0, "result": {"x": 1}}])
    assert cli_with(runner).run(["org", "list"]) == {"x": 1}
    assert runner.calls == [["sf-test", "org", "list", "--json"]]


def test_cli_environment_disables_telemetry_and_updates():
    seen = {}

    def runner(argv, cwd, timeout, env):
        seen.update(env)
        return ProcessResult(0, '{"status":0,"result":{}}', "")

    cli_with(runner).run(["org", "list"])
    assert seen["SF_DISABLE_TELEMETRY"] == "true"
    assert seen["SF_AUTOUPDATE_DISABLE"] == "true"


# --- Errors -----------------------------------------------------------------------------


def test_timeout_injected():
    runner = RecordingRunner([TimeoutError()])
    with pytest.raises(SfCliTimeoutError) as err:
        cli_with(runner).run(["org", "list"], timeout=3)
    assert "3s" in err.value.message


def test_timeout_real_process_is_killed(sim, monkeypatch):
    monkeypatch.setenv("SFTI_SIM_DELAY", "30")
    with pytest.raises(SfCliTimeoutError):
        sim.cli().run(["org", "list"], timeout=1.0)


def test_missing_executable_on_path(monkeypatch):
    monkeypatch.delenv("SFTI_SF_EXECUTABLE", raising=False)
    monkeypatch.setattr(sf_cli.shutil, "which", lambda name: None)
    with pytest.raises(SfCliNotFoundError) as err:
        SfCli().run(["org", "list"])
    assert "npm install" in err.value.hint


def test_missing_executable_file(tmp_path):
    with pytest.raises(SfCliNotFoundError):
        SfCli(executable=str(tmp_path / "no-such-sf")).run(["org", "list"])


def test_error_envelope_is_mapped_with_hint():
    runner = RecordingRunner([{
        "name": "NamedOrgNotFoundError", "message": "No authorization information found for nope.",
        "exitCode": 1, "status": 1, "stack": "…",
    }])
    with pytest.raises(SfCliError) as err:
        cli_with(runner).run(["org", "display", "--target-org", "nope"])
    assert err.value.name == "NamedOrgNotFoundError"
    assert err.value.exit_code == 1
    assert "not authenticated" in err.value.hint
    assert err.value.to_dict()["command"] == "sf org display"


@pytest.mark.parametrize("message,fragment", [
    ("INVALID_SESSION_ID: Session expired or invalid", "expired"),
    ("MALFORMED_QUERY: unexpected token", "describe_object"),
    ("Nonexistent flag: --frobnicate", "prereqs"),
])
def test_hints(message, fragment):
    runner = RecordingRunner([{"name": "Error", "message": message, "status": 1}])
    with pytest.raises(SfCliError) as err:
        cli_with(runner).run(["org", "list"])
    assert fragment in err.value.hint


def test_named_org_not_found_through_the_simulator(sim):
    with pytest.raises(SfCliError) as err:
        sim.cli().run(["org", "display", "--target-org", "nobody"])
    assert "NamedOrgNotFound" in err.value.name


def test_accepted_non_zero_status_returns_result():
    runner = RecordingRunner([{"status": 100, "result": {"summary": {"outcome": "Failed"}}}])
    with pytest.raises(SfCliError):
        cli_with(runner).run(["org", "list"])
    assert cli_with(runner).run(["org", "list"], accept_statuses=(0, 100)) == {"summary": {"outcome": "Failed"}}


# --- Secrets ----------------------------------------------------------------------------


def test_scrub_secrets_recursive_and_case_insensitive():
    data = {"accessToken": "a", "nested": [{"RefreshToken": "b", "ok": 1}], "sfdxAuthUrl": "force://x",
            "deep": {"client_secret": "c", "password": "p", "keep": "k"}}
    assert scrub_secrets(data) == {"nested": [{"ok": 1}], "deep": {"keep": "k"}}


def test_access_token_never_leaves_the_wrapper(sim):
    cli = sim.cli()
    for args in (["org", "list"], ["org", "display", "--target-org", "prod"]):
        envelope = cli.run_envelope(args)
        assert SIMULATED_TOKEN not in json.dumps(envelope)
        assert "accessToken" not in json.dumps(envelope)


def test_access_token_absent_from_error_data():
    runner = RecordingRunner([{"name": "X", "message": "m", "status": 1, "data": {"accessToken": "SECRET"}}])
    with pytest.raises(SfCliError) as err:
        cli_with(runner).run(["org", "list"])
    assert "SECRET" not in json.dumps(err.value.to_dict())


# --- Arguments --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a&b", "a|b", "x>y", "x<y", "50%", 'say "hi"', "a^b", "wow!"])
def test_windows_metacharacters_rejected_before_running(bad):
    runner = RecordingRunner()
    with pytest.raises(UnsafeArgumentError):
        cli_with(runner, windows=True).run(["sobject", "describe", "--sobject", bad, "--target-org", "x"])
    assert runner.calls == []


def test_same_argument_is_fine_off_windows():
    runner = RecordingRunner()
    cli_with(runner, windows=False).run(["sobject", "describe", "--sobject", "a&b", "--target-org", "x"])
    assert len(runner.calls) == 1


@pytest.mark.parametrize("bad", ["line\nbreak", "nul\x00", "cr\r"])
def test_line_breaks_rejected_everywhere(bad):
    runner = RecordingRunner()
    with pytest.raises(UnsafeArgumentError):
        cli_with(runner, windows=False).run(["sobject", "describe", "--sobject", bad, "--target-org", "x"])
    assert runner.calls == []


def test_non_string_argument_rejected():
    with pytest.raises(UnsafeArgumentError):
        cli_with(RecordingRunner()).run(["data", "query", "--file", 3])  # type: ignore[list-item]


@pytest.mark.parametrize("target", ["", "-rf", "a b", "x;y", "a/b", "$(id)", "a" * 200])
def test_invalid_target_org_rejected(target):
    runner = RecordingRunner()
    with pytest.raises((InputValidationError, UnsafeArgumentError)):
        cli_with(runner).run(["org", "display", "--target-org", target])
    assert runner.calls == []


def test_empty_command_rejected():
    with pytest.raises(InputValidationError):
        cli_with(RecordingRunner()).run([])


# --- Classification ---------------------------------------------------------------------


@pytest.mark.parametrize("args,kind", [
    (["org", "list"], KIND_READ),
    (["org", "display", "--target-org", "x"], KIND_READ),
    (["data", "query", "--file", "q.soql", "--target-org", "x", "--use-tooling-api"], KIND_READ),
    (["sobject", "describe", "--sobject", "Case", "--target-org", "x"], KIND_READ),
    (["project", "retrieve", "start", "--metadata", "ApexClass:A", "--target-org", "x", "--wait", "10"], KIND_READ),
    (["apex", "get", "log", "--log-id", "07L", "--output-dir", "logs", "--target-org", "x"], KIND_READ),
    (["apex", "run", "test", "--class-names", "T", "--target-org", "x"], KIND_TEST),
    (["apex", "run", "--file", "a.apex", "--target-org", "x"], KIND_WRITE),
    (["project", "deploy", "start", "--metadata", "ApexClass:A", "--target-org", "x"], KIND_WRITE),
    (["project", "delete", "source", "--metadata", "ApexClass:A", "--target-org", "x"], KIND_WRITE),
    (["api", "request", "rest", "/services/data/v62.0/sobjects/Case/500", "--method", "PATCH"], KIND_WRITE),
    (["data", "update", "record", "--sobject", "Case"], KIND_WRITE),
    (["data", "delete", "bulk"], KIND_WRITE),
    (["org", "delete", "sandbox"], KIND_WRITE),
    (["org", "logout", "--all"], KIND_WRITE),
    (["some", "future", "command"], KIND_WRITE),
    (["data"], KIND_WRITE),
])
def test_classify_command(args, kind):
    assert classify_command(args) == kind


@pytest.mark.parametrize("args", [
    ["org", "list", "--clean"],
    ["org", "display", "--target-org", "x", "--verbose"],
    ["data", "query", "--query", "SELECT Id FROM Case", "--target-org", "x"],
    ["data", "query", "--file", "q", "--bulk"],
    ["project", "retrieve", "start", "--metadata", "A:B", "--output-dir", "/etc"],
    ["apex", "run", "test", "--class-names", "T", "--output-dir=/tmp"],
])
def test_flag_outside_the_allowlist_is_refused(args):
    runner = RecordingRunner()
    with pytest.raises(UnsafeArgumentError):
        cli_with(runner).run(args)
    assert runner.calls == []


# --- Permits ----------------------------------------------------------------------------

DEPLOY = ["project", "deploy", "start", "--metadata", "ApexClass:A", "--target-org", "sbx"]


def write_permit(target="sbx", uses=1, classification="sandbox"):
    return _issue_permit(kind=KIND_WRITE, target=target, org_id="00D", classification=classification,
                         operation="test", uses=uses)


def test_write_without_permit_never_reaches_the_runner():
    runner = RecordingRunner()
    with pytest.raises(WritePermitError):
        cli_with(runner).run(DEPLOY)
    with pytest.raises(WritePermitError):
        cli_with(runner).run(["some", "future", "command", "--target-org", "sbx"])
    with pytest.raises(WritePermitError):
        cli_with(runner).run(["apex", "run", "test", "--class-names", "T", "--target-org", "sbx"])
    assert runner.calls == []


def test_write_without_permit_never_starts_a_process(sim):
    before = sim.fingerprint("sandbox")
    with pytest.raises(WritePermitError):
        sim.cli().run(["project", "deploy", "start", "--metadata", "ApexClass:CaseTriggerHandler",
                       "--target-org", "sandbox"])
    assert sim.fingerprint("sandbox") == before


def test_permit_cannot_be_constructed_directly():
    with pytest.raises(WritePermitError):
        Permit(object(), kind=KIND_WRITE, target="sbx", org_id="00D", classification="sandbox",
               operation="x", uses=1)
    with pytest.raises(TypeError):
        Permit()  # type: ignore[call-arg]


@pytest.mark.parametrize("classification", ["production", "", "unknown", "PRODUCTION", None])
def test_write_permit_cannot_be_issued_for_production(classification):
    with pytest.raises(ProductionWriteBlockedError):
        write_permit(classification=classification)


def test_permit_use_count_is_bounded():
    for uses in (0, 21, -1):
        with pytest.raises(WritePermitError):
            write_permit(uses=uses)


def test_permit_is_bound_to_its_org():
    runner = RecordingRunner()
    with pytest.raises(WritePermitError) as err:
        cli_with(runner).run(["project", "deploy", "start", "--metadata", "ApexClass:A", "--target-org", "prod"],
                             permit=write_permit("sbx"))
    assert "sbx" in err.value.message and "prod" in err.value.message
    # A write command with no --target-org would fall back to the CLI's default org: refused too.
    with pytest.raises(WritePermitError):
        cli_with(runner).run(["project", "deploy", "start", "--metadata", "ApexClass:A"], permit=write_permit("sbx"))
    assert runner.calls == []


def test_permit_runs_out():
    runner = RecordingRunner([{"status": 0, "result": {}}])
    permit = write_permit(uses=2)
    cli = cli_with(runner)
    cli.run(DEPLOY, permit=permit)
    cli.run(DEPLOY, permit=permit)
    with pytest.raises(WritePermitError) as err:
        cli.run(DEPLOY, permit=permit)
    assert "already been used" in err.value.message
    assert len(runner.calls) == 2


def test_permit_kind_must_match():
    runner = RecordingRunner()
    test_permit = _issue_permit(kind=KIND_TEST, target="sbx", org_id="00D", classification="production",
                                operation="run_apex_tests")
    with pytest.raises(WritePermitError):
        cli_with(runner).run(DEPLOY, permit=test_permit)
    with pytest.raises(WritePermitError):
        cli_with(runner).run(["apex", "run", "test", "--class-names", "T", "--target-org", "sbx"],
                             permit=write_permit())
    with pytest.raises(WritePermitError):
        cli_with(runner).run(DEPLOY, permit="yes")  # type: ignore[arg-type]
    assert runner.calls == []


def test_tampered_permit_is_still_refused_on_production():
    runner = RecordingRunner()
    permit = write_permit()
    permit.classification = "production"
    with pytest.raises(ProductionWriteBlockedError):
        cli_with(runner).run(DEPLOY, permit=permit)
    assert runner.calls == []


def test_reads_need_no_permit_and_do_not_consume_one():
    runner = RecordingRunner()
    permit = write_permit()
    cli_with(runner).run(["org", "list"], permit=permit)
    assert permit.uses_left == 1


def test_version(sim):
    assert "simulated" in sim.cli().version()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_launcher_is_executable(sim):
    assert os.access(sim.launcher, os.X_OK)
    assert sys.executable in sim.launcher.read_text()
