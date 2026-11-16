from __future__ import annotations

import json

import pytest

from core.errors import InputValidationError, WorkspaceError
from core.report import REQUIRED_REPORT_SECTIONS, markdown_to_html

from .conftest import SERVICE

KEY = "T-1"

GOOD_REPORT = """# Investigation report: T-1

| | |
|---|---|
| Ticket | T-1 |
| Org investigated | prod (00DWs000000PrdAMAS) |
| Org classification | production |
| Mode | advise |
| Root cause status | Confirmed |
| Report date | 2026-11-12 |

## Ticket summary

Cases for Premium accounts are created with Medium priority (500Ws00000C0003IAA).

## Investigation steps

1. Queried the affected cases.
2. Read the service class.

## Evidence

| # | Artifact | What it shows |
|---|---|---|
| E1 | `{service}:30` | `return;` leaves the method inside the loop |
| E2 | `{query}` | affected cases follow a Standard-account case |
| E3 | `{service}:26-33` | the loop |

## Root cause

The loop returns at the first non-Premium account (E1), so later Premium cases keep Medium (E2).

## Fix

```diff
-                return;
+                continue;
```

## Test results

Existing tests pass; none covers a mixed batch.

## Risks

Existing records are not repaired.

## Open questions

None.
"""


@pytest.fixture
def ticket(inv, project):
    inv.init_ticket(KEY)
    inv.retrieve_components("prod", KEY, ["ApexClass:CaseEscalationService"])
    saved = inv.soql_query("prod", "SELECT Id, Priority FROM Case", ticket_key=KEY, save_as="cases")["saved_to"]

    def write(text=GOOD_REPORT, name="report.md", **replace):
        text = text.format(service=SERVICE, query=saved) if "{service}" in text else text
        for old, new in replace.items():
            text = text.replace(old, new)
        (project.ticket(KEY) / name).write_text(text, encoding="utf-8")
        return inv.validate_report(KEY, name)

    write.saved = saved
    return write


# --- scaffolding ------------------------------------------------------------------------


def test_init_ticket(inv, project):
    out = inv.init_ticket(KEY)
    assert out["created_template"] is True and out["ticket_file"] == "tickets/T-1/ticket.md"
    folder = project.ticket(KEY)
    assert all((folder / sub).is_dir() for sub in ("attachments", "metadata", "queries", "logs", "apex"))
    template = (folder / "ticket.md").read_text()
    assert template.startswith("# T-1: <title>") and "## Comment timeline" in template and "## Ambiguities" in template
    (folder / "ticket.md").write_text("# T-1: mine\n")
    (folder / "attachments" / "error.png").write_bytes(b"\x89PNG")
    again = inv.init_ticket(KEY)
    assert again["created_template"] is False and again["attachments"] == ["error.png"]
    assert (folder / "ticket.md").read_text() == "# T-1: mine\n"


def test_write_template(inv, project):
    inv.init_ticket(KEY)
    assert inv.write_template(KEY, "report") == {"path": "report.md", "created": True}
    report = (project.ticket(KEY) / "report.md").read_text()
    assert all(f"## {name}" in report for name in REQUIRED_REPORT_SECTIONS)
    (project.ticket(KEY) / "report.md").write_text("mine")
    assert inv.write_template(KEY, "report")["created"] is False
    assert (project.ticket(KEY) / "report.md").read_text() == "mine"
    assert inv.write_template(KEY, "report", overwrite=True)["created"] is True
    assert inv.write_template(KEY, "comment")["path"] == "comment.md"
    assert "nothing is posted automatically" in (project.ticket(KEY) / "comment.md").read_text()
    with pytest.raises(InputValidationError):
        inv.write_template(KEY, "email")
    with pytest.raises(WorkspaceError):
        inv.write_template("NOPE-1", "report")


def test_untouched_template_does_not_validate(inv):
    inv.init_ticket(KEY)
    inv.write_template(KEY, "report")
    out = inv.validate_report(KEY)
    assert out["ok"] is False
    assert any("placeholder" in p for p in out["problems"])
    # The template's example citation points at a file that does not exist: it has to be replaced.
    assert out["citations"] == {"valid": 0, "broken": 1}


# --- validate_report --------------------------------------------------------------------


def test_good_report_passes(ticket):
    out = ticket()
    assert out["ok"] is True and out["problems"] == [], out
    assert out["root_cause_status"] == "confirmed"
    assert out["citations"] == {"valid": 3, "broken": 0}
    assert out["sections_found"] == list(REQUIRED_REPORT_SECTIONS)
    assert out["warnings"] == []


def test_broken_citation_fails(ticket):
    out = ticket(**{"CaseEscalationService.cls:30": "CaseEscalationServise.cls:30"})
    assert out["ok"] is False and out["citations"]["broken"] == 1
    assert "file not found in the ticket folder" in out["problems"][0] and "Line 25" in out["problems"][0]


@pytest.mark.parametrize("bad", [":36", ":0", ":30-99", ":33-26", ":400"])
def test_line_out_of_range_fails(ticket, bad):
    out = ticket(**{"CaseEscalationService.cls:30": "CaseEscalationService.cls" + bad})
    assert out["ok"] is False
    assert "line out of range (file has 35 lines)" in out["problems"][0]


def test_last_line_is_in_range(ticket):
    assert ticket(**{"CaseEscalationService.cls:30": "CaseEscalationService.cls:35"})["ok"] is True


def test_bare_file_name_is_a_mention_but_with_a_line_or_folder_it_must_resolve(ticket):
    out = ticket(**{"None.\n": "The class `CaseEscalationService.cls` and `fix.diff` are named here.\n"})
    assert out["ok"] is True and out["citations"] == {"valid": 3, "broken": 0}
    assert ticket(**{"None.\n": "See `CaseEscalationService.cls:30`.\n"})["citations"]["broken"] == 1
    assert ticket(**{"None.\n": "See `classes/CaseEscalationService.cls`.\n"})["citations"]["broken"] == 1


def test_citing_a_file_with_a_drafted_fix_is_warned(ticket, project):
    assert ticket()["warnings"] == []
    service = project.ticket(KEY) / SERVICE
    service.write_text(service.read_text().replace("                return;", "                continue;"))
    out = ticket()
    assert out["ok"] is True
    assert len(out["warnings"]) == 1 and "carry local edits" in out["warnings"][0] and SERVICE in out["warnings"][0]


def test_citation_outside_the_ticket_folder_fails(ticket, project):
    (project.root / "sfti.config.json").write_text("{}")
    out = ticket(**{f"`{ticket.saved}`": "`../../sfti.config.json`"})
    assert out["ok"] is False and out["citations"]["broken"] == 1


def test_confirmed_cause_without_any_evidence_fails(ticket):
    text = GOOD_REPORT.split("## Evidence")[0] + "## Evidence\n\nNone collected.\n\n## Root cause\n\n" \
        "The loop returns too early.\n\n## Fix\n\nx\n\n## Test results\n\nx\n\n## Risks\n\nx\n\n## Open questions\n\nx\n"
    out = ticket(text)
    assert out["ok"] is False
    assert any("cites no artifact" in p for p in out["problems"])


def test_confirmed_cause_that_does_not_reference_its_evidence_fails(ticket):
    out = ticket(**{"(E1)": "", "(E2)": ""})
    assert out["ok"] is False
    assert any("does not reference any evidence" in p for p in out["problems"])


def test_not_confirmed_report_passes_without_citations_but_must_say_what_is_missing(ticket):
    base = GOOD_REPORT.split("## Evidence")[0].replace("| Confirmed |", "| Not confirmed |")
    tail = "## Fix\n\nNot yet proposed.\n\n## Test results\n\nNone.\n\n## Risks\n\nNone.\n\n## Open questions\n\nNone.\n"
    out = ticket(base + "## Evidence\n\nNone yet.\n\n## Root cause\n\nNot confirmed. Hypothesis: the import "
                 "skips the trigger.\n\n" + tail)
    assert out["ok"] is True and out["root_cause_status"] == "not confirmed"
    assert any("does not say what is missing" in w for w in out["warnings"])
    out = ticket(base + "## Evidence\n\nNone yet.\n\n## Root cause\n\nNot confirmed. To confirm it we need a "
                 "debug log of the import.\n\n" + tail)
    assert out["ok"] is True and out["warnings"] == []


def test_email_address_fails(ticket):
    out = ticket(**{"None.\n": "Ask jane.doe@customer.test about it.\n"})
    assert out["ok"] is False
    assert len(out["problems"]) == 1 and "contains an email address" in out["problems"][0]
    assert "jane.doe" not in json.dumps(out)  # the validator does not echo the value


def test_email_inside_a_code_block_also_fails(ticket):
    out = ticket(**{"+                continue;": "+                notify('jane.doe@customer.test');"})
    assert out["ok"] is False


def test_phone_number_is_a_warning_and_ids_are_not(ticket):
    out = ticket(**{"None.\n": "Call +33 1 23 45 67 89. Case 00001003, 2026-11-10 02:00:14, 500Ws00000C0003IAA.\n"})
    assert out["ok"] is True
    assert len([w for w in out["warnings"] if "phone number" in w]) == 1


def test_missing_section_and_placeholder_status(ticket):
    out = ticket(**{"## Risks": "## Dangers"})
    assert out["ok"] is False and "Missing section(s): Risks." in out["problems"][0]
    out = ticket(**{"| Confirmed |": "| <Confirmed / Not confirmed> |"})
    assert any("template placeholder" in p for p in out["problems"])


def test_speculation_without_hypothesis_label_is_warned(ticket):
    out = ticket(**{"The loop returns": "The loop probably returns"})
    assert out["ok"] is True and any("speculative wording" in w and "'probably'" in w for w in out["warnings"])
    out = ticket(**{"The loop returns": "**Hypothesis**: the loop probably returns"})
    assert not any("speculative" in w for w in out["warnings"])


def test_validate_report_path_confinement(ticket, inv):
    ticket()
    with pytest.raises(WorkspaceError):
        inv.validate_report(KEY, "../../README.md")
    with pytest.raises(WorkspaceError):
        inv.validate_report(KEY, "missing.md")


# --- comment ----------------------------------------------------------------------------

GOOD_COMMENT = """<!-- Draft for T-1. Paste into the ticket yourself; nothing is posted automatically. -->

**Root cause** (confirmed)

`CaseEscalationService` stops at the first non-Premium case of a batch.

**Next step**

Deploy the one-line fix to the sandbox.
"""


def test_validate_comment(inv, project):
    inv.init_ticket(KEY)
    path = project.ticket(KEY) / "comment.md"
    path.write_text(GOOD_COMMENT)
    assert inv.validate_comment(KEY) == {"file": "comment.md", "ok": True, "problems": [], "warnings": [],
                                         "words": inv.validate_comment(KEY)["words"]}
    path.write_text(GOOD_COMMENT + "\nContact jane.doe@customer.test or <the admin>.\n")
    out = inv.validate_comment(KEY)
    assert out["ok"] is False and len(out["problems"]) == 2
    path.write_text("Fixed. " + "word " * 400)
    out = inv.validate_comment(KEY)
    assert out["ok"] is True and len(out["warnings"]) == 2
    inv.write_template(KEY, "comment", overwrite=True)
    assert inv.validate_comment(KEY)["ok"] is False


# --- HTML export ------------------------------------------------------------------------


def test_export_report_html(ticket, inv, project):
    ticket()
    out = inv.export_report_html(KEY)
    assert out["path"] == "report.html" and out["evidence_check"]["ok"] is True
    page = (project.ticket(KEY) / "report.html").read_text()
    assert page.startswith("<!doctype html>") and "<title>Investigation report: T-1</title>" in page
    assert "Evidence check passed · 3 citations verified" in page
    assert '<span class="del">-                return;</span>' in page
    assert '<span class="add">+                continue;</span>' in page
    assert f"<code>{SERVICE}:30</code>" in page
    assert "<h2>Root cause</h2>" in page and "<table>" in page
    assert "http://" not in page and "https://" not in page and "<script" not in page  # self-contained
    assert "not posted anywhere" in page


def test_html_export_flags_a_failing_report_and_escapes_html(ticket, inv, project):
    ticket(**{"None.\n": "<script>alert(1)</script> & co\n", "(E1)": "", "(E2)": ""})
    out = inv.export_report_html(KEY)
    page = (project.ticket(KEY) / "report.html").read_text()
    assert out["evidence_check"]["ok"] is False and "problem(s) to fix" in page
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in page
    with pytest.raises(WorkspaceError):
        inv.export_report_html(KEY, f"{ticket.saved}")


def test_markdown_subset():
    html = markdown_to_html("# T\n\n1. one\n2. two\n\n- a\n- **b** and *c*\n\n> quote\n\n---\n\n"
                            "| h |\n|---|\n| `x\\|y` |\n\n[link](https://example.com/a)\n<!-- hidden -->\n\ntext")
    assert "<ol>\n<li>one</li>\n<li>two</li>\n</ol>" in html
    assert "<li><strong>b</strong> and <em>c</em></li>" in html
    assert "<blockquote><p>quote</p></blockquote>" in html and "<hr>" in html
    assert '<a href="https://example.com/a">link</a>' in html
    assert "hidden" not in html and "<p>text</p>" in html
    assert markdown_to_html("> **Note.** one\n> two\n> three\n\nafter") == \
        "<blockquote><p><strong>Note.</strong> one two three</p></blockquote>\n<p>after</p>"


# --- past tickets -----------------------------------------------------------------------


def test_list_and_search_tickets(ticket, inv, project):
    ticket()
    (project.ticket(KEY) / "ticket.md").write_text("# T-1: Premium cases not High\n\nNightly import.\n")
    inv.init_ticket("T-2")
    listed = {t["ticket_key"]: t for t in inv.list_tickets()["tickets"]}
    assert listed[KEY]["title"] == "T-1: Premium cases not High"
    assert listed[KEY]["has_report"] is True and listed[KEY]["root_cause_status"] == "Confirmed"
    assert listed["T-2"]["has_report"] is False and listed["T-2"]["has_comment"] is False

    found = inv.search_tickets("nightly IMPORT")
    assert found["matches"] == [{"ticket_key": KEY, "file": "ticket.md", "line": 3, "text": "Nightly import."}]
    assert {m["file"] for m in inv.search_tickets("CaseEscalationService")["matches"]} == {"report.md"}
    assert inv.search_tickets("e", max_results=2)["truncated"] is True
    assert inv.search_tickets("never-seen-before")["match_count"] == 0
    for bad in ("", "(", "x" * 301):
        with pytest.raises(InputValidationError):
            inv.search_tickets(bad)


def test_wrapped_list_items_stay_one_item():
    html = markdown_to_html("- **First:** starts here\n  and continues on the next line.\n- Second\n\n1. One\n   still one.\n2. Two")
    assert "<li><strong>First:</strong> starts here and continues on the next line.</li>" in html
    assert "<li>One still one.</li>" in html and html.count("<p>") == 0
