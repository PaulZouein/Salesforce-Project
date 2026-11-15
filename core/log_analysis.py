"""Structured analysis of an Apex debug log.

Turns a raw log into the things an investigator looks for first: which code units ran and
in what order, which triggers, flows, validation rules and workflow rules fired, where
exceptions were thrown, what DML and SOQL ran (and whether a query repeats suspiciously),
how close the transaction came to governor limits, which Apex source lines executed (when
the log was captured at FINEST, so a line that never ran can be shown not to have run), and
how each flow decision evaluated.

Every item carries the log line number so it can be cited as evidence.
"""

from __future__ import annotations

import re
from typing import Any

MAX_ITEMS = 200

_LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d+)\s*\((\d+)\)\|([A-Z_]+)\|?(.*)$")
_SOURCE_LINE_RE = re.compile(r"^\[(\d+|EXTERNAL)\]")
_LIMIT_RE = re.compile(r"^\s*(?:Number of |Maximum )?(.+?):\s*(\d+) out of (\d+)")
_TRIGGER_RE = re.compile(r"(\w+) on (\w+) trigger event (\w+)")


def _source_line(details: str) -> int | None:
    match = _SOURCE_LINE_RE.match(details)
    if match and match.group(1).isdigit():
        return int(match.group(1))
    return None


def _cap(items: list[Any]) -> list[Any]:
    return items[:MAX_ITEMS]


def analyze_log(text: str) -> dict[str, Any]:
    """Analyze the text of a debug log."""
    lines = text.splitlines()
    code_units: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    triggers: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []
    flow_errors: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    workflow: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    fatal: list[dict[str, Any]] = []
    dml: list[dict[str, Any]] = []
    soql: list[dict[str, Any]] = []
    user_debug: list[dict[str, Any]] = []
    limits: dict[str, dict[str, int]] = {}
    soql_by_source: dict[int, int] = {}
    dml_by_source: dict[int, int] = {}
    statements: dict[int, dict[str, int]] = {}
    statement_units: list[dict[str, Any]] = []
    flow_decisions: list[dict[str, Any]] = []
    method_stack: list[str] = []
    pending_rule: dict[str, Any] | None = None
    pending_wf: dict[str, Any] | None = None
    pending_soql: dict[str, Any] | None = None
    in_limits = False
    event_total = 0

    for number, raw in enumerate(lines, start=1):
        match = _LINE_RE.match(raw)
        if not match:
            if in_limits:
                limit = _LIMIT_RE.match(raw)
                if limit:
                    limits[limit.group(1).strip()] = {"used": int(limit.group(2)), "limit": int(limit.group(3))}
            continue
        event_total += 1
        _, nanos, event, details = match.groups()
        in_limits = event in ("LIMIT_USAGE_FOR_NS", "CUMULATIVE_LIMIT_USAGE")
        parts = details.split("|")

        if event == "CODE_UNIT_STARTED":
            name = parts[-1] if parts else details
            label = parts[2] if len(parts) > 2 and not parts[2].startswith("__sfdc") else name
            if len(parts) > 1 and "trigger event" in details:
                label = next((p for p in parts if "trigger event" in p), label)
            unit = {"name": label, "depth": len(stack), "line": number, "start_ns": int(nanos)}
            stack.append(unit)
            code_units.append(unit)
            trig = _TRIGGER_RE.search(details)
            if trig:
                triggers.append({"trigger": trig.group(1), "object": trig.group(2), "event": trig.group(3),
                                 "line": number})
        elif event == "CODE_UNIT_FINISHED":
            if stack:
                unit = stack.pop()
                unit["end_line"] = number
                unit["duration_ms"] = round((int(nanos) - unit.pop("start_ns")) / 1_000_000, 1)
        elif event in ("METHOD_ENTRY", "CONSTRUCTOR_ENTRY"):
            method_stack.append(parts[-1] if parts else details)
        elif event in ("METHOD_EXIT", "CONSTRUCTOR_EXIT"):
            if method_stack:
                method_stack.pop()
        elif event == "STATEMENT_EXECUTE":
            source = _source_line(details)
            if source is not None:
                unit_name = method_stack[-1] if method_stack else (stack[-1]["name"] if stack else "(unknown)")
                if not statement_units or statement_units[-1]["in"] != unit_name:
                    statement_units.append({"in": unit_name, "first_log_line": number, "source_lines": []})
                statement_units[-1]["source_lines"].append(source)
                seen = statements.setdefault(source, {"count": 0, "first_log_line": number})
                seen["count"] += 1
        elif event == "FLOW_RULE_DETAIL":
            # interview id | rule (outcome) name | true/false | condition logic
            flow_decisions.append({
                "line": number,
                "interview": parts[0] if parts else None,
                "outcome": parts[1] if len(parts) > 1 else None,
                "result": (parts[2].strip().lower() == "true") if len(parts) > 2 else None,
            })
        elif event == "EXCEPTION_THROWN":
            exceptions.append({"line": number, "source_line": _source_line(details),
                               "message": "|".join(parts[1:])[:500] if len(parts) > 1 else details[:500],
                               "in": stack[-1]["name"] if stack else None})
        elif event == "FATAL_ERROR":
            fatal.append({"line": number, "message": details[:800]})
        elif event == "VALIDATION_RULE":
            pending_rule = {"rule": parts[-1] if parts else details, "line": number}
        elif event == "VALIDATION_FORMULA" and pending_rule is not None:
            pending_rule["formula"] = parts[0][:300] if parts else ""
        elif event in ("VALIDATION_PASS", "VALIDATION_FAIL"):
            entry = pending_rule or {"rule": "(unnamed)", "line": number}
            entry["outcome"] = "fail" if event == "VALIDATION_FAIL" else "pass"
            entry["result_line"] = number
            validations.append(entry)
            pending_rule = None
        elif event == "FLOW_START_INTERVIEW_BEGIN":
            flows.append({"flow": parts[-1] if parts else details, "line": number})
        elif event in ("FLOW_ELEMENT_ERROR", "FLOW_ELEMENT_FAULT"):
            flow_errors.append({"line": number, "message": details[:500]})
        elif event == "WF_CRITERIA_BEGIN":
            pending_wf = {"rule": parts[1] if len(parts) > 1 else details[:200], "line": number}
        elif event == "WF_CRITERIA_END" and pending_wf is not None:
            pending_wf["criteria_met"] = details.strip().lower() == "true"
            workflow.append(pending_wf)
            pending_wf = None
        elif event == "DML_BEGIN":
            source = _source_line(details)
            info = {p.split(":", 1)[0]: p.split(":", 1)[1] for p in parts if ":" in p}
            rows = info.get("Rows", "")
            dml.append({"line": number, "source_line": source, "operation": info.get("Op"),
                        "object": info.get("Type"), "rows": int(rows) if rows.isdigit() else None})
            if source is not None:
                dml_by_source[source] = dml_by_source.get(source, 0) + 1
        elif event == "SOQL_EXECUTE_BEGIN":
            source = _source_line(details)
            pending_soql = {"line": number, "source_line": source, "query": (parts[-1] if parts else "")[:300]}
            soql.append(pending_soql)
            if source is not None:
                soql_by_source[source] = soql_by_source.get(source, 0) + 1
        elif event == "SOQL_EXECUTE_END" and pending_soql is not None:
            rows = next((p.split(":", 1)[1] for p in parts if p.startswith("Rows:")), "")
            pending_soql["rows"] = int(rows) if rows.isdigit() else None
            pending_soql = None
        elif event == "USER_DEBUG":
            user_debug.append({"line": number, "source_line": _source_line(details),
                               "message": "|".join(parts[2:])[:500] if len(parts) > 2 else details[:500]})

    for unit in code_units:
        unit.pop("start_ns", None)

    observations: list[str] = []
    for item in fatal[:5]:
        observations.append(f"Fatal error at log line {item['line']}: {item['message'][:200]}")
    for item in exceptions[:5]:
        where = f" (Apex source line {item['source_line']})" if item.get("source_line") else ""
        observations.append(f"Exception thrown at log line {item['line']}{where}: {item['message'][:200]}")
    for item in validations:
        if item.get("outcome") == "fail":
            observations.append(f"Validation rule {item['rule']} failed at log line {item['result_line']}.")
    for item in flow_errors[:5]:
        observations.append(f"Flow element error at log line {item['line']}: {item['message'][:200]}")
    for source, count in sorted(soql_by_source.items(), key=lambda kv: -kv[1]):
        if count >= 5:
            observations.append(
                f"The SOQL query at Apex source line {source} ran {count} times in this transaction "
                "(possible query inside a loop)."
            )
    for source, count in sorted(dml_by_source.items(), key=lambda kv: -kv[1]):
        if count >= 5:
            observations.append(
                f"The DML statement at Apex source line {source} ran {count} times in this transaction "
                "(possible DML inside a loop)."
            )
    near_limits = {}
    for name, value in limits.items():
        if value["limit"] and value["used"] / value["limit"] >= 0.8:
            near_limits[name] = value
            observations.append(f"Governor limit '{name}' reached {value['used']} of {value['limit']}.")
    repeated = {}
    for trig in triggers:
        key = f"{trig['trigger']} {trig['event']}"
        repeated[key] = repeated.get(key, 0) + 1
    for key, count in repeated.items():
        if count >= 3:
            observations.append(f"{key} ran {count} times in this transaction (possible recursion).")

    header = lines[0][:200] if lines else ""
    out: dict[str, Any] = {
        "header": header,
        "total_lines": len(lines),
        "events_parsed": event_total,
        "observations": observations,
        "triggers": _cap(triggers),
        "flows": _cap(flows),
        "flow_decisions": _cap(flow_decisions),
        "flow_errors": _cap(flow_errors),
        "validation_rules": _cap(validations),
        "workflow_rules": _cap(workflow),
        "exceptions": _cap(exceptions),
        "fatal_errors": _cap(fatal),
        "dml": {"count": len(dml), "statements": _cap(dml)},
        "soql": {"count": len(soql), "queries": _cap(soql)},
        "user_debug": {"count": len(user_debug), "messages": user_debug[:60]},
        "limits": limits,
        "limits_at_or_above_80_percent": near_limits,
        "code_units": _cap(code_units),
        "apex_statements": {
            "recorded": bool(statements),
            "by_method": [
                {**unit, "source_lines": unit["source_lines"][:MAX_ITEMS]} for unit in _cap(statement_units)
            ],
            "note": (
                "Source lines are per class or trigger: read them together with 'in'. A line that is absent "
                "did not execute in this transaction; that only means something when the log level for Apex "
                "code was FINEST (see the header)."
                if statements else
                "No STATEMENT_EXECUTE events: the Apex code log level was below FINEST, so this log cannot "
                "show which lines ran."
            ),
        },
    }
    if event_total == 0:
        out["note"] = "No log events were recognized. Is this an Apex debug log?"
    elif not observations:
        out["note"] = (
            "Nothing stood out automatically. That is not proof the transaction was healthy: check that "
            "the log covers the failing action and that the debug level was high enough."
        )
    if "APEX_CODE,NONE" in header or "APEX_CODE,ERROR" in header:
        out["debug_level_warning"] = "Apex logging was off or errors-only in this log; method-level detail is missing."
    return out
