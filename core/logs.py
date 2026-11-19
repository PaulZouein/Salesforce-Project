"""Apex debug logs: list, fetch, excerpt, and trace flags.

Listing and fetching are reads. Creating or extending a trace flag writes ``TraceFlag`` and
``DebugLevel`` records in the org, so it goes through ``org_guard`` and is refused on
production.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .audit import OUTCOME_FAILED, OUTCOME_SUCCEEDED, AuditLog
from .errors import InputValidationError, SfCliError, SfInvestigatorError, WorkspaceError
from .org_guard import OrgGuard
from .query import run_raw_query, soql_literal, strip_attributes
from .sf_cli import Permit, SfCli, validate_target_org
from .workspace import Workspace

DEBUG_LEVEL_NAME = "SFTI_Investigation"
DEFAULT_API_VERSION = "62.0"
MAX_TRACE_MINUTES = 24 * 60

_LOG_ID_RE = re.compile(r"^07L[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")
_USER_ID_RE = re.compile(r"^005[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?)?$")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
#: What Salesforce writes into a log that reached its size limit (20 MB): lines were removed, from anywhere.
TRUNCATION_MARKER = "MAXIMUM DEBUG LOG SIZE REACHED"
TRUNCATION_WARNING = (
    "This log reached Salesforce's size limit and lines were removed from it, anywhere in the log. A line that "
    "is not in it may still have run: do not conclude from absence. Narrow the trace (a class-level trace "
    "flag, or lower log levels) and capture the transaction again."
)


def mask_emails(text: str) -> str:
    """E-mail addresses, and usernames (which look like them), masked in text shown from a log."""
    return _EMAIL_RE.sub("[email]", text)


def is_truncated(lines: list[str]) -> bool:
    return any(TRUNCATION_MARKER in line for line in lines)

#: Log events worth pointing out in a summary.
NOTABLE_EVENTS = (
    "FATAL_ERROR",
    "EXCEPTION_THROWN",
    "VALIDATION_FAIL",
    "FLOW_ELEMENT_ERROR",
    "FLOW_ELEMENT_FAULT",
    "DML_BEGIN",
    "SOQL_EXECUTE_BEGIN",
    "CODE_UNIT_STARTED",
    "USER_DEBUG",
    "LIMIT_USAGE_FOR_NS",
    "WF_RULE_EVAL_VALUE",
    "FLOW_START_INTERVIEW_BEGIN",
)
_ERROR_EVENTS = ("FATAL_ERROR", "EXCEPTION_THROWN", "VALIDATION_FAIL", "FLOW_ELEMENT_ERROR", "FLOW_ELEMENT_FAULT")


def validate_log_id(log_id: str) -> str:
    if not isinstance(log_id, str) or not _LOG_ID_RE.match(log_id):
        raise InputValidationError(f"Invalid debug log Id: {log_id!r}.", hint="Log Ids start with 07L.")
    return log_id


def _soql_datetime(value: str) -> str:
    if not isinstance(value, str) or not _ISO_RE.match(value):
        raise InputValidationError(
            f"Invalid date/time: {value!r}.", hint="Use ISO 8601, for example 2026-03-01T08:00:00Z."
        )
    if "T" not in value:
        return value + "T00:00:00Z"
    # SOQL wants YYYY-MM-DDThh:mm:ss and a zone: "2026-11-10T08:00" alone was sent as-is and refused by the org.
    date, time, seconds, _fraction, zone = re.match(
        r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?$", value).groups()
    if zone and zone != "Z" and ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    return f"{date}T{time}{seconds or ':00'}{zone or 'Z'}"


def list_debug_logs(
    cli: SfCli,
    org: str,
    *,
    user_id: str | None = None,
    since: str | None = None,
    operation_contains: str | None = None,
    status_not_success: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent debug logs, newest first. Users are identified by Id, not by name."""
    validate_target_org(org)
    limit = max(1, min(int(limit), 100))
    where: list[str] = []
    if user_id:
        if not _USER_ID_RE.match(user_id):
            raise InputValidationError(f"Invalid user Id: {user_id!r}.", hint="User Ids start with 005.")
        where.append(f"LogUserId = {soql_literal(user_id)}")
    if since:
        where.append(f"StartTime >= {_soql_datetime(since)}")
    if operation_contains:
        if len(operation_contains) > 80 or "%" in operation_contains or "_" in operation_contains:
            raise InputValidationError("operation_contains must be plain text without % or _.")
        where.append(f"Operation LIKE {soql_literal('%' + operation_contains + '%')}")
    if status_not_success:
        where.append("Status != 'Success'")
    query = (
        "SELECT Id, LogUserId, Operation, Request, Status, LogLength, DurationMilliseconds, StartTime, "
        "Location, Application FROM ApexLog"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY StartTime DESC LIMIT {limit}"
    )
    records = strip_attributes(run_raw_query(cli, org, query, tooling=True))
    out: dict[str, Any] = {"count": len(records), "logs": records}
    if not records:
        out["note"] = (
            "No debug logs matched. Logs only exist while a trace flag is active for the user, "
            "and Salesforce deletes them after 24 hours (or when storage is full)."
        )
    return out


# --- Fetch and excerpt ------------------------------------------------------------------


def summarize_log(lines: list[str]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    errors: list[str] = []
    for index, line in enumerate(lines):
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        event = parts[1]
        if event in NOTABLE_EVENTS:
            counts[event] = counts.get(event, 0) + 1
        if event in _ERROR_EVENTS and len(errors) < 25:
            errors.append(f"{index + 1}: {line[:400]}")
    return {"event_counts": counts, "error_lines": errors}


def get_debug_log(
    cli: SfCli,
    workspace: Workspace,
    org: str,
    log_id: str,
    ticket_key: str,
    *,
    grep: str | None = None,
    start_line: int = 1,
    max_lines: int = 200,
    context_lines: int = 0,
) -> dict[str, Any]:
    """Download a debug log into ``tickets/<KEY>/logs/`` and return a summary and an excerpt."""
    validate_target_org(org)
    validate_log_id(log_id)
    logs_dir = workspace.subdir(ticket_key, "logs")
    path = logs_dir / f"{log_id}.log"
    downloaded = False
    if not path.exists():
        ticket_dir = workspace.ticket_dir(ticket_key)
        try:
            result = cli.run(
                ["apex", "get", "log", "--log-id", log_id, "--output-dir", "logs", "--target-org", org],
                cwd=ticket_dir,
                timeout=300,
            )
        except SfCliError as exc:
            if re.search(r"NOT_FOUND|404|does not exist|not found", f"{exc.name} {exc.message}", re.IGNORECASE):
                raise WorkspaceError(
                    f"The log {log_id} is no longer in the org.",
                    hint="Debug logs are deleted after a while. Run list_debug_logs to see what is available.",
                ) from exc
            raise
        downloaded = True
        if not path.exists():
            # The CLI writes <dir>/<id>.log. What --json returns with --output-dir differs between versions:
            # the log's text (2.158.7, plugin-apex get/log.js), file paths, or {"log": "..."} objects.
            text = None
            for item in result or []:
                if isinstance(item, dict) and isinstance(item.get("log"), str):
                    text = item["log"]
                elif isinstance(item, str) and ("\n" in item or len(item) > 400 or not item.endswith(".log")):
                    text = item                                   # the log itself, not a path
                elif isinstance(item, str):
                    candidate = Path(item)
                    candidate = candidate if candidate.is_absolute() else ticket_dir / candidate
                    if candidate.is_file():
                        text = candidate.read_text(encoding="utf-8", errors="replace")
                        if candidate.resolve() != path.resolve() and logs_dir.resolve() in candidate.resolve().parents:
                            candidate.unlink()
            if text is None:
                raise WorkspaceError(
                    f"The log {log_id} was not returned by the org.",
                    hint="It may have been deleted. Run list_debug_logs to see what is available.",
                )
            path.write_text(text, encoding="utf-8")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    max_lines = max(1, min(int(max_lines), 1000))
    context_lines = max(0, min(int(context_lines), 5))
    out: dict[str, Any] = {
        "log_id": log_id,
        "saved_to": workspace.relative(ticket_key, path),
        "downloaded_now": downloaded,
        "total_lines": len(lines),
        "size_bytes": path.stat().st_size,
        "summary": summarize_log(lines),
    }
    if is_truncated(lines):
        out["truncated"] = True
        out["warning"] = TRUNCATION_WARNING

    if grep:
        try:
            regex = re.compile(grep, re.IGNORECASE)
        except re.error as exc:
            raise InputValidationError(f"Invalid regular expression: {exc}.") from None
        excerpt: list[str] = []
        shown: set[int] = set()
        matched = 0
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            matched += 1
            if len(excerpt) >= max_lines:
                continue
            for n in range(max(0, index - context_lines), min(len(lines), index + context_lines + 1)):
                if n not in shown:
                    shown.add(n)
                    excerpt.append(f"{n + 1}: {mask_emails(lines[n][:500])}")
        out["grep"] = grep
        out["matching_lines"] = matched
        out["excerpt"] = "\n".join(excerpt[:max_lines])
        if matched > max_lines:
            out["note"] = f"{matched} lines matched; showing the first {max_lines}."
    else:
        start = max(1, int(start_line))
        end = min(len(lines), start + max_lines - 1)
        out["start_line"] = start
        out["end_line"] = end
        out["excerpt"] = "\n".join(f"{n}: {mask_emails(lines[n - 1][:500])}" for n in range(start, end + 1))
        if end < len(lines):
            out["note"] = (
                f"Showing lines {start}-{end} of {len(lines)}. Use grep, or pass start_line, "
                "or search the saved file with search_metadata(scope='logs')."
            )
    return out


# --- REST helper (writes) ---------------------------------------------------------------


def rest_request(
    cli: SfCli,
    org: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    permit: Permit,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Send one REST request through ``sf api request rest``. The body travels in a file.

    Returns ``{"status_code": int, "body": ...}`` and raises ``SfCliError`` on HTTP >= 400.
    """
    if method not in ("POST", "PATCH", "DELETE"):
        raise InputValidationError(f"Unsupported method {method!r}.")
    if not re.match(r"^/services/data/v\d{2,3}\.0/[A-Za-z0-9_/.\-]+$", path):
        raise InputValidationError(f"Invalid REST path: {path!r}.")
    with tempfile.TemporaryDirectory(prefix="sfti-rest-") as tmp:
        args = ["api", "request", "rest", path, "--method", method, "--target-org", org]
        if body is not None:
            (Path(tmp) / "body.json").write_text(json.dumps(body), encoding="utf-8")
            args += ["--body", "@body.json"]
        result = cli.run(args, permit=permit, cwd=tmp, timeout=timeout, accept_statuses=(0, 1)) or {}
    status_code = result.get("statusCode") if isinstance(result, dict) else None
    response_body = result.get("body") if isinstance(result, dict) else None
    if not isinstance(status_code, int) or status_code >= 400 or status_code == 0:
        message = "The request failed."
        if isinstance(response_body, list) and response_body and isinstance(response_body[0], dict):
            first = response_body[0]
            message = f"{first.get('errorCode', 'ERROR')}: {first.get('message', '')}".strip()
        elif isinstance(response_body, str) and response_body:
            message = response_body[:300]
        raise SfCliError(
            message,
            name="RestRequestFailed",
            exit_code=status_code if isinstance(status_code, int) else None,
            command=f"sf api request rest {method}",
            data=response_body,
        )
    return {"status_code": status_code, "body": response_body}


def api_version_of(info_api_version: str | None) -> str:
    if info_api_version and re.match(r"^\d{2,3}\.0$", info_api_version):
        return info_api_version
    return DEFAULT_API_VERSION


def _sf_datetime(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


# --- Trace flags (write) ----------------------------------------------------------------


def set_trace_flag(
    cli: SfCli,
    guard: OrgGuard,
    org: str,
    user: str,
    *,
    minutes: int = 30,
    ticket_key: str | None = None,
    approved_by: str | None = None,
    audit: AuditLog | None = None,
) -> dict[str, Any]:
    """Create or extend a USER_DEBUG trace flag for ``user`` (a user Id or a username)."""
    validate_target_org(org)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_TRACE_MINUTES:
        raise InputValidationError(f"minutes must be between 1 and {MAX_TRACE_MINUTES}.")
    if not isinstance(user, str) or not user or len(user) > 120:
        raise InputValidationError("user must be a user Id (005…) or a username.")

    # Up to three write commands: debug level, then create or update the trace flag.
    permit, info = guard.authorize_write(
        org, "set_trace_flag", commands=3, diagnostic=True, ticket_key=ticket_key, approved_by=approved_by
    )
    version = api_version_of(info.api_version)
    base = f"/services/data/v{version}/tooling/sobjects"
    try:
        if _USER_ID_RE.match(user):
            user_id = user
        else:
            rows = run_raw_query(cli, org, f"SELECT Id FROM User WHERE Username = {soql_literal(user)} LIMIT 1")
            if not rows:
                raise InputValidationError(f"No user with username {user!r} in {org}.")
            user_id = rows[0]["Id"]

        levels = run_raw_query(
            cli,
            org,
            f"SELECT Id FROM DebugLevel WHERE DeveloperName = {soql_literal(DEBUG_LEVEL_NAME)} LIMIT 1",
            tooling=True,
        )
        created_level = False
        if levels:
            level_id = levels[0]["Id"]
        else:
            created = rest_request(
                cli,
                org,
                "POST",
                f"{base}/DebugLevel",
                {
                    "DeveloperName": DEBUG_LEVEL_NAME,
                    "MasterLabel": DEBUG_LEVEL_NAME,
                    "ApexCode": "FINEST",
                    "ApexProfiling": "INFO",
                    "Callout": "INFO",
                    "Database": "INFO",
                    "System": "DEBUG",
                    "Validation": "INFO",
                    "Visualforce": "INFO",
                    "Workflow": "FINER",
                },
                permit,
            )
            level_id = (created.get("body") or {}).get("id")
            created_level = True
            if not level_id:
                raise SfCliError("The org did not return an Id for the new debug level.", name="DebugLevelCreateFailed")

        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=minutes)
        existing = run_raw_query(
            cli,
            org,
            "SELECT Id, ExpirationDate FROM TraceFlag "
            f"WHERE TracedEntityId = {soql_literal(user_id)} AND LogType = 'USER_DEBUG' LIMIT 1",
            tooling=True,
        )
        if existing:
            flag_id = existing[0]["Id"]
            rest_request(
                cli,
                org,
                "PATCH",
                f"{base}/TraceFlag/{flag_id}",
                {"StartDate": _sf_datetime(now), "ExpirationDate": _sf_datetime(expires), "DebugLevelId": level_id},
                permit,
            )
            action = "extended"
        else:
            created = rest_request(
                cli,
                org,
                "POST",
                f"{base}/TraceFlag",
                {
                    "TracedEntityId": user_id,
                    "LogType": "USER_DEBUG",
                    "DebugLevelId": level_id,
                    "StartDate": _sf_datetime(now),
                    "ExpirationDate": _sf_datetime(expires),
                },
                permit,
            )
            flag_id = (created.get("body") or {}).get("id")
            action = "created"
    except SfInvestigatorError as exc:
        if audit:
            audit.record("set_trace_flag", OUTCOME_FAILED, org_target=org, org_id=info.org_id,
                         classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                         detail={"error": exc.message})
        raise

    out = {
        "action": action,
        "trace_flag_id": flag_id,
        "user_id": user_id,
        "debug_level": DEBUG_LEVEL_NAME,
        "debug_level_created": created_level,
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "org": org,
        "classification": info.classification,
        "next_step": "Reproduce the issue as that user, then call list_debug_logs with user_id.",
    }
    if audit:
        audit.record("set_trace_flag", OUTCOME_SUCCEEDED, org_target=org, org_id=info.org_id,
                     classification=info.classification, ticket_key=ticket_key, approved_by=approved_by, detail=out)
    return out
