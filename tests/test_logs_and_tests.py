from __future__ import annotations

import json

import pytest

from core.errors import (
    DeveloperEditionWriteBlockedError,
    InputValidationError,
    ProductionTestRunBlockedError,
    ProductionWriteBlockedError,
    WorkspaceError,
)

LOG_ID = "07LWs00000L0001MAA"
INTEGRATION_USER = "005Ws000001IntgIAB"


# --- debug logs -------------------------------------------------------------------------


def test_list_debug_logs_returns_ids_not_names(inv, sim):
    out = inv.list_debug_logs("prod")
    assert out["count"] == 1
    log = out["logs"][0]
    assert log["Id"] == LOG_ID and log["LogUserId"] == INTEGRATION_USER
    text = json.dumps(out)
    assert not any(u["Name"] in text or u["Username"] in text for u in sim.records("prod")["User"])


def test_list_debug_logs_filters(inv):
    assert inv.list_debug_logs("prod", user_id=INTEGRATION_USER, since="2026-11-11")["count"] == 1
    assert inv.list_debug_logs("prod", since="2026-11-12T00:00:00Z")["count"] == 0
    assert inv.list_debug_logs("prod", operation_contains="composite")["count"] == 1
    empty = inv.list_debug_logs("prod", status_not_success=True)
    assert empty["count"] == 0 and "trace flag" in empty["note"]
    for kwargs in ({"user_id": "001notauser"}, {"since": "yesterday"}, {"operation_contains": "a%b"}):
        with pytest.raises(InputValidationError):
            inv.list_debug_logs("prod", **kwargs)


def test_get_debug_log_saves_file_and_returns_excerpt(inv, project):
    out = inv.get_debug_log("prod", LOG_ID, "T-1", max_lines=5)
    assert out["saved_to"] == f"logs/{LOG_ID}.log" and out["downloaded_now"] is True
    saved = project.ticket("T-1") / "logs" / f"{LOG_ID}.log"
    assert saved.is_file() and out["total_lines"] == 56 == len(saved.read_text().splitlines())
    assert out["excerpt"].splitlines()[0].startswith("1: 62.0 APEX_CODE,FINEST")
    assert len(out["excerpt"].splitlines()) == 5 and "Showing lines 1-5 of 56" in out["note"]
    assert out["summary"]["event_counts"]["CODE_UNIT_STARTED"] == 2
    again = inv.get_debug_log("prod", LOG_ID, "T-1", start_line=55)
    assert again["downloaded_now"] is False and again["end_line"] == 56


def test_get_debug_log_grep_with_context(inv):
    out = inv.get_debug_log("prod", LOG_ID, "T-1", grep=r"STATEMENT_EXECUTE\|\[(30|32)\]", context_lines=1)
    assert out["matching_lines"] == 1
    assert [l.split(":")[0] for l in out["excerpt"].splitlines()] == ["23", "24", "25"]
    assert "STATEMENT_EXECUTE|[30]" in out["excerpt"]
    rules = inv.get_debug_log("prod", LOG_ID, "T-1", grep="FLOW_RULE_DETAIL")
    assert rules["matching_lines"] == 2 and "High_Priority|false" in rules["excerpt"]
    with pytest.raises(InputValidationError):
        inv.get_debug_log("prod", LOG_ID, "T-1", grep="(")


def test_get_debug_log_missing_or_invalid(inv):
    with pytest.raises(WorkspaceError) as err:
        inv.get_debug_log("prod", "07LWs00000L9999MAA", "T-1")
    assert "list_debug_logs" in err.value.hint
    for bad in ("../../etc/passwd", "500Ws00000C0001IAA", "07L", ""):
        with pytest.raises(InputValidationError):
            inv.get_debug_log("prod", bad, "T-1")


def test_analyze_sample_log(inv):
    saved = inv.get_debug_log("prod", LOG_ID, "T-1")["saved_to"]
    out = inv.analyze_debug_log("T-1", saved)
    assert out["path"] == saved
    assert out["triggers"] == [{"trigger": "CaseTrigger", "object": "Case", "event": "BeforeInsert", "line": 4}]
    assert [(f["flow"], f["line"]) for f in out["flows"]] == [
        ("Case_Premium_Notification", 44), ("Case_Premium_Notification", 49)]
    # Trigger first, then the after-save flow.
    assert [u["name"] for u in out["code_units"]] == ["CaseTrigger on Case trigger event BeforeInsert", "Flow:Case"]
    assert out["code_units"][0]["line"] < out["code_units"][1]["line"]
    assert [(d["outcome"], d["result"], d["line"]) for d in out["flow_decisions"]] == [
        ("High_Priority", False, 46), ("High_Priority", False, 51)]
    service = next(u for u in out["apex_statements"]["by_method"] if u["in"].startswith("CaseEscalationService."))
    assert service["source_lines"][-1] == 30
    assert 32 not in service["source_lines"]
    assert out["dml"]["statements"] == [
        {"line": 40, "source_line": None, "operation": "Insert", "object": "Case", "rows": 2}]
    assert out["soql"]["queries"][0]["source_line"] == 23 and out["soql"]["queries"][0]["rows"] == 2
    assert out["limits"]["SOQL queries"] == {"used": 1, "limit": 100}
    assert out["exceptions"] == [] and out["fatal_errors"] == []
    # The raw log agrees: statement 30 ran, statement 32 never did.
    text = inv.read_file("T-1", saved)["content"]
    assert "STATEMENT_EXECUTE|[30]" in text and "STATEMENT_EXECUTE|[32]" not in text


def test_analyze_log_units():
    from core.log_analysis import analyze_log
    log = "\n".join([
        "62.0 APEX_CODE,ERROR;WORKFLOW,INFO",
        "10:00:00.0 (100)|CODE_UNIT_STARTED|[EXTERNAL]|01q|AccTrigger on Account trigger event AfterUpdate|__sfdc_trigger/AccTrigger",
        *[f"10:00:00.0 ({200 + i})|SOQL_EXECUTE_BEGIN|[12]|Aggregations:0|SELECT Id FROM Contact" for i in range(6)],
        "10:00:00.0 (300)|VALIDATION_RULE|03d000000000001|Require_Reason",
        "10:00:00.0 (301)|VALIDATION_FORMULA|ISBLANK(Reason)|Reason=null",
        "10:00:00.0 (302)|VALIDATION_FAIL",
        "10:00:00.0 (400)|EXCEPTION_THROWN|[17]|System.NullPointerException: Attempt to de-reference a null object",
        "10:00:00.0 (500)|FATAL_ERROR|System.NullPointerException: Attempt to de-reference a null object",
        "10:00:00.0 (600)|CODE_UNIT_FINISHED|AccTrigger on Account trigger event AfterUpdate",
        "10:00:00.0 (700)|LIMIT_USAGE_FOR_NS|(default)|",
        "  Number of SOQL queries: 95 out of 100",
    ])
    out = analyze_log(log)
    joined = " ".join(out["observations"])
    assert "ran 6 times" in joined and "Require_Reason failed" in joined and "Fatal error" in joined
    assert "Apex source line 17" in joined and "'SOQL queries' reached 95 of 100" in joined
    assert out["validation_rules"][0]["outcome"] == "fail"
    assert out["exceptions"][0]["in"].startswith("AccTrigger")
    assert "debug_level_warning" in out
    assert out["apex_statements"]["recorded"] is False
    assert "No log events" in analyze_log("hello\nworld")["note"]


# --- trace flags ------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["prod", "broken"])
def test_trace_flag_refused_on_production_without_any_write(inv, sim, project, alias):
    project.write_config(mode="apply", allow_developer_edition_writes=True)
    before = sim.fingerprint(alias)
    with pytest.raises(ProductionWriteBlockedError):
        inv.set_trace_flag(alias, INTEGRATION_USER)
    assert sim.fingerprint(alias) == before
    assert inv.audit.entries(limit=1)[0]["outcome"] == "blocked"


def test_trace_flag_refused_without_any_write_command_sent(project, recording_runner):
    """Same refusal with an injected runner: prove that only read commands were issued."""
    from core.app import Investigator
    from core.sf_cli import SfCli, classify_command
    runner = recording_runner([
        {"status": 0, "result": {"id": "00DPROD", "username": "u@example.com", "apiVersion": "62.0"}},
        {"status": 0, "result": {"records": [{"Id": "00DPROD", "IsSandbox": False,
                                              "OrganizationType": "Enterprise Edition"}], "totalSize": 1}},
    ])
    inv = Investigator(root=project.root, cli=SfCli(executable="sf-test", runner=runner))
    with pytest.raises(ProductionWriteBlockedError):
        inv.set_trace_flag("prod", INTEGRATION_USER)
    assert len(runner.calls) == 2
    assert all(classify_command([a for a in call[1:] if a != "--json"]) == "read" for call in runner.calls)


def test_trace_flag_on_sandbox_in_advise_mode(inv, sim):
    assert inv.get_config()["mode"] == "advise"
    first = inv.set_trace_flag("sandbox", INTEGRATION_USER, minutes=45, ticket_key="T-1", approved_by="tester")
    assert first["action"] == "created" and first["debug_level_created"] is True
    assert first["user_id"] == INTEGRATION_USER and first["classification"] == "sandbox"
    records = sim.records("sandbox")
    assert len(records["DebugLevel"]) == 1 and records["DebugLevel"][0]["ApexCode"] == "FINEST"
    assert len(records["TraceFlag"]) == 1
    flag = records["TraceFlag"][0]
    assert flag["TracedEntityId"] == INTEGRATION_USER and flag["LogType"] == "USER_DEBUG"
    assert flag["DebugLevelId"] == records["DebugLevel"][0]["Id"]
    first_expiry = flag["ExpirationDate"]

    second = inv.set_trace_flag("sandbox", INTEGRATION_USER, minutes=600)
    assert second["action"] == "extended" and second["debug_level_created"] is False
    assert second["trace_flag_id"] == first["trace_flag_id"]
    records = sim.records("sandbox")
    assert len(records["DebugLevel"]) == 1 and len(records["TraceFlag"]) == 1
    assert records["TraceFlag"][0]["ExpirationDate"] > first_expiry

    rows = inv.audit.entries(limit=10)
    assert [r["outcome"] for r in rows if r["operation"] == "set_trace_flag"] == [
        "succeeded", "allowed", "succeeded", "allowed"]
    assert rows[-1]["approved_by"] == "tester"


def test_trace_flag_by_username_and_validation(inv, sim):
    out = inv.set_trace_flag("sandbox", "integration@demo.example.com")
    assert out["user_id"] == INTEGRATION_USER
    before = sim.fingerprint("sandbox")
    with pytest.raises(InputValidationError):
        inv.set_trace_flag("sandbox", "nobody@nowhere.test")
    assert inv.audit.entries(limit=1)[0]["outcome"] == "failed"
    for minutes in (0, 1441, True, "30"):
        with pytest.raises(InputValidationError):
            inv.set_trace_flag("sandbox", INTEGRATION_USER, minutes=minutes)
    assert sim.fingerprint("sandbox") == before


def test_trace_flag_on_developer_edition_needs_the_setting(inv, project):
    with pytest.raises(DeveloperEditionWriteBlockedError):
        inv.set_trace_flag("dev", INTEGRATION_USER)
    project.write_config(allow_developer_edition_writes=True)
    assert inv.set_trace_flag("dev", INTEGRATION_USER)["action"] == "created"


# --- Apex tests -------------------------------------------------------------------------


def test_apex_tests_pass_on_sandbox_and_are_saved(inv, project):
    out = inv.run_apex_tests("sandbox", ["CaseEscalationServiceTest"], ticket_key="T-1")
    assert (out["outcome"], out["tests_ran"], out["passing"], out["failing"]) == ("Passed", 2, 2, 0)
    assert {t["name"] for t in out["tests"]} == {
        "CaseEscalationServiceTest.premiumAccountCaseIsHighPriority",
        "CaseEscalationServiceTest.standardAccountCaseKeepsPriority"}
    assert out["failures"] == [] and out["classification"] == "sandbox"
    assert out["coverage"][0]["name"] == "CaseEscalationService"
    saved = json.loads((project.ticket("T-1") / out["saved_to"]).read_text())
    assert saved["passing"] == 2 and out["saved_to"].endswith("-apex-tests.json")


def test_failing_apex_test_is_reported_not_raised(inv, sim):
    test_class = sim.source("sandbox", "classes/CaseEscalationServiceTest.cls")
    text = test_class.read_text().rstrip()[:-1]
    test_class.write_text(text + "\n    @isTest\n    static void mixedBatchEscalatesEveryPremiumCase() {\n    }\n}\n")
    out = inv.run_apex_tests("sandbox", ["CaseEscalationServiceTest"], code_coverage=False)
    assert (out["outcome"], out["passing"], out["failing"]) == ("Failed", 2, 1)
    failure = out["failures"][0]
    assert failure["name"].endswith(".mixedBatchEscalatesEveryPremiumCase")
    assert "Expected: High, Actual: Medium" in failure["message"] and "line 58" in failure["stack_trace"]
    assert "coverage" not in out


@pytest.mark.parametrize("alias", ["prod", "broken"])
def test_apex_tests_refused_on_production(inv, sim, alias):
    before = sim.fingerprint(alias)
    with pytest.raises(ProductionTestRunBlockedError) as err:
        inv.run_apex_tests(alias, ["CaseEscalationServiceTest"])
    assert "allow_apex_tests_on_production" in err.value.hint
    with pytest.raises(ProductionTestRunBlockedError):
        inv.run_apex_tests(alias, ["CaseEscalationServiceTest"], production_confirmed=True)
    assert sim.fingerprint(alias) == before


def test_apex_tests_on_production_need_setting_and_confirmation(inv, project):
    project.write_config(allow_apex_tests_on_production=True)
    with pytest.raises(ProductionTestRunBlockedError) as err:
        inv.run_apex_tests("prod", ["CaseEscalationServiceTest"])
    assert "run_apex_tests_on_production" in err.value.message
    out = inv.run_apex_tests("prod", ["CaseEscalationServiceTest"], production_confirmed=True, approved_by="tester")
    assert out["outcome"] == "Passed" and out["classification"] == "production"
    allowed = inv.audit.entries(limit=1)[0]
    assert allowed["outcome"] == "allowed" and allowed["detail"]["production_confirmed"] is True
    # The opt-in covers test runs only: writes stay refused.
    project.write_config(allow_apex_tests_on_production=True, mode="apply")
    with pytest.raises(ProductionWriteBlockedError):
        inv.set_trace_flag("prod", INTEGRATION_USER)


def test_apex_test_input_validation(inv):
    for names in ([], None, ["Bad Name"], ["a;b"], ["T"] * 21, "CaseEscalationServiceTest"):
        with pytest.raises(InputValidationError):
            inv.run_apex_tests("sandbox", names)
    with pytest.raises(InputValidationError):
        inv.run_apex_tests("sandbox", ["T"], wait_minutes=0)
