"""Heuristic scan of retrieved Apex for patterns that often cause production bugs.

The results are **leads, not findings**. A hit says "look here"; it never proves a root
cause. Each lead has a file and line so it can be read in context and then confirmed or
discarded with real evidence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .workspace import Workspace

MAX_LEADS = 300

_LOOP_RE = re.compile(r"\b(for|while)\s*\(|\bdo\s*\{")
_SOQL_RE = re.compile(r"\[\s*SELECT\b|\bDatabase\.(query|getQueryLocator|countQuery)\s*\(", re.IGNORECASE)
_DML_RE = re.compile(
    r"^\s*(insert|update|upsert|delete|undelete|merge)\s+[\w.\[\]()]+|"
    r"\bDatabase\.(insert|update|upsert|delete|undelete|merge)\s*\(",
    re.IGNORECASE,
)
_FIRST_ONLY_RE = re.compile(r"\bTrigger\.(new|old)\s*\[\s*0\s*\]|\bTrigger\.(new|old)\.get\(\s*0\s*\)", re.IGNORECASE)
_INDEX_ZERO_RE = re.compile(r"\b(\w+)\s*\[\s*0\s*\]|\b(\w+)\.get\(\s*0\s*\)")
_RETURN_RE = re.compile(r"^\s*return\s*;")
_HARDCODED_ID_RE = re.compile(r"'(00[0-9A-Za-z]|01[0-9A-Za-z]|a[0-9A-Za-z]{2}|500|701|800)[0-9A-Za-z]{12}([0-9A-Za-z]{3})?'")
_SINGLE_ROW_RE = re.compile(r"^\s*(?!List\b|Set\b|Map\b|return\b|for\b)[A-Za-z_][\w.]*\s+\w+\s*=\s*\[\s*SELECT\b", re.IGNORECASE)
_QUERY_INDEX_RE = re.compile(r"\]\s*\[\s*0\s*\]|\]\s*\.get\(\s*0\s*\)")
_CATCH_RE = re.compile(r"\bcatch\s*\(\s*[\w.]+\s+\w+\s*\)\s*\{")
_TEST_RE = re.compile(r"@isTest\b", re.IGNORECASE)
RULES = {
    "soql_in_loop": "A query inside a loop runs once per iteration and hits the 100-query limit on bulk operations.",
    "dml_in_loop": "DML inside a loop runs once per iteration and hits the 150-statement limit on bulk operations.",
    "first_record_only": "Only the first record of the trigger batch is handled; the rest are ignored in bulk operations.",
    "index_zero_in_trigger_code": "A list is read at index 0 only; check whether the other records are processed.",
    "return_inside_loop": "'return' inside a loop stops processing every remaining record; 'continue' may have been intended.",
    "hardcoded_id": "A hardcoded record Id differs between orgs and breaks after a refresh or migration.",
    "single_row_query": "Assigning a query to a single record throws QueryException when no row (or more than one) matches.",
    "query_indexed_without_check": "Indexing a query result throws ListException when the query returns no rows.",
    "swallowed_exception": "The catch block does nothing or only logs, so failures are silent and data may be left half-updated.",
}


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and string literals while keeping line and column positions."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif ch == "/" and nxt == "*":
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        elif ch == "'":
            out.append("'")
            i += 1
            while i < n and source[i] != "'":
                if source[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("'")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def scan_apex_source(source: str, path: str) -> list[dict[str, Any]]:
    """Return leads for one Apex class or trigger."""
    is_trigger = path.endswith(".trigger")
    is_test = bool(_TEST_RE.search(source))
    raw_lines = source.splitlines()
    clean_lines = strip_comments_and_strings(source).splitlines()
    leads: list[dict[str, Any]] = []

    def add(rule: str, number: int) -> None:
        if is_test and rule not in ("hardcoded_id",):
            return
        leads.append({
            "rule": rule,
            "path": path,
            "line": number,
            "code": raw_lines[number - 1].strip()[:200] if number <= len(raw_lines) else "",
            "why_it_matters": RULES[rule],
        })

    depth = 0
    loop_depths: list[int] = []
    pending_loop = False
    uses_trigger_context = is_trigger or bool(re.search(r"\bTrigger\.(new|old|newMap|oldMap)\b", source))

    for number, line in enumerate(clean_lines, start=1):
        in_loop = bool(loop_depths)
        header = _LOOP_RE.search(line)

        if _FIRST_ONLY_RE.search(line):
            add("first_record_only", number)
        elif uses_trigger_context and not is_test:
            match = _INDEX_ZERO_RE.search(line)
            name = (match.group(1) or match.group(2) or "") if match else ""
            if match and re.search(r"(new|old|record|case|account|lead|contact|opp|sobject|item|list)s?\w*$", name, re.I):
                add("index_zero_in_trigger_code", number)

        if in_loop:
            # The SOQL for-loop idiom (for (X x : [SELECT …])) is fine at the outermost level only.
            if _SOQL_RE.search(line):
                add("soql_in_loop", number)
            if _DML_RE.search(line):
                add("dml_in_loop", number)
            if _RETURN_RE.match(line):
                add("return_inside_loop", number)

        if _HARDCODED_ID_RE.search(raw_lines[number - 1] if number <= len(raw_lines) else ""):
            if not raw_lines[number - 1].lstrip().startswith(("//", "*", "/*")):
                add("hardcoded_id", number)
        if _QUERY_INDEX_RE.search(line):
            add("query_indexed_without_check", number)
        elif _SINGLE_ROW_RE.match(line):
            add("single_row_query", number)

        catch = _CATCH_RE.search(line)
        if catch:
            body: list[str] = []
            rest = line[catch.end():]
            look = number
            balance = 1
            text = rest
            while True:
                for ch in text:
                    if ch == "{":
                        balance += 1
                    elif ch == "}":
                        balance -= 1
                        if balance == 0:
                            break
                    if balance > 0:
                        body.append(ch)
                if balance == 0 or look >= len(clean_lines) or look - number > 12:
                    break
                look += 1
                text = "\n" + clean_lines[look - 1]
            statements = [s.strip() for s in "".join(body).split(";") if s.strip()]
            if balance == 0 and all(s.lower().startswith("system.debug") for s in statements):
                add("swallowed_exception", number)

        if header:
            pending_loop = True
        for ch in line:
            if ch == "{":
                depth += 1
                if pending_loop:
                    loop_depths.append(depth)
                    pending_loop = False
            elif ch == "}":
                if loop_depths and loop_depths[-1] == depth:
                    loop_depths.pop()
                depth -= 1
        if pending_loop and line.rstrip().endswith(";") and "{" not in line:
            pending_loop = False  # single-statement loop body on the same line
    return leads


def scan_workspace(workspace: Workspace, ticket_key: str, *, glob: str = "") -> dict[str, Any]:
    """Scan every retrieved Apex class and trigger in a ticket workspace."""
    from .metadata import _iter_files  # local import: metadata does not depend on this module

    base = workspace.ticket_dir(ticket_key).resolve()
    metadata_dir = base / "metadata"
    leads: list[dict[str, Any]] = []
    files = 0
    if metadata_dir.is_dir():
        for path in _iter_files(metadata_dir, glob):
            if path.suffix not in (".cls", ".trigger"):
                continue
            files += 1
            rel = path.relative_to(base).as_posix()
            leads.extend(scan_apex_source(path.read_text(encoding="utf-8", errors="replace"), rel))
    by_rule: dict[str, int] = {}
    for lead in leads:
        by_rule[lead["rule"]] = by_rule.get(lead["rule"], 0) + 1
    out: dict[str, Any] = {
        "files_scanned": files,
        "lead_count": len(leads),
        "by_rule": by_rule,
        "leads": leads[:MAX_LEADS],
        "how_to_use": (
            "These are leads from pattern matching, not findings. Read each in context with read_file and "
            "only report one as a cause when records, logs or a test confirm it."
        ),
    }
    if len(leads) > MAX_LEADS:
        out["truncated"] = True
    if not files:
        out["note"] = "No Apex classes or triggers have been retrieved for this ticket yet."
    return out
