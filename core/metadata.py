"""Retrieve org metadata into a ticket workspace, and search or read what was retrieved.

Retrieved source lives in ``tickets/<KEY>/metadata/`` (an SFDX project with its own git
repository). Every retrieve is committed, so the history shows exactly what the org
contained at each point and any later change can be diffed against it.
"""

from __future__ import annotations

import fnmatch
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import InputValidationError, WorkspaceError
from .sf_cli import SfCli, validate_target_org
from .workspace import Workspace

MAX_COMPONENTS_PER_CALL = 50
MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
MAX_LOG_FILE_BYTES = 30 * 1024 * 1024
BASELINE_FILE = "sfti-baseline.json"

_COMPONENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(:[A-Za-z0-9_.\-* ()/$@]+)?$")

SEARCH_SCOPES = {
    "metadata": "metadata",
    "logs": "logs",
    "queries": "queries",
    "apex": "apex",
    "all": "",
}


def validate_component(spec: str) -> str:
    """Validate a ``Type:Name`` component reference (``Type:*`` and ``Type`` are allowed)."""
    if not isinstance(spec, str) or not _COMPONENT_RE.match(spec) or len(spec) > 200:
        raise InputValidationError(
            f"Invalid component reference: {spec!r}.",
            hint="Use Type:Name, for example ApexClass:CaseService, Flow:Case_Routing, "
            "ValidationRule:Case.Require_Reason, or ApexTrigger:* for a whole type.",
        )
    return spec


def validate_components(components: Sequence[str], *, maximum: int = MAX_COMPONENTS_PER_CALL) -> list[str]:
    if not isinstance(components, (list, tuple)) or not components:
        raise InputValidationError("At least one component is required.")
    if len(components) > maximum:
        raise InputValidationError(f"At most {maximum} components per call; got {len(components)}.")
    seen: list[str] = []
    for spec in components:
        validate_component(spec)
        if spec not in seen:
            seen.append(spec)
    return seen


# --- Baselines --------------------------------------------------------------------------
#
# For each file, the commit at which the file was last known to equal the org's version.
# Stored inside .git so it is never part of the tracked tree.


def _baseline_path(repo_path: Path) -> Path:
    return repo_path / ".git" / BASELINE_FILE


def load_baselines(repo_path: Path) -> dict[str, str]:
    path = _baseline_path(repo_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_baselines(repo_path: Path, baselines: dict[str, str]) -> None:
    _baseline_path(repo_path).write_text(json.dumps(baselines, indent=2, sort_keys=True), encoding="utf-8")


def set_baselines(repo_path: Path, files: Iterable[str], commit: str) -> None:
    baselines = load_baselines(repo_path)
    for f in files:
        baselines[f] = commit
    save_baselines(repo_path, baselines)


# --- Retrieve ---------------------------------------------------------------------------


def _relative_to_repo(repo_path: Path, file_path: str) -> str | None:
    try:
        path = Path(file_path)
        if not path.is_absolute():
            path = repo_path / path
        return path.resolve().relative_to(repo_path.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def run_retrieve(cli: SfCli, org: str, repo_path: Path, components: Sequence[str], wait_minutes: int) -> dict[str, Any]:
    """Run ``sf project retrieve start`` in ``repo_path`` and normalize the result."""
    args = ["project", "retrieve", "start"]
    for spec in components:
        args += ["--metadata", spec]
    args += ["--target-org", org, "--wait", str(wait_minutes), "--ignore-conflicts"]
    envelope = cli.run_envelope(args, cwd=repo_path, timeout=wait_minutes * 60 + 60)
    result = envelope.get("result") or {}
    files: list[dict[str, Any]] = []
    for f in result.get("files") or []:
        if not isinstance(f, dict):
            continue
        rel = _relative_to_repo(repo_path, f["filePath"]) if f.get("filePath") else None
        files.append(
            {
                "type": f.get("type"),
                "name": f.get("fullName"),
                "state": f.get("state"),
                "path": rel,
                **({"error": f.get("error")} if f.get("error") else {}),
            }
        )
    problems = [
        str(m.get("problem"))
        for m in (result.get("messages") or [])
        if isinstance(m, dict) and m.get("problem")
    ]
    warnings = [str(w) for w in (envelope.get("warnings") or [])]
    return {"files": files, "problems": problems, "warnings": warnings, "status": result.get("status")}


def retrieve_components(
    cli: SfCli,
    workspace: Workspace,
    org: str,
    ticket_key: str,
    components: Sequence[str],
    *,
    wait_minutes: int = 10,
    api_version: str | None = None,
) -> dict[str, Any]:
    """Retrieve components into the ticket's metadata project and commit them as a baseline."""
    validate_target_org(org)
    specs = validate_components(components)
    if not 1 <= int(wait_minutes) <= 60:
        raise InputValidationError("wait_minutes must be between 1 and 60.")
    workspace.ticket_dir(ticket_key, create=True)
    repo = workspace.metadata_repo(ticket_key, api_version=api_version)

    saved_local = None
    if not repo.is_clean():
        # Never lose local edits: a retrieve overwrites files with the org's version.
        saved_local = repo.commit_all("local: uncommitted edits saved before retrieve")

    retrieved = run_retrieve(cli, org, repo.path, specs, int(wait_minutes))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit = repo.commit_all(f"retrieve: {', '.join(specs)[:150]} from {org} at {stamp}")
    head = repo.head()
    paths = [f["path"] for f in retrieved["files"] if f.get("path") and f.get("state") != "Failed"]
    if head:
        set_baselines(repo.path, paths, head)

    out: dict[str, Any] = {
        "ticket_key": ticket_key,
        "org": org,
        "requested": specs,
        "retrieved_count": len(paths),
        "files": [
            {**f, "path": f"metadata/{f['path']}" if f.get("path") else None} for f in retrieved["files"]
        ][:300],
        "commit": commit or head,
        "changed_since_last_retrieve": commit is not None,
    }
    if len(retrieved["files"]) > 300:
        out["files_truncated"] = len(retrieved["files"])
    if retrieved["problems"]:
        out["problems"] = retrieved["problems"]
    if retrieved["warnings"]:
        out["warnings"] = retrieved["warnings"]
    if not paths:
        out["note"] = (
            "Nothing was retrieved. Check the type and name spelling; a component that does not exist "
            "in the org is reported under problems or warnings."
        )
    if saved_local:
        out["local_edits_saved_in_commit"] = saved_local
        out["local_edits_note"] = (
            "You had uncommitted local edits. They were committed before the retrieve; files that were "
            "retrieved now show the org's version."
        )
    return out


# --- Search and read --------------------------------------------------------------------


def _iter_files(base: Path, glob: str) -> Iterable[Path]:
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part in (".git", ".sf", ".sfdx", "node_modules") for part in rel.parts):
            continue
        rel_posix = rel.as_posix()
        if glob and not (fnmatch.fnmatch(rel_posix, glob) or fnmatch.fnmatch(path.name, glob)):
            continue
        yield path


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(2048)
    except OSError:
        return True


def search_metadata(
    workspace: Workspace,
    ticket_key: str,
    pattern: str,
    *,
    glob: str = "",
    scope: str = "metadata",
    ignore_case: bool = True,
    max_results: int = 100,
    context_lines: int = 0,
) -> dict[str, Any]:
    """Regex search over files in the ticket workspace. Returns ``path:line`` matches."""
    if scope not in SEARCH_SCOPES:
        raise InputValidationError(f"Unknown scope {scope!r}.", hint=f"Choose from {sorted(SEARCH_SCOPES)}.")
    if not isinstance(pattern, str) or not pattern:
        raise InputValidationError("A search pattern is required.")
    if len(pattern) > 500:
        raise InputValidationError("The search pattern is too long (500 characters at most).")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise InputValidationError(f"Invalid regular expression: {exc}.") from None
    max_results = max(1, min(int(max_results), 500))
    context_lines = max(0, min(int(context_lines), 5))

    ticket_dir = workspace.ticket_dir(ticket_key).resolve()
    base = (ticket_dir / SEARCH_SCOPES[scope]) if SEARCH_SCOPES[scope] else ticket_dir
    if not base.is_dir():
        return {"pattern": pattern, "scope": scope, "matches": [], "files_searched": 0,
                "note": f"tickets/{ticket_key}/{SEARCH_SCOPES[scope]} does not exist yet."}

    matches: list[dict[str, Any]] = []
    files_searched = 0
    skipped: list[str] = []
    truncated = False
    for path in _iter_files(base, glob):
        size = path.stat().st_size
        limit = MAX_LOG_FILE_BYTES if path.suffix == ".log" else MAX_SEARCH_FILE_BYTES
        rel = path.relative_to(ticket_dir).as_posix()
        if size > limit or _is_binary(path):
            skipped.append(rel)
            continue
        files_searched += 1
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            skipped.append(rel)
            continue
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            match: dict[str, Any] = {"path": rel, "line": index + 1, "text": line.strip()[:300]}
            if context_lines:
                start, end = max(0, index - context_lines), min(len(lines), index + context_lines + 1)
                match["context"] = [f"{n + 1}: {lines[n][:300]}" for n in range(start, end)]
            matches.append(match)
            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break

    out: dict[str, Any] = {
        "pattern": pattern,
        "scope": scope,
        "files_searched": files_searched,
        "match_count": len(matches),
        "matches": matches,
    }
    if truncated:
        out["truncated"] = True
        out["note"] = f"Stopped at {max_results} matches. Narrow the pattern or use glob."
    if skipped:
        out["skipped_files"] = skipped[:20]
    if not matches and not truncated:
        out["note"] = (
            "No matches in what has been retrieved so far. This only covers retrieved files: "
            "a component that was never retrieved cannot match."
        )
    return out


def read_file(
    workspace: Workspace,
    ticket_key: str,
    path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = 400,
) -> dict[str, Any]:
    """Read a file from the ticket workspace with line numbers."""
    resolved = workspace.resolve(ticket_key, path)
    if not resolved.is_file():
        raise WorkspaceError(f"{path!r} is not a file.")
    if _is_binary(resolved):
        raise WorkspaceError(f"{path!r} looks like a binary file.", hint="Images can be opened with the Read tool.")
    if resolved.stat().st_size > MAX_LOG_FILE_BYTES:
        raise WorkspaceError(f"{path!r} is too large to read through this tool.")
    lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(start_line))
    max_lines = max(1, min(int(max_lines), 2000))
    last = len(lines) if end_line is None else min(len(lines), int(end_line))
    last = min(last, start + max_lines - 1)
    numbered = [f"{n}: {lines[n - 1]}" for n in range(start, last + 1)]
    out: dict[str, Any] = {
        "path": workspace.relative(ticket_key, resolved),
        "total_lines": len(lines),
        "start_line": start,
        "end_line": last if numbered else start - 1,
        "content": "\n".join(numbered),
    }
    if last < len(lines) and (end_line is None or int(end_line) > last):
        out["note"] = f"Showing lines {start}-{last} of {len(lines)}. Pass start_line to continue."
    return out


def list_files(workspace: Workspace, ticket_key: str, *, scope: str = "metadata", glob: str = "") -> dict[str, Any]:
    """List files in a ticket workspace folder."""
    if scope not in SEARCH_SCOPES:
        raise InputValidationError(f"Unknown scope {scope!r}.", hint=f"Choose from {sorted(SEARCH_SCOPES)}.")
    ticket_dir = workspace.ticket_dir(ticket_key).resolve()
    base = (ticket_dir / SEARCH_SCOPES[scope]) if SEARCH_SCOPES[scope] else ticket_dir
    files = [p.relative_to(ticket_dir).as_posix() for p in _iter_files(base, glob)] if base.is_dir() else []
    return {"scope": scope, "count": len(files), "files": files[:500], **({"truncated": True} if len(files) > 500 else {})}
