"""Tickets as the interface sees them: created from pasted text, with a status read from the files.

The status of a ticket is derived from what is on disk in ``tickets/<KEY>/``, not from what the
interface remembers. It is therefore the same whether the investigation was started from the
interface, from Claude Code, or is still running.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.app import Investigator
from core.errors import InputValidationError, SfInvestigatorError, WorkspaceError
from core.report import _find_section, _sections, markdown_to_html
from core.workspace import validate_ticket_key

MAX_TICKET_CHARS = 200_000
ORIGINAL_NAME = "ticket-original.txt"
REQUEST_NAME = "request.json"

_KEY_LINE_RE = re.compile(r"^\s*(?:key|ticket|issue)\s*[:#]\s*([A-Za-z][A-Za-z0-9_]{0,19}-\d{1,9})\b", re.I | re.M)
_KEY_ANY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,19}-\d{1,9})\b")
_TITLE_LINE_RE = re.compile(r"^\s*(?:title|summary|subject)\s*:\s*(.+)$", re.I | re.M)
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_key(text: str) -> str | None:
    """The ticket key in pasted text: a ``Key:`` line first, otherwise the first thing shaped like one."""
    match = _KEY_LINE_RE.search(text) or _KEY_ANY_RE.search(text)
    return match.group(1).upper() if match else None


def detect_title(text: str) -> str | None:
    match = _TITLE_LINE_RE.search(text)
    if match:
        return _EMAIL_RE.sub("[email]", match.group(1).strip())[:160]
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 12 and not re.match(r"^[\w ]{1,20}:", line):
            return _EMAIL_RE.sub("[email]", line)[:160]
    return None


def create_ticket(inv: Investigator, text: str, org: str, *, key: str | None = None,
                  sandbox: str | None = None) -> dict[str, Any]:
    """Create ``tickets/<KEY>/`` from pasted ticket text and record which org to investigate."""
    if not isinstance(text, str) or len(text.strip()) < 40:
        raise InputValidationError("Paste the whole ticket: description, comments and history.")
    if len(text) > MAX_TICKET_CHARS:
        raise InputValidationError(f"The ticket text is too long ({MAX_TICKET_CHARS:,} characters at most).")
    key = (key or "").strip() or detect_key(text)
    if not key:
        raise InputValidationError(
            "No ticket key was found in the text.",
            hint="Type the key in the field next to the text, for example ABC-123.")
    key = validate_ticket_key(key.upper() if re.match(r"^[A-Za-z][A-Za-z0-9_]*-\d+$", key) else key)
    if not isinstance(org, str) or not org.strip():
        raise InputValidationError("Choose the org to investigate.")
    folder = inv.workspace.tickets_root / key
    if (folder / ORIGINAL_NAME).exists() or (folder / "attachments" / ORIGINAL_NAME).exists():
        raise WorkspaceError(
            f"Ticket {key} already exists.",
            hint="Open it from the ticket list, or delete it there first if you want to start again.")
    inv.init_ticket(key)
    attachments = folder / "attachments"
    (attachments / ORIGINAL_NAME).write_text(text, encoding="utf-8")
    request = {"key": key, "title": detect_title(text), "org": org.strip(), "sandbox": (sandbox or "").strip() or None,
               "created_at": utc_now(), "source": "web"}
    (folder / REQUEST_NAME).write_text(json.dumps(request, indent=2), encoding="utf-8")
    return request


def read_request(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / REQUEST_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def agent_prompt(request: dict[str, Any]) -> str:
    """The instruction given to Claude Code for a ticket submitted through the interface."""
    key = request["key"]
    sandbox = request.get("sandbox")
    lines = [
        f"A new ticket, {key}, was submitted through the web interface.",
        f"Its full original text is in tickets/{key}/attachments/{ORIGINAL_NAME}. Read all of it, comments included.",
        f"Org to investigate: {request['org']}." + (f" Non-production org available for tests: {sandbox}." if sandbox else ""),
        "",
        "Follow CLAUDE.md and the skills, in this order: intake-ticket, investigate-ticket, propose-fix, "
        "write-ticket-comment.",
        "",
        "You are running unattended, so:",
        "- Do not ask questions. Record every ambiguity in ticket.md, state the assumption you proceed on, "
        "and list what you would have asked under Open questions in the report.",
        "- Stay in advise mode whatever the config says: hand the fix over as a diff. Write tools need an "
        "approval nobody can give here and will be refused; do not attempt them.",
        "- Start with recall_org_knowledge for this org, and before you finish, use remember_org_fact for "
        "what you learned about how the org is built.",
        "- Finish only when report.md passes validate_report, report.html is exported, fix.diff exists if you "
        "propose a change, and comment.md passes validate_comment.",
        "- If the root cause cannot be confirmed, say Not confirmed and what is missing. Do not guess.",
    ]
    return "\n".join(lines)


# --- Status -------------------------------------------------------------------------------


def _count(folder: Path, pattern: str) -> int:
    return sum(1 for p in folder.glob(pattern) if p.is_file()) if folder.is_dir() else 0


def _section_markdown(markdown: str, name: str) -> str:
    lines = markdown.splitlines()
    span = _find_section(_sections(lines), name)
    if span is None:
        return ""
    return "\n".join(lines[span[0]:span[1] - 1]).strip()


def _first_paragraph(markdown: str) -> str:
    paragraph: list[str] = []
    for line in markdown.splitlines():
        if not line.strip():
            if paragraph:
                break
            continue
        if line.lstrip().startswith(("|", "```", ">", "#")):
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return " ".join(paragraph)


def ticket_status(inv: Investigator, key: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where a ticket stands, read from its folder."""
    folder = inv.workspace.ticket_dir(key)
    request = read_request(folder)
    ticket_md = folder / "ticket.md"
    report_md = folder / "report.md"
    # The file may be read at the very moment it is being rewritten (empty for an instant).
    try:
        ticket_text = ticket_md.read_text(encoding="utf-8", errors="replace") if ticket_md.is_file() else ""
    except OSError:
        ticket_text = ""
    intake_done = bool(ticket_text.strip()) and "<title>" not in ticket_text[:200]
    queries = _count(folder / "queries", "*.json")
    logs = _count(folder / "logs", "*.log")
    sources = sum(1 for p in (folder / "metadata" / "force-app").rglob("*")
                  if p.is_file() and not p.name.endswith("-meta.xml") or p.name.endswith(".flow-meta.xml")) \
        if (folder / "metadata" / "force-app").is_dir() else 0
    evidence_count = queries + logs + sources

    title = request.get("title")
    if intake_done:
        first = ticket_text.lstrip().splitlines()[0]
        if first.startswith("# "):
            title = first[2:].split(":", 1)[-1].strip() or title

    validation = None
    root_cause_status = None
    if report_md.is_file():
        try:
            validation = inv.validate_report(key)
        except SfInvestigatorError as exc:
            validation = {"ok": False, "problems": [exc.message], "warnings": [], "citations": {"valid": 0, "broken": 0}}
        match = re.search(r"root cause status\s*\|\s*([^|\n]+)", report_md.read_text(encoding="utf-8", errors="replace"), re.I)
        root_cause_status = match.group(1).strip() if match else validation.get("root_cause_status")

    steps = [
        {"id": "saved", "label": "Ticket saved", "done": True},
        {"id": "intake", "label": "Ticket read and structured", "done": intake_done},
        {"id": "evidence", "label": "Evidence gathered from the org", "done": evidence_count > 0,
         "detail": f"{queries} query result(s), {sources} source file(s), {logs} log(s)" if evidence_count else None},
        {"id": "report", "label": "Root cause written up", "done": report_md.is_file()},
        {"id": "checked", "label": "Evidence check passed", "done": bool(validation and validation["ok"]),
         "detail": (f"{validation['citations']['valid']} citation(s) verified" if validation and validation["ok"]
                    else (f"{len(validation['problems'])} problem(s) to fix" if validation else None))},
        {"id": "fix", "label": "Fix proposed", "done": (folder / "fix.diff").is_file()},
        {"id": "comment", "label": "Ticket comment drafted", "done": (folder / "comment.md").is_file()},
    ]

    run_state = (run or {}).get("status")
    if run_state == "running":
        stage = "running"
    elif report_md.is_file() and (folder / "comment.md").is_file():
        stage = "done"
    elif report_md.is_file():
        stage = "report"
    elif run_state in ("failed", "cancelled", "interrupted"):
        stage = run_state
    elif evidence_count or intake_done:
        stage = "in_progress"
    else:
        stage = "new"

    return {
        "key": key,
        "title": title,
        "org": request.get("org"),
        "sandbox": request.get("sandbox"),
        "created_at": request.get("created_at"),
        "source": request.get("source") or "claude-code",
        "stage": stage,
        "steps": steps,
        "root_cause_status": root_cause_status,
        "evidence_check": ({"ok": validation["ok"], "citations": validation["citations"],
                            "problems": validation["problems"], "warnings": validation["warnings"]}
                           if validation else None),
        "run": run,
    }


def ticket_detail(inv: Investigator, key: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Status plus everything the result page shows, rendered to safe HTML."""
    out = ticket_status(inv, key, run)
    folder = inv.workspace.ticket_dir(key)

    def read(name: str) -> str | None:
        path = folder / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    report = read("report.md")
    out["report_html"] = markdown_to_html(report) if report else None
    if report:
        root_cause = _section_markdown(report, "Root cause")
        fix = _section_markdown(report, "Fix")
        out["root_cause_summary"] = _first_paragraph(root_cause)
        out["root_cause_summary_html"] = markdown_to_html(out["root_cause_summary"]) if out["root_cause_summary"] else None
        out["root_cause_html"] = markdown_to_html(root_cause) if root_cause else None
        out["fix_html"] = markdown_to_html(fix) if fix else None
        out["tests_html"] = markdown_to_html(_section_markdown(report, "Test results")) or None
        out["risks_html"] = markdown_to_html(_section_markdown(report, "Risks")) or None
        out["open_questions_html"] = markdown_to_html(_section_markdown(report, "Open questions")) or None
    ticket = read("ticket.md")
    out["ticket_html"] = markdown_to_html(ticket) if ticket and "<title>" not in ticket[:200] else None
    out["original_text"] = read(f"attachments/{ORIGINAL_NAME}")
    out["comment_markdown"] = read("comment.md")
    out["comment_html"] = markdown_to_html(out["comment_markdown"]) if out["comment_markdown"] else None
    out["fix_diff"] = read("fix.diff")
    out["has_report_html"] = (folder / "report.html").is_file()
    try:
        out["evidence"] = inv.evidence_index(key)["artifacts"]
    except SfInvestigatorError:
        out["evidence"] = []
    request = read_request(folder)
    out["prompt"] = agent_prompt(request) if request.get("org") else None
    return out


def list_tickets(inv: Investigator, runs: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = []
    for key in inv.workspace.list_tickets():
        try:
            status = ticket_status(inv, key, (runs or {}).get(key))
        except SfInvestigatorError:
            continue
        status.pop("steps", None)
        out.append(status)
    out.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return out


def delete_ticket(inv: Investigator, key: str) -> dict[str, Any]:
    """Remove a ticket's local folder. Touches no org."""
    folder = inv.workspace.ticket_dir(key).resolve()
    root = inv.workspace.tickets_root.resolve()
    if folder.parent != root:
        raise WorkspaceError("Refusing to delete outside the tickets folder.")

    def make_writable(function: Any, path: str, _exc: Any) -> None:
        os.chmod(path, stat.S_IWRITE)  # git objects are read-only on Windows
        function(path)

    shutil.rmtree(folder, onerror=make_writable)
    return {"deleted": key}
