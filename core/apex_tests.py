"""Run Apex test classes and return condensed results and coverage.

Running tests commits no business data, but it does enqueue jobs and create test-run
records in the org, so each run needs a test permit from ``org_guard``. On production that
permit is refused unless the user enabled it and approved the run.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Sequence

from .errors import InputValidationError
from .org_guard import OrgGuard
from .sf_cli import SfCli, validate_target_org
from .workspace import Workspace

MAX_TEST_CLASSES = 20
TEST_FAILURE_STATUS = 100

_CLASS_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}(\.[A-Za-z][A-Za-z0-9_]{0,79})?\Z")


def _condense(result: dict[str, Any], *, include_coverage: bool) -> dict[str, Any]:
    summary = result.get("summary") or {}
    tests = []
    for t in result.get("tests") or []:
        entry: dict[str, Any] = {
            "name": t.get("FullName") or f"{(t.get('ApexClass') or {}).get('Name')}.{t.get('MethodName')}",
            "outcome": t.get("Outcome"),
            "run_time_ms": t.get("RunTime"),
        }
        if t.get("Message"):
            entry["message"] = str(t.get("Message"))[:1500]
        if t.get("StackTrace"):
            entry["stack_trace"] = str(t.get("StackTrace"))[:1500]
        tests.append(entry)
    out: dict[str, Any] = {
        "outcome": summary.get("outcome"),
        "tests_ran": summary.get("testsRan"),
        "passing": summary.get("passing"),
        "failing": summary.get("failing"),
        "skipped": summary.get("skipped"),
        "test_run_id": summary.get("testRunId") or result.get("testRunId"),
        "execution_time": summary.get("testExecutionTime"),
        "tests": tests,
        "failures": [t for t in tests if t.get("outcome") not in ("Pass", "Skip")],
    }
    if include_coverage:
        classes = []
        for c in ((result.get("coverage") or {}).get("coverage")) or []:
            lines = c.get("lines") or {}
            uncovered = sorted(int(n) for n, hit in lines.items() if not hit and str(n).isdigit())
            classes.append(
                {
                    "name": c.get("name"),
                    "covered_percent": c.get("coveredPercent"),
                    "total_lines": c.get("totalLines"),
                    "uncovered_lines": uncovered[:200],
                }
            )
        out["coverage"] = classes
        if summary.get("testRunCoverage") is not None:
            out["test_run_coverage"] = summary.get("testRunCoverage")
    if not tests:
        out["note"] = (
            "No test results came back. The run may still be in progress (see test_run_id), "
            "or the class names matched no test classes."
        )
    return out


def run_apex_tests(
    cli: SfCli,
    guard: OrgGuard,
    org: str,
    class_names: Sequence[str],
    *,
    code_coverage: bool = True,
    wait_minutes: int = 10,
    workspace: Workspace | None = None,
    ticket_key: str | None = None,
    production_confirmed: bool = False,
    approved_by: str | None = None,
) -> dict[str, Any]:
    """Run the given test classes and return per-method outcomes, failures and coverage."""
    validate_target_org(org)
    if not isinstance(class_names, (list, tuple)) or not class_names:
        raise InputValidationError("At least one test class name is required.")
    if len(class_names) > MAX_TEST_CLASSES:
        raise InputValidationError(f"At most {MAX_TEST_CLASSES} test classes per run.")
    for name in class_names:
        if not isinstance(name, str) or not _CLASS_NAME_RE.match(name):
            raise InputValidationError(f"Invalid Apex class name: {name!r}.")
    if not 1 <= int(wait_minutes) <= 60:
        raise InputValidationError("wait_minutes must be between 1 and 60.")

    permit, info = guard.authorize_test_run(
        org, production_confirmed=production_confirmed, ticket_key=ticket_key, approved_by=approved_by
    )
    args = ["apex", "run", "test"]
    for name in dict.fromkeys(class_names):
        args += ["--class-names", name]
    args += ["--result-format", "json", "--wait", str(int(wait_minutes)), "--target-org", org]
    if code_coverage:
        args.append("--code-coverage")
    result = cli.run(
        args,
        permit=permit,
        timeout=int(wait_minutes) * 60 + 60,
        accept_statuses=(0, TEST_FAILURE_STATUS),
    ) or {}

    out = _condense(result, include_coverage=code_coverage)
    out["org"] = org
    out["classification"] = info.classification
    out["classes"] = list(dict.fromkeys(class_names))

    if workspace is not None and ticket_key:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = workspace.subdir(ticket_key, "queries") / f"{stamp}-apex-tests.json"
        path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        out["saved_to"] = workspace.relative(ticket_key, path)
    return out
