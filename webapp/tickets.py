"""Tickets as the interface sees them: created from pasted text or a PDF, with a status read from the files.

The status of a ticket is derived from what is on disk in ``tickets/<KEY>/``, not from what the
interface remembers. It is therefore the same whether the investigation was started from the
interface, from Claude Code, or is still running.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.app import Investigator
from core.errors import InputValidationError, SfInvestigatorError, WorkspaceError
from core.report import _find_section, _sections, markdown_to_html
from core.sf_cli import validate_target_org
from core.workspace import validate_ticket_key

MAX_TICKET_CHARS = 200_000
MAX_FOLLOW_UP_CHARS = 20_000
MAX_ATTACHMENT_BYTES = 5_000_000
MAX_ATTACHMENTS = 20
ORIGINAL_NAME = "ticket-original.txt"
ORIGINAL_PDF_NAME = "ticket-original.pdf"
REQUEST_NAME = "request.json"
#: A ticket given as a PDF (an export or a printout from Jira).
MAX_TICKET_PDF_BYTES = 10_000_000
MAX_PDF_PAGES = 80
#: Spending limit per run, in US dollars.
MIN_BUDGET_USD, MAX_BUDGET_USD = 0.05, 100.0
#: What may be attached to a ticket: text the investigator can read, and images and PDFs it can look at.
ATTACHMENT_TYPES = frozenset({".txt", ".log", ".md", ".csv", ".json", ".xml", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"})
#: What the result download contains, when present.
BUNDLE_FILES = ("report.html", "report.md", "comment.md", "fix.diff", "ticket.md")

_FOLLOW_UP_RE = re.compile(r"^follow-up-\d+\.txt\Z")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,59}\Z")

_KEY_LINE_RE = re.compile(r"^\s*(?:key|ticket|issue)\s*[:#]\s*([A-Za-z][A-Za-z0-9_]{0,19}-\d{1,9})\b", re.I | re.M)
_KEY_ANY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,19}-\d{1,9})\b")
_TITLE_LINE_RE = re.compile(r"^\s*(?:title|summary|subject)\s*:\s*(.+)$", re.I | re.M)
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_LEADING_KEY_RE = re.compile(r"^\[?[A-Z][A-Z0-9_]{1,19}-\d{1,9}\]?\s*[:\-\u2013\u2014|]?\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_key(text: str) -> str | None:
    """The ticket key in pasted text: a ``Key:`` line first, otherwise the first thing shaped like one."""
    match = _KEY_LINE_RE.search(text) or _KEY_ANY_RE.search(text)
    return match.group(1).upper() if match else None


def detect_title(text: str) -> str | None:
    match = _TITLE_LINE_RE.search(text)
    if match:
        return _tidy_title(match.group(1))
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 12 and not re.match(r"^[\w ]{1,20}:", line):
            return _tidy_title(line)
    return None


def _tidy_title(line: str) -> str | None:
    """A title without the key in front of it (Jira's exports and printouts start with "[ABC-123] ...")."""
    title = _LEADING_KEY_RE.sub("", line.strip()) or line.strip()
    return _EMAIL_RE.sub("[email]", title)[:160] or None


def read_pdf(data: bytes) -> dict[str, Any]:
    """The text of a ticket given as a PDF: ``{"text", "pages", "readable"}``.

    ``readable`` is false for a PDF with (almost) no text in it, such as a scan. The investigator
    can still read such a PDF itself: Claude Code opens PDFs, images included.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise InputValidationError("The file is empty.")
    if len(data) > MAX_TICKET_PDF_BYTES:
        raise InputValidationError(f"The PDF is too large ({MAX_TICKET_PDF_BYTES // 1_000_000} MB at most).")
    if not bytes(data[:1024]).lstrip().startswith(b"%PDF"):
        raise InputValidationError("That file is not a PDF.", hint="Choose the PDF exported or printed from Jira.")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise WorkspaceError("Reading PDF files needs the pypdf package, which is not installed.",
                             hint="Start the investigator with its start script again: it installs what is missing.") from None
    try:
        reader = PdfReader(io.BytesIO(bytes(data)))
        if reader.is_encrypted and not reader.decrypt(""):
            raise InputValidationError("The PDF is protected by a password.", hint="Export it again without one.")
        total = len(reader.pages)
        parts = [(page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES]]
    except SfInvestigatorError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parsing failure means the same thing to the user
        raise InputValidationError("The PDF could not be read.", hint=f"{type(exc).__name__}: {str(exc)[:200]}") from None
    lines = [line.rstrip() for line in "\n\n".join(parts).replace("\x00", "").splitlines()]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if total > MAX_PDF_PAGES:
        cleaned += f"\n\n[Only the first {MAX_PDF_PAGES} of {total} pages were read as text.]"
    return {"text": cleaned[:MAX_TICKET_CHARS], "pages": total, "readable": len(cleaned) >= 40}


def check_budget(value: Any) -> float | None:
    """A spending limit in US dollars, or ``None`` for none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = float(str(value).strip().lstrip("$"))
    except ValueError:
        raise InputValidationError("The spending limit must be an amount in dollars, for example 1.50.") from None
    if not MIN_BUDGET_USD <= amount <= MAX_BUDGET_USD:
        raise InputValidationError(f"The spending limit must be between ${MIN_BUDGET_USD:.2f} and ${MAX_BUDGET_USD:.0f}.")
    return round(amount, 2)


def create_ticket(inv: Investigator, text: str, org: str, *, key: str | None = None,
                  sandbox: str | None = None, model: str | None = None, economy: bool = False,
                  budget_usd: Any = None, pdf: bytes | None = None) -> dict[str, Any]:
    """Create ``tickets/<KEY>/`` from pasted ticket text (or a PDF of the ticket) and record which org to investigate."""
    if pdf:
        from_pdf = read_pdf(pdf)  # checked before anything is created
        if not isinstance(text, str) or len(text.strip()) < 40:
            text = from_pdf["text"] if from_pdf["readable"] else ""
        header = ("[The ticket was given as a PDF: attachments/ticket-original.pdf. The text below was read from it.]"
                  "\n\n")
        body = text or "[No text could be read from the PDF: read the PDF itself.]"
        cut = "\n\n[The text read from the PDF stops here because it is long: the PDF itself has the rest.]"
        if len(header) + len(body) > MAX_TICKET_CHARS:  # the PDF holds everything: cut the copy, never refuse it
            body = body[:MAX_TICKET_CHARS - len(header) - len(cut)] + cut
        text = header + body
    if not isinstance(text, str) or len(text.strip()) < 40:
        raise InputValidationError("Paste the whole ticket: description, comments and history.",
                                   hint="Or upload the ticket as a PDF.")
    if len(text) > MAX_TICKET_CHARS:
        raise InputValidationError(f"The ticket text is too long ({MAX_TICKET_CHARS:,} characters at most).")
    key = (key or "").strip() or detect_key(text)
    if not key:
        raise InputValidationError(
            "No ticket key was found in the text.",
            hint="Type the key in the field next to the text, for example ABC-123.")
    key = validate_ticket_key(key.upper() if re.match(r"^[A-Za-z][A-Za-z0-9_]*-\d+\Z", key) else key)
    if not isinstance(org, str) or not org.strip():
        raise InputValidationError("Choose the org to investigate.")
    model = set_model(None, model)  # checked before anything is created
    budget_usd = check_budget(budget_usd)
    folder = inv.workspace.tickets_root / key
    if (folder / ORIGINAL_NAME).exists() or (folder / "attachments" / ORIGINAL_NAME).exists():
        raise WorkspaceError(
            f"Ticket {key} already exists.",
            hint="Open it from the ticket list, or delete it there first if you want to start again.")
    inv.init_ticket(key)
    attachments = folder / "attachments"
    attachments.mkdir(exist_ok=True)
    (attachments / ORIGINAL_NAME).write_text(text, encoding="utf-8")
    if pdf:
        (attachments / ORIGINAL_PDF_NAME).write_bytes(bytes(pdf))
    title = detect_title(text.split("\n\n", 1)[1] if pdf else text)
    request = {"key": key, "title": title, "org": org.strip(), "sandbox": (sandbox or "").strip() or None,
               "created_at": utc_now(), "source": "web"}
    if pdf:
        request["pdf"] = ORIGINAL_PDF_NAME
    if model:
        request["model"] = model
    if economy:
        request["economy"] = True
    if budget_usd:
        request["budget_usd"] = budget_usd
    _write_request(folder, request)
    return request


def read_request(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / REQUEST_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_request(folder: Path, request: dict[str, Any]) -> None:
    (folder / REQUEST_NAME).write_text(json.dumps(request, indent=2), encoding="utf-8")


def set_org(folder: Path, key: str, org: Any) -> str:
    """Record which org a ticket investigates: a ticket started in Claude Code has no record of it."""
    if not isinstance(org, str) or not org.strip():
        raise InputValidationError("Choose the org to investigate.")
    org = validate_target_org(org.strip())
    request = read_request(folder)
    request["key"] = key
    request["org"] = org
    _write_request(folder, request)
    return org


def set_model(folder: Path | None, model: str | None) -> str | None:
    """Validate a model name (a short word such as ``sonnet``) and, given a folder, store it for the ticket."""
    model = (model or "").strip() or None
    if model and not MODEL_RE.match(model):
        raise InputValidationError("That is not a model name.", hint="Use a name such as sonnet or opus, or leave it empty.")
    if folder is not None:
        request = read_request(folder)
        if model:
            request["model"] = model
        else:
            request.pop("model", None)
        _write_request(folder, request)
    return model


def set_run_options(folder: Path, *, economy: Any = None, budget_usd: Any = "unchanged") -> dict[str, Any]:
    """Store how the next run should spend tokens: ``economy`` (use fewer tokens) and a spending limit."""
    request = read_request(folder)
    if economy is not None:
        if economy:
            request["economy"] = True
        else:
            request.pop("economy", None)
    if budget_usd != "unchanged":
        amount = check_budget(budget_usd)
        if amount:
            request["budget_usd"] = amount
        else:
            request.pop("budget_usd", None)
    _write_request(folder, request)
    return request


# --- What the user adds after the ticket is created --------------------------------------------


def _attachment_names(folder: Path) -> list[str]:
    attachments = folder / "attachments"
    if not attachments.is_dir():
        return []
    return sorted(p.name for p in attachments.iterdir()
                  if p.is_file() and p.name not in (ORIGINAL_NAME, ORIGINAL_PDF_NAME))


def add_follow_up(inv: Investigator, key: str, text: str) -> dict[str, Any]:
    """Keep something the user adds after the first run (an answer to an open question, a record Id, a correction).

    It is saved next to the ticket as ``attachments/follow-up-NN.txt`` and the next run is told to read it.
    """
    if not isinstance(text, str) or len(text.strip()) < 3:
        raise InputValidationError("Write what you want to add.")
    if len(text) > MAX_FOLLOW_UP_CHARS:
        raise InputValidationError(f"That is too long ({MAX_FOLLOW_UP_CHARS:,} characters at most). Attach it as a file instead.")
    folder = inv.workspace.ticket_dir(key)
    attachments = folder / "attachments"
    attachments.mkdir(exist_ok=True)
    number = 1 + sum(1 for name in _attachment_names(folder) if _FOLLOW_UP_RE.match(name))
    name = f"follow-up-{number:02d}.txt"
    (attachments / name).write_text(f"Added by the user on {utc_now()}\n\n{text.strip()}\n", encoding="utf-8")
    return {"saved": f"attachments/{name}"}


def save_attachment(inv: Investigator, key: str, name: str, data: bytes) -> dict[str, Any]:
    """Save a file the user attaches to a ticket (a screenshot, an exported log, a spreadsheet extract)."""
    if not isinstance(name, str) or not name.strip():
        raise InputValidationError("The file has no name.")
    stem, suffix = os.path.splitext(Path(name.replace("\\", "/")).name)
    if suffix.lower() not in ATTACHMENT_TYPES:
        raise InputValidationError(
            f"Files of type '{suffix.lower() or 'unknown'}' are not accepted.",
            hint="Accepted: " + ", ".join(sorted(ATTACHMENT_TYPES)) + ".")
    # Only plain letters and digits are kept, and the type is kept whatever the length: "Снимок экрана.png"
    # becomes "attachment.png", not the refused "png".
    clean = (re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:90] or "attachment") + suffix
    if clean in (ORIGINAL_NAME, ORIGINAL_PDF_NAME) or _FOLLOW_UP_RE.match(clean):
        raise InputValidationError("That file name is used by the investigator. Rename the file and try again.")
    if not data:
        raise InputValidationError("The file is empty.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise InputValidationError(f"The file is too large ({MAX_ATTACHMENT_BYTES // 1_000_000} MB at most).")
    folder = inv.workspace.ticket_dir(key)
    attachments = folder / "attachments"
    attachments.mkdir(exist_ok=True)
    # Two different files can clean to the same name ("ошибка 1.png" and "скрин 1.png"): never overwrite
    # evidence with other content. The same file sent again keeps its name.
    stem, number = clean[:-len(suffix)], 1
    while (attachments / clean).exists() and (attachments / clean).read_bytes() != data:
        number += 1
        clean = f"{stem}-{number}{suffix}"
    if (attachments / clean).exists():
        return {"saved": f"attachments/{clean}", "bytes": len(data), "note": "This file was already attached."}
    if len(_attachment_names(folder)) >= MAX_ATTACHMENTS:
        raise InputValidationError(f"A ticket can hold {MAX_ATTACHMENTS} attached files at most.")
    (attachments / clean).write_bytes(data)
    return {"saved": f"attachments/{clean}", "bytes": len(data)}


def has_progress(folder: Path) -> bool:
    """True when an earlier run already left work in the ticket folder (so the next one continues from it)."""
    if (folder / "report.md").is_file() or _count(folder / "queries", "*.json") or _count(folder / "logs", "*.log"):
        return True
    return (folder / "metadata" / "force-app").is_dir() and any((folder / "metadata" / "force-app").rglob("*.*"))


def result_bundle(inv: Investigator, key: str) -> bytes:
    """A zip of what the user takes away from a ticket: the report, the comment, the fix and the ticket summary."""
    folder = inv.workspace.ticket_dir(key)
    present = [name for name in BUNDLE_FILES if (folder / name).is_file()]
    if "report.md" not in present:
        raise WorkspaceError("There is no report for this ticket yet.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in present:
            archive.write(folder / name, f"{key}/{name}")
    return buffer.getvalue()


def agent_prompt(request: dict[str, Any], *, resume: bool = False) -> str:
    """The instruction given to Claude Code for a ticket submitted through the interface.

    ``resume`` is for a ticket an earlier run already worked on: continue from what is there.
    """
    key = request["key"]
    sandbox = request.get("sandbox")
    lines = [
        (f"Ticket {key} was submitted through the web interface and has already been worked on. Continue it."
         if resume else f"A new ticket, {key}, was submitted through the web interface."),
        f"Its full original text is in tickets/{key}/attachments/{ORIGINAL_NAME}. Read all of it, comments included.",
        *([f"The ticket was given as a PDF, tickets/{key}/attachments/{ORIGINAL_PDF_NAME}; the text file holds what "
           "could be read from it. Open the PDF itself for anything the text loses or lacks (tables, screenshots, "
           "a scanned page)."] if request.get("pdf") else []),
        f"Read every other file in tickets/{key}/attachments/ as well: files named follow-up-NN.txt are what the "
        "user added after an earlier run (answers, corrections, new facts). They are part of the ticket, and the "
        "newest one wins where they disagree.",
        f"Org to investigate: {request['org']}." + (f" Non-production org available for tests: {sandbox}." if sandbox else ""),
        "",
        "Follow CLAUDE.md and the skills, in this order: intake-ticket, investigate-ticket, propose-fix, "
        "write-ticket-comment. Each one is a file, .claude/skills/<name>/SKILL.md: read it before its step.",
        "",
    ]
    if resume:
        lines += [
            f"What earlier runs left is in tickets/{key}/: ticket.md, metadata/, queries/, logs/ and, if they got "
            "that far, report.md, fix.diff and comment.md. Start from it instead of starting again: call "
            "evidence_index to see what is there, re-check whatever a follow-up changes, gather only what is "
            "still missing, and bring ticket.md, the report, the fix and the comment up to date.",
            "",
        ]
    lines += [
        "You are running unattended, so:",
        "- Do not ask questions. Record every ambiguity in ticket.md, state the assumption you proceed on, "
        "and list what you would have asked under Open questions in the report.",
        "- Stay in advise mode whatever the config says: hand the fix over as a diff. The tools that change an "
        "org are not available here, and there is no shell.",
        "- Start with recall_org_knowledge for this org, and before you finish, use remember_org_fact for "
        "what you learned about how the org is built.",
        "- Finish only when report.md passes validate_report, report.html is exported, fix.diff exists if you "
        "propose a change, and comment.md passes validate_comment.",
        "- If the root cause cannot be confirmed, say Not confirmed and what is missing. Do not guess.",
    ]
    if request.get("economy"):
        lines += [
            "",
            "The user asked to use as few tokens as the rules allow:",
            "- Make independent tool calls together, in one step.",
            "- Retrieve and read only what a hypothesis needs. Query only the fields you need. Do not read a "
            "file twice, and read each SKILL.md once.",
            "- Trace the execution path yourself: subagents are not available in this run.",
            "- Keep ticket.md, the report and the comment short. Do not restate the ticket.",
            "- The evidence rules do not change: every claim still cites an artifact, and the report still "
            "passes validate_report.",
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
    elif run_state in ("failed", "cancelled", "interrupted", "incomplete"):
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
        "model": request.get("model"),
        "economy": bool(request.get("economy")),
        "budget_usd": request.get("budget_usd"),
        "attachments": _attachment_names(folder),
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
    out["has_pdf"] = (folder / "attachments" / ORIGINAL_PDF_NAME).is_file()
    out["comment_markdown"] = read("comment.md")
    out["comment_html"] = markdown_to_html(out["comment_markdown"]) if out["comment_markdown"] else None
    out["fix_diff"] = read("fix.diff")
    out["has_report_html"] = (folder / "report.html").is_file()
    try:
        out["evidence"] = inv.evidence_index(key)["artifacts"]
    except SfInvestigatorError:
        out["evidence"] = []
    request = {**read_request(folder), "key": key}  # a ticket started in Claude Code has no request.json
    out["prompt"] = agent_prompt(request, resume=has_progress(folder)) if request.get("org") else None
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
