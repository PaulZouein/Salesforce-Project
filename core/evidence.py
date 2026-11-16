"""Index of the artifacts saved for a ticket.

Lists every saved query result, debug log, test run, anonymous Apex script and metadata
commit in ``tickets/<KEY>/`` with a one-line description and the exact string to cite, so
the report's Evidence table can be written from what actually exists on disk.

Local and read-only. Descriptions are built from already-redacted files and never include
record values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .gitrepo import GitRepo
from .workspace import Workspace

MAX_ITEMS_PER_KIND = 200


def _one_line(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _query_entry(rel: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"kind": "file", "cite": f"`{rel}`", "description": "Not a readable JSON result."}
    if "tests" in data and "query" not in data:
        failures = data.get("failures") or []
        names = ", ".join(str(n) for n in data.get("classes") or [])
        return {
            "kind": "apex_test_run",
            "cite": f"`{rel}`",
            "description": _one_line(
                f"Apex tests {names} on {data.get('org')} ({data.get('classification')}): "
                f"{data.get('outcome')}, {data.get('passing')} passing, {data.get('failing')} failing"
                + (f"; first failure: {failures[0].get('name')}" if failures else "")
            ),
        }
    if "tool" in data and "result" in data and "query" not in data:
        asked = data.get("asked") if isinstance(data.get("asked"), dict) else {}
        about = ", ".join(f"{k} {v}" for k, v in asked.items() if isinstance(v, (str, int)) and k != "limit")
        return {
            "kind": "tool_result",
            "cite": f"`{rel}`",
            "description": _one_line(f"Answer of {data.get('tool')} from {data.get('org')} at "
                                     f"{data.get('executed_at')}" + (f": {about}" if about else "")),
        }
    records = data.get("records") if isinstance(data.get("records"), list) else []
    rows = data.get("total_size") if not records and isinstance(data.get("total_size"), int) else len(records)
    return {
        "kind": "query_result",
        "cite": f"`{rel}`",
        "description": _one_line(
            f"{rows} row(s) from {data.get('org')} at {data.get('executed_at')} "
            f"({data.get('api', 'data')} API): {data.get('query')}"
        ),
        "redacted_fields": data.get("redacted_fields") or [],
    }


def _log_entry(rel: str, path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"kind": "debug_log", "cite": f"`{rel}`", "description": "Unreadable log file."}
    units = []
    for line in lines:
        if "|CODE_UNIT_STARTED|" in line:
            units.append(line.rsplit("|", 2)[-2] if line.count("|") > 3 else line.rsplit("|", 1)[-1])
        if len(units) >= 3:
            break
    errors = sum(1 for line in lines if "|FATAL_ERROR|" in line or "|EXCEPTION_THROWN|" in line)
    return {
        "kind": "debug_log",
        "cite": f"`{rel}:<line>`",
        "description": _one_line(
            f"{len(lines)} lines; code units: {', '.join(units) or 'none recognized'}; "
            f"{errors} exception or fatal-error line(s)"
        ),
    }


def evidence_index(workspace: Workspace, ticket_key: str) -> dict[str, Any]:
    """List the artifacts of a ticket that a report can cite."""
    base = workspace.ticket_dir(ticket_key).resolve()
    items: list[dict[str, Any]] = []

    def files(folder: str, pattern: str) -> list[Path]:
        root = base / folder
        return sorted(p for p in root.glob(pattern) if p.is_file())[:MAX_ITEMS_PER_KIND] if root.is_dir() else []

    for path in files("queries", "*.json"):
        rel = path.relative_to(base).as_posix()
        items.append({"path": rel, **_query_entry(rel, _load_json(path))})
    for path in files("logs", "*.log"):
        rel = path.relative_to(base).as_posix()
        items.append({"path": rel, **_log_entry(rel, path)})
    for path in files("apex", "*.apex"):
        rel = path.relative_to(base).as_posix()
        log = path.with_suffix(".log")
        items.append({
            "path": rel, "kind": "anonymous_apex", "cite": f"`{rel}`",
            "description": "Anonymous Apex script" + (f"; output in apex/{log.name}" if log.is_file() else ""),
        })

    metadata_dir = base / "metadata"
    commits: list[dict[str, str]] = []
    source_files: list[str] = []
    if (metadata_dir / ".git").exists():
        repo = GitRepo(metadata_dir)
        commits = repo.log(limit=30)
        source_files = [f"metadata/{f}" for f in repo.list_files() if f.startswith("force-app/")
                        and not f.endswith("-meta.xml") or f.endswith((".flow-meta.xml", ".field-meta.xml",
                                                                       ".validationRule-meta.xml"))]
    for name, kind, description in (
        ("fix.diff", "proposed_fix", "Proposed change as a unified diff (local; not deployed by itself)"),
        ("ticket.md", "ticket", "Structured intake of the ticket"),
    ):
        if (base / name).is_file():
            items.append({"path": name, "kind": kind, "cite": f"`{name}`", "description": description})

    by_kind: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    out: dict[str, Any] = {
        "ticket_key": ticket_key,
        "count": len(items),
        "by_kind": by_kind,
        "artifacts": items,
        "retrieved_source": {
            "files": source_files[:MAX_ITEMS_PER_KIND],
            "cite_as": "`<path>:<line>` or `<path>:<first>-<last>`; get line numbers from read_file or search_metadata",
        },
        "metadata_commits": commits,
        "how_to_use": (
            "Copy the 'cite' strings into the report's Evidence table (replace <line> with a real line "
            "number), then run validate_report: it checks that each cited file and line exists."
        ),
    }
    if not items and not source_files:
        out["note"] = "Nothing has been saved for this ticket yet. Pass ticket_key (and save_as) to soql_query, " \
                      "use get_debug_log and retrieve_components, then call this again."
    return out
