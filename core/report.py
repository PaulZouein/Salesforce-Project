"""Ticket files: templates, evidence checking, HTML export and search across past tickets.

The validator enforces the project's first rule mechanically: a report whose root cause is
stated as confirmed must cite artifacts that actually exist in the ticket folder (a file
and line, a saved query result, a log line), and must not contain personal data.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import InputValidationError, SfInvestigatorError, WorkspaceError
from .gitrepo import GitRepo
from .workspace import Workspace, validate_ticket_key

REQUIRED_REPORT_SECTIONS = (
    "Ticket summary",
    "Investigation steps",
    "Evidence",
    "Root cause",
    "Fix",
    "Test results",
    "Risks",
    "Open questions",
)

TICKET_TEMPLATE = """# {key}: <title>

| Field | Value |
|---|---|
| Key | {key} |
| Type | |
| Status | |
| Priority | |
| Created | |
| Last updated | |
| Environment reported | |
| Org alias to investigate | |

## Summary

<Two or three sentences: what is broken, for whom, since when.>

## Steps to reproduce

1.

## Expected result

## Actual result

## Affected records

<Record Ids only. No names, emails or phone numbers.>

## Comment timeline

| When | Who (role, not name) | What it adds or changes |
|---|---|---|
| | | |

## History

<Status changes, reassignments, linked tickets.>

## Attachments

<Files in attachments/ and what each one shows.>

## Ambiguities

<What is unclear or contradictory, and which answer would change what gets checked.>
"""

REPORT_TEMPLATE = """# Investigation report: {key}

| | |
|---|---|
| Ticket | {key} |
| Org investigated | <alias> (<org id>) |
| Org classification | <production / sandbox / scratch / developer> |
| Mode | <advise / apply> |
| Root cause status | <Confirmed / Not confirmed> |
| Report date | {date} |

## Ticket summary

<What was reported, in your own words. Record Ids only.>

## Investigation steps

1. <What you checked, in order, and what each step showed.>

## Evidence

Paths are relative to `tickets/{key}/`.

| # | Artifact | What it shows |
|---|---|---|
| E1 | `metadata/force-app/main/default/classes/Example.cls:42` | |
| E2 | `queries/<file>.json` | |

## Root cause

<One precise statement. Every claim cites evidence (E1, E2, …). Anything not backed by an
artifact is labelled **Hypothesis**. If the cause is not confirmed, say what is missing.>

## Fix

<Smallest correct change, as a diff. Why this layer. What else uses the changed component.>

## Test results

<Apex tests run, with outcomes. Manual checks. If nothing was run, say so.>

## Risks

## Open questions
"""

COMMENT_TEMPLATE = """<!-- Draft for {key}. Paste into the ticket yourself; nothing is posted automatically. -->

**Root cause** (<confirmed / not confirmed>)

<Two or three sentences in plain language.>

**Evidence**

- <artifact and what it shows>

**Proposed fix**

<What changes, where, and why it is the smallest correct change.>

**Testing**

<What was run and the result, or what still needs to be run.>

**Next step / needed from you**

<One line.>
"""

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_CITATION_RE = re.compile(
    r"`([A-Za-z0-9_./\-]+\.(?:cls|trigger|xml|json|log|md|apex|diff|txt|page|component|cmp|js|html|css|soql))"
    r"(?::(\d+)(?:-(\d+))?)?`"
)
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
#: Phone numbers, with an optional country code and an optional area code in parentheses: "+44 20 7946 0958",
#: "(415) 555-0132", "+1 (415) 555-0132". Dates, case numbers without separators and versions are filtered below.
#: The header row "| Root cause status | Confirmed |", also with the label or the value in bold.
STATUS_ROW_RE = r"root cause status\**\s*\|\s*\**\s*([^|\n*]+)"
_PHONE_RE = re.compile(r"(?<![\w.\-])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{1,4}\)\s?)?\d(?:[\s.\-]{0,2}\d){6,12}(?![\w\-])")
_SPECULATION_RE = re.compile(
    r"\b(probably|likely|might|may be|maybe|seems|appears to|i think|i believe|possibly|presumably|"
    r"should be|could be|most likely)\b",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"\bTODO\b|\bTBD\b|<[A-Za-z][^<>|`\n]{2,80}>")
#: Addresses that cannot belong to a person: the reserved example domains (and their subdomains), and system
#: mailboxes. Not ".invalid": a sandbox refresh appends it to real users' addresses. Not ".test" either: the
#: simulated orgs use it for their customers' addresses, which stand for real people's.
_RESERVED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
_RESERVED_EMAIL_TLDS = (".example", ".localhost")
_SYSTEM_MAILBOXES = frozenset({"noreply", "no-reply", "donotreply", "do-not-reply"})


def _not_a_person(address: str) -> bool:
    local, _, domain = address.lower().rpartition("@")
    if local in _SYSTEM_MAILBOXES or domain == "localhost" or domain.endswith(_RESERVED_EMAIL_TLDS):
        return True
    return any(domain == d or domain.endswith("." + d) for d in _RESERVED_EMAIL_DOMAINS)


# --- Ticket scaffolding -----------------------------------------------------------------


def init_ticket(workspace: Workspace, ticket_key: str) -> dict[str, Any]:
    """Create ``tickets/<KEY>/`` with its folders and a ``ticket.md`` template."""
    validate_ticket_key(ticket_key)
    path = workspace.ticket_dir(ticket_key, create=True)
    ticket_file = path / "ticket.md"
    created = False
    if not ticket_file.exists():
        ticket_file.write_text(TICKET_TEMPLATE.format(key=ticket_key), encoding="utf-8")
        created = True
    return {
        "ticket_key": ticket_key,
        "folder": f"tickets/{ticket_key}",
        "ticket_file": f"tickets/{ticket_key}/ticket.md",
        "created_template": created,
        "folders": ["attachments", "metadata", "queries", "logs", "apex"],
        "attachments": sorted(p.name for p in (path / "attachments").iterdir()) if (path / "attachments").is_dir() else [],
        "next_step": "Fill in ticket.md from the pasted ticket, then summarize it back and list ambiguities.",
    }


def write_template(workspace: Workspace, ticket_key: str, kind: str, *, overwrite: bool = False) -> dict[str, Any]:
    """Write the report or comment template into the ticket folder."""
    templates = {"report": ("report.md", REPORT_TEMPLATE), "comment": ("comment.md", COMMENT_TEMPLATE)}
    if kind not in templates:
        raise InputValidationError(f"kind must be one of {sorted(templates)}.")
    name, template = templates[kind]
    path = workspace.ticket_dir(ticket_key) / name
    if path.exists() and not overwrite:
        return {"path": name, "created": False, "note": f"{name} already exists; it was left untouched."}
    path.write_text(
        template.format(key=ticket_key, date=datetime.now(timezone.utc).strftime("%Y-%m-%d")), encoding="utf-8"
    )
    return {"path": name, "created": True}


# --- Validation -------------------------------------------------------------------------


def _sections(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map heading text (lower-case) to its (start, end) line range, 1-based, end exclusive."""
    heads: list[tuple[int, int, str]] = []
    in_code = False
    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = _HEADING_RE.match(line)
        if match:
            heads.append((number, len(match.group(1)), match.group(2).strip().lower()))
    out: dict[str, tuple[int, int]] = {}
    for index, (number, level, title) in enumerate(heads):
        end = len(lines) + 1
        for later_number, later_level, _ in heads[index + 1:]:
            if later_level <= level:
                end = later_number
                break
        out.setdefault(title, (number, end))
    return out


def report_digest(path: Path, *, max_chars: int = 500) -> dict[str, Any] | None:
    """What a finished report concluded, in a few lines: the status and the first paragraph of its root cause.

    Used to remind the investigator of earlier tickets on the same org. ``None`` when there is no report.
    """
    try:
        markdown = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = markdown.splitlines()
    status = re.search(STATUS_ROW_RE, markdown, re.IGNORECASE)
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), None)
    span = _find_section(_sections(lines), "Root cause")
    paragraph: list[str] = []
    for line in (lines[span[0]:span[1] - 1] if span else []):
        if not line.strip():
            if paragraph:
                break
            continue
        if line.lstrip().startswith(("|", "```", ">", "#")):
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    summary = " ".join(paragraph)
    return {
        "title": title,
        "root_cause_status": status.group(1).strip() if status else None,
        "root_cause": summary[:max_chars] + ("…" if len(summary) > max_chars else ""),
    }


def _find_section(sections: dict[str, tuple[int, int]], name: str) -> tuple[int, int] | None:
    wanted = name.lower()
    for title, span in sections.items():
        if title == wanted or title.startswith(wanted):
            return span
    return None


def _personal_data(lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    emails, phones = [], []
    in_code = False
    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
        for match in _EMAIL_RE.finditer(line):
            if _not_a_person(match.group(0)):
                continue
            emails.append({"line": number, "kind": "email address"})
        if in_code:
            continue
        for match in _PHONE_RE.finditer(line):
            value = match.group(0)
            digits = re.sub(r"\D", "", value)
            if re.match(r"^\d{4}[\-.]\d{2}[\-.]\d{2}", value) or len(digits) > 13 or len(digits) < 9:
                continue  # a date, a long number, or too short for a phone number (a line range like 1200-1234)
            if not re.search(r"[\s().\-+]", value) and len(digits) < 10:
                continue
            phones.append({"line": number, "kind": "possible phone number"})
    return emails, phones


def _cited_files_with_local_edits(base: Path, cited: set[str]) -> list[str]:
    """Cited metadata files whose working copy differs from the version last retrieved from the org."""
    from .metadata import load_baselines  # local import: metadata does not depend on this module

    repo_path = base / "metadata"
    if not (repo_path / ".git").exists():
        return []
    baselines = load_baselines(repo_path)
    repo = GitRepo(repo_path)
    out = []
    for rel in sorted(cited):
        if not rel.startswith("metadata/"):
            continue
        inner = rel[len("metadata/"):]
        commit = baselines.get(inner)
        try:
            if commit and repo.differs(commit, None, [inner]):
                out.append(rel)
        except SfInvestigatorError:
            continue
    return out


def validate_report(workspace: Workspace, ticket_key: str, filename: str = "report.md") -> dict[str, Any]:
    """Check a report against the evidence and data-minimization rules."""
    path = workspace.resolve(ticket_key, filename)
    base = workspace.ticket_dir(ticket_key).resolve()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sections = _sections(lines)
    problems: list[str] = []
    warnings: list[str] = []

    missing = [name for name in REQUIRED_REPORT_SECTIONS if _find_section(sections, name) is None]
    if missing:
        problems.append("Missing section(s): " + ", ".join(missing) + ".")

    valid: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    line_counts: dict[Path, int] = {}
    for number, line in enumerate(lines, start=1):
        for match in _CITATION_RE.finditer(line):
            rel, first, last = match.group(1), match.group(2), match.group(3)
            entry: dict[str, Any] = {"report_line": number, "citation": match.group(0).strip("`")}
            candidate = (base / rel).resolve()
            if base not in candidate.parents or not candidate.is_file():
                if "/" not in rel and not first:
                    # A bare file name without a line (`CaseService.cls`) names a component in prose;
                    # it is not a citation. With a folder or a line number it must resolve.
                    continue
                broken.append({**entry, "reason": "file not found in the ticket folder"})
                continue
            if first:
                if candidate not in line_counts:
                    line_counts[candidate] = len(candidate.read_text(encoding="utf-8", errors="replace").splitlines())
                highest = int(last or first)
                if int(first) < 1 or highest > line_counts[candidate] or (last and int(last) < int(first)):
                    broken.append({**entry, "reason": f"line out of range (file has {line_counts[candidate]} lines)"})
                    continue
            valid.append(entry)
    for item in broken:
        problems.append(f"Line {item['report_line']}: citation `{item['citation']}` is broken ({item['reason']}).")
    edited = _cited_files_with_local_edits(base, {c["citation"].split(":")[0] for c in valid})
    if edited:
        warnings.append(
            "Cited source file(s) carry local edits (a drafted fix), so their lines no longer all match what "
            "was retrieved from the org: " + ", ".join(edited) + ". Say in the report which version the "
            "citations refer to, and check that each cited line still shows what you claim."
        )

    def citations_in(span: tuple[int, int] | None) -> int:
        return 0 if span is None else sum(1 for c in valid if span[0] <= c["report_line"] < span[1])

    root = _find_section(sections, "Root cause")
    evidence = _find_section(sections, "Evidence")
    full_text = "\n".join(lines)
    status_match = re.search(STATUS_ROW_RE, full_text, re.IGNORECASE)
    status = status_match.group(1).strip().lower() if status_match else ""
    root_text = "\n".join(lines[root[0] - 1: root[1] - 1]) if root else ""
    if status_match:
        # The header row decides. The section may well say "the flow hypothesis was not confirmed"
        # about a hypothesis it ruled out, in a report whose root cause is confirmed.
        # Its first word decides: "Confirmed (not reproduced in a sandbox)" is confirmed.
        first = (re.findall(r"[a-z]+", status) or [""])[0]
        not_confirmed = first in ("not", "unconfirmed", "unknown", "pending", "tbd", "none", "open")
        confirmed = not not_confirmed and "confirm" in status
    else:
        not_confirmed = "not confirmed" in root_text.lower()
        confirmed = not not_confirmed and bool(root)

    evidence_refs = len(re.findall(r"\bE\d+\b", root_text))
    if confirmed and not valid:
        problems.append(
            "The root cause is marked Confirmed but the report cites no artifact that exists in the ticket "
            "folder. Cite a file and line, a saved query result or a log line, or mark the root cause as "
            "Not confirmed."
        )
    elif root and confirmed:
        if citations_in(evidence) == 0 and citations_in(root) == 0:
            problems.append(
                "The root cause is presented as confirmed but its Evidence and Root cause sections cite no "
                "artifact that exists in the ticket folder. Cite a file and line, a saved query result or a log "
                "line, or mark the root cause as Not confirmed."
            )
        elif citations_in(root) == 0 and evidence_refs == 0:
            problems.append(
                "The Root cause section does not reference any evidence. Cite the artifacts directly or by "
                "their Evidence table number (E1, E2, …)."
            )
    if not_confirmed and root and not re.search(r"(missing|need|to confirm|would confirm|next step)", root_text, re.I):
        warnings.append("The root cause is not confirmed, but the section does not say what is missing to confirm it.")
    if status_match is None:
        warnings.append("No 'Root cause status' row found in the header table (Confirmed / Not confirmed).")
    elif "<" in status_match.group(1):
        problems.append("'Root cause status' still holds the template placeholder.")

    if root:
        in_code = False
        for number in range(root[0], root[1] - 1):
            line = lines[number]
            if line.strip().startswith("```"):
                in_code = not in_code
            if in_code or not _SPECULATION_RE.search(line):
                continue
            nearby = " ".join(lines[max(root[0], number - 1): min(root[1] - 1, number + 2)]).lower()
            if "hypothesis" not in nearby:
                warnings.append(
                    f"Line {number + 1}: speculative wording in the root cause without a Hypothesis label "
                    f"({_SPECULATION_RE.search(line).group(0)!r})."
                )

    emails, phones = _personal_data(lines)
    for item in emails:
        problems.append(f"Line {item['line']}: contains an email address. Reference the record Id instead.")
    for item in phones:
        warnings.append(f"Line {item['line']}: looks like a phone number. Remove it if it is one.")

    in_code = False
    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
        if in_code or line.lstrip().startswith("<!--"):
            continue
        without_code = re.sub(r"`[^`]*`", "", line)
        if _PLACEHOLDER_RE.search(without_code):
            warnings.append(f"Line {number}: unfinished placeholder ({_PLACEHOLDER_RE.search(without_code).group(0)[:40]!r}).")

    return {
        "file": filename,
        "ok": not problems,
        "root_cause_status": "not confirmed" if not_confirmed else ("confirmed" if confirmed else "unknown"),
        "problems": problems,
        "warnings": warnings[:40],
        "citations": {"valid": len(valid), "broken": len(broken)},
        "sections_found": [name for name in REQUIRED_REPORT_SECTIONS if name not in missing],
    }


def validate_comment(workspace: Workspace, ticket_key: str, filename: str = "comment.md") -> dict[str, Any]:
    """Check a draft ticket comment for personal data, leftovers and length."""
    path = workspace.resolve(ticket_key, filename)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    problems: list[str] = []
    warnings: list[str] = []
    emails, phones = _personal_data(lines)
    for item in emails:
        problems.append(f"Line {item['line']}: contains an email address. Reference the record Id instead.")
    for item in phones:
        warnings.append(f"Line {item['line']}: looks like a phone number. Remove it if it is one.")
    body = [line for line in lines if not line.lstrip().startswith("<!--")]
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("<!--"):
            continue
        if _PLACEHOLDER_RE.search(re.sub(r"`[^`]*`", "", line)):
            problems.append(f"Line {number}: unfinished placeholder.")
    words = len(" ".join(body).split())
    if words > 350:
        warnings.append(f"The comment has {words} words; ticket comments read best under about 250.")
    text = "\n".join(body).lower()
    if "root cause" not in text:
        warnings.append("The comment does not state the root cause (or that it is not confirmed).")
    return {"file": filename, "ok": not problems, "problems": problems, "warnings": warnings, "words": words}


# --- Past tickets -----------------------------------------------------------------------


def list_tickets(workspace: Workspace) -> dict[str, Any]:
    tickets = []
    for key in workspace.list_tickets():
        folder = workspace.tickets_root / key
        title = None
        ticket_file = folder / "ticket.md"
        if ticket_file.is_file():
            for line in ticket_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        status = None
        report = folder / "report.md"
        if report.is_file():
            match = re.search(STATUS_ROW_RE, report.read_text(encoding="utf-8", errors="replace"),
                              re.IGNORECASE)
            status = match.group(1).strip() if match else "report present"
        tickets.append({
            "ticket_key": key,
            "title": title,
            "has_report": report.is_file(),
            "root_cause_status": status,
            "has_comment": (folder / "comment.md").is_file(),
        })
    return {"count": len(tickets), "tickets": tickets}


def search_tickets(workspace: Workspace, pattern: str, *, max_results: int = 60) -> dict[str, Any]:
    """Search ticket.md, report.md and comment.md of every ticket: has this been seen before?"""
    if not isinstance(pattern, str) or not pattern or len(pattern) > 300:
        raise InputValidationError("A search pattern (at most 300 characters) is required.")
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise InputValidationError(f"Invalid regular expression: {exc}.") from None
    max_results = max(1, min(int(max_results), 200))
    matches: list[dict[str, Any]] = []
    for key in workspace.list_tickets():
        for name in ("ticket.md", "report.md", "comment.md"):
            path = workspace.tickets_root / key / name
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if regex.search(line):
                    matches.append({"ticket_key": key, "file": name, "line": number, "text": line.strip()[:240]})
                    if len(matches) >= max_results:
                        return {"pattern": pattern, "matches": matches, "truncated": True}
    return {"pattern": pattern, "match_count": len(matches), "matches": matches}


# --- HTML export ------------------------------------------------------------------------

_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1c1a;--muted:#6b6862;--line:#e2dfd8;--card:#ffffff;--accent:#0b6e4f;--code:#f2efe8;
--add:#e3f5e9;--del:#fbe6e4}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a09d95;--line:#33322e;--card:#1e1e1b;
--accent:#5fd0a5;--code:#262622;--add:#173524;--del:#40201d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:900px;margin:0 auto;padding:32px 16px 80px}
h1{font-size:1.7rem;line-height:1.25;margin:0 0 12px}h2{font-size:1.2rem;margin:36px 0 10px;padding-top:14px;
border-top:1px solid var(--line)}h3{font-size:1.02rem;margin:22px 0 6px}
p{margin:10px 0}a{color:var(--accent)}code{background:var(--code);padding:1px 5px;border-radius:4px;
font:0.88em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
pre{background:var(--code);padding:14px;border-radius:8px;overflow-x:auto;font-size:0.86rem;line-height:1.5}
pre code{background:none;padding:0;overflow-wrap:normal}
.tablewrap{overflow-x:auto;margin:14px 0}table{border-collapse:collapse;width:100%;background:var(--card);font-size:0.94rem}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}th{background:var(--code)}
td:has(>code:only-child){min-width:17rem}
blockquote{margin:12px 0;padding:2px 14px;border-left:3px solid var(--accent);color:var(--muted)}
.add{background:var(--add);display:block}.del{background:var(--del);display:block}.hunk{color:var(--muted);display:block}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:0.8rem;font-weight:600;
border:1px solid var(--line);margin-bottom:14px}.ok{color:var(--accent)}.warn{color:#b45309}
footer{margin-top:48px;color:var(--muted);font-size:0.85rem}hr{border:0;border-top:1px solid var(--line);margin:24px 0}
ul,ol{padding-left:24px}li{margin:3px 0}
"""


#: A link target as it reads after escaping: http(s), then URL characters only. Quotes are escaped to entities
#: that this does not accept, so a link can never close its href and add attributes; "&" may appear only as
#: "&amp;" (a query string).
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://(?:[A-Za-z0-9\-._~:/?#\[\]@!$%+,;=]|&amp;)+)\)")


def _inline(text: str) -> str:
    """Inline Markdown to HTML. Report text comes from tickets and orgs: everything is escaped, quotes included."""
    out = html.escape(text, quote=True)
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<em>\1</em>", out)
    out = _LINK_RE.sub(r'<a href="\2" rel="noopener noreferrer">\1</a>', out)
    return out


def markdown_to_html(markdown: str) -> str:
    """Convert the subset of Markdown used in reports to HTML."""
    lines = markdown.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    list_stack: list[str] = []
    i = 0

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            close_lists()
            language = stripped[3:].strip().lower()
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if language == "diff":
                rendered = []
                for row in block:
                    escaped = html.escape(row, quote=False) or " "
                    if row.startswith("@@"):
                        rendered.append(f'<span class="hunk">{escaped}</span>')
                    elif row.startswith("+") and not row.startswith("+++"):
                        rendered.append(f'<span class="add">{escaped}</span>')
                    elif row.startswith("-") and not row.startswith("---"):
                        rendered.append(f'<span class="del">{escaped}</span>')
                    else:
                        rendered.append(escaped + "\n")
                out.append("<pre><code>" + "".join(rendered) + "</code></pre>")
            else:
                out.append("<pre><code>" + html.escape("\n".join(block), quote=False) + "</code></pre>")
        elif stripped.startswith("<!--"):
            while i < len(lines) and "-->" not in lines[i]:
                i += 1
        elif not stripped:
            flush_paragraph()
            close_lists()
        elif _HEADING_RE.match(line):
            flush_paragraph()
            close_lists()
            match = _HEADING_RE.match(line)
            level = len(match.group(1))
            out.append(f"<h{level}>{_inline(match.group(2))}</h{level}>")
        elif stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", lines[i + 1]):
            flush_paragraph()
            close_lists()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))])
                i += 1
            i -= 1
            table = ['<div class="tablewrap"><table>']
            if any(header):
                table.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead>")
            table.append("<tbody>")
            for row in rows:
                table.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
            table.append("</tbody></table></div>")
            out.append("".join(table))
        elif re.match(r"^\s*([-*]|\d+[.)])\s+", line):
            flush_paragraph()
            kind = "ol" if re.match(r"^\s*\d+[.)]\s+", line) else "ul"
            if not list_stack:
                out.append(f"<{kind}>")
                list_stack.append(kind)
            elif list_stack[-1] != kind:
                out.append(f"</{list_stack.pop()}>")
                out.append(f"<{kind}>")
                list_stack.append(kind)
            item = re.sub(r"^\s*([-*]|\d+[.)])\s+", "", line)
            # A wrapped list item continues on lines indented by two or more spaces.
            while i + 1 < len(lines) and lines[i + 1].startswith(("  ", "\t")) and lines[i + 1].strip() \
                    and not re.match(r"^\s*([-*]|\d+[.)])\s+", lines[i + 1]):
                i += 1
                item += " " + lines[i].strip()
            out.append(f"<li>{_inline(item)}</li>")
        elif stripped.startswith(">"):
            flush_paragraph()
            close_lists()
            quoted = [stripped.lstrip(">").strip()]
            while i + 1 < len(lines) and lines[i + 1].strip().startswith(">"):
                i += 1
                quoted.append(lines[i].strip().lstrip(">").strip())
            out.append("<blockquote><p>" + _inline(" ".join(q for q in quoted if q)) + "</p></blockquote>")
        elif re.match(r"^(-{3,}|\*{3,})$", stripped):
            flush_paragraph()
            close_lists()
            out.append("<hr>")
        else:
            close_lists()
            paragraph.append(stripped)
        i += 1
    flush_paragraph()
    close_lists()
    return "\n".join(out)


def export_report_html(workspace: Workspace, ticket_key: str, filename: str = "report.md") -> dict[str, Any]:
    """Render the report as a self-contained HTML page next to the Markdown file."""
    path = workspace.resolve(ticket_key, filename)
    if path.suffix.lower() != ".md":
        raise WorkspaceError("Only Markdown files can be exported.")
    markdown = path.read_text(encoding="utf-8", errors="replace")
    check = validate_report(workspace, ticket_key, filename)
    badge_class = "ok" if check["ok"] else "warn"
    badge = (
        f"Evidence check passed · {check['citations']['valid']} citations verified"
        if check["ok"]
        else f"Evidence check: {len(check['problems'])} problem(s) to fix"
    )
    title = next((l[2:].strip() for l in markdown.splitlines() if l.startswith("# ")), f"Report {ticket_key}")
    page = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f'<div class="badge {badge_class}">{html.escape(badge)}</div>\n'
        f"{markdown_to_html(markdown)}\n"
        f"<footer>Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from {html.escape(filename)}. "
        "Local file: not posted anywhere.</footer></main></body></html>\n"
    )
    target = path.with_suffix(".html")
    target.write_text(page, encoding="utf-8")
    return {"path": workspace.relative(ticket_key, target), "evidence_check": check}
