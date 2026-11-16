"""The local web interface: guided set-up, ticket submission, runs, results and the org memory view.

The server runs in a thread on a free loopback port, against the simulated orgs. Claude Code is
replaced by a small stand-in program that speaks the same ``stream-json`` protocol.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.errors import InputValidationError
from simulator import demo
from webapp import agent, tickets
from webapp.server import App, create_server

ROOT = Path(__file__).resolve().parent.parent
TICKET_TEXT = demo.TICKET_TEXT.read_text(encoding="utf-8")


class Client:
    def __init__(self, app: App, port: int):
        self.app, self.base = app, f"http://127.0.0.1:{port}"

    def request(self, method: str, path: str, body=None, *, headers=None, token=True):
        data = None
        all_headers = dict(headers or {})
        if body is not None or method != "GET":
            data = json.dumps(body if body is not None else {}).encode()
            all_headers.setdefault("Content-Type", "application/json")
            if token:
                all_headers.setdefault("X-SFTI-Token", self.app.token)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def get(self, path, **kw):
        status, _, raw = self.request("GET", path, **kw)
        return status, json.loads(raw)

    def post(self, path, body=None, **kw):
        status, _, raw = self.request("POST", path, body if body is not None else {}, **kw)
        return status, json.loads(raw)

    def wait_until_finished(self, key: str, timeout: float = 120.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, detail = self.get(f"/api/tickets/{key}")
            if detail["stage"] != "running":
                return detail
            time.sleep(0.2)
        raise AssertionError("the run did not finish in time")


@pytest.fixture
def web(project, tmp_path, monkeypatch):
    monkeypatch.setenv("SFTI_SAMPLE_STEP_DELAY", "0")
    monkeypatch.delenv("SFTI_SF_EXECUTABLE", raising=False)
    monkeypatch.delenv("SFTI_CLAUDE_EXECUTABLE", raising=False)
    app = App(root=project.root, demo=True, demo_state=tmp_path / "web-demo-state")
    server = create_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(app, server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def submit(web, text=TICKET_TEXT, **extra):
    return web.post("/api/tickets", {"text": text, "org": "demo-prod", "sandbox": "demo-sandbox", **extra})


# --- page, status, set-up -----------------------------------------------------------------


def test_page_is_served_with_the_token_and_safe_headers(web):
    status, headers, raw = web.request("GET", "/")
    page = raw.decode()
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert web.app.token in page and "__SFTI_TOKEN__" not in page
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    # Self-contained: nothing is loaded from another host.
    assert not re.search(r"""(src|href)=["']https?://""", page)


def test_status_and_prerequisites(web):
    status, data = web.get("/api/status")
    assert status == 200 and data["demo"] is True and data["mode"] == "advise"
    assert data["jira"]["enabled"] is False and data["sample_ticket_key"] == "DEMO-101"
    _, pre = web.get("/api/prereqs")
    names = [c["name"] for c in pre["checks"]]
    assert names[:4] == ["Python", "git", "Salesforce CLI (sf)", "mcp Python package"] and "Claude Code" in names
    assert pre["ok"] is True and pre["os"] in ("windows", "macos", "linux")
    assert "simulated" in next(c for c in pre["checks"] if c["name"].startswith("Salesforce CLI"))["detail"]


def test_missing_tools_come_with_exact_fix_commands(project, tmp_path, monkeypatch):
    monkeypatch.setenv("SFTI_SF_EXECUTABLE", str(tmp_path / "no-sf"))
    monkeypatch.setenv("SFTI_CLAUDE_EXECUTABLE", str(tmp_path / "no-claude"))
    pre = App(root=project.root).prerequisites()
    cli = next(c for c in pre["checks"] if c["name"] == "Salesforce CLI (sf)")
    assert cli["ok"] is False and cli["fix"][0] == "npm install --global @salesforce/cli"
    claude = next(c for c in pre["checks"] if c["name"] == "Claude Code")
    assert claude["ok"] is False and claude["required"] is False and "claude-code" in claude["fix"][0]
    assert pre["ok"] is False


def test_orgs_are_listed_and_classified_without_secrets(web):
    _, data = web.get("/api/orgs")
    assert sorted(o["alias"] for o in data["orgs"]) == ["demo-dev", "demo-prod", "demo-sandbox"]
    _, prod = web.get("/api/orgs/demo-prod")
    assert prod["classification"] == "production" and prod["writes_allowed"] is False
    assert "cannot be overridden" in prod["what_is_allowed"]
    _, sandbox = web.get("/api/orgs/demo-sandbox")
    assert sandbox["classification"] == "sandbox"
    status, unknown = web.get("/api/orgs/nobody")
    assert status == 502 and unknown["ok"] is False and "hint" in unknown["error"]
    assert "SIMULATED-TOKEN" not in json.dumps([data, prod, sandbox])


def test_demo_mode_can_be_switched_and_reset(web):
    assert web.post("/api/demo", {"enabled": False})[1]["demo"] is False
    assert web.get("/api/status")[1]["demo"] is False
    assert web.post("/api/demo", {"enabled": True, "reset": True})[1]["demo"] is True
    assert len(web.get("/api/orgs")[1]["orgs"]) == 3


# --- the server only talks to this computer -------------------------------------------------


def test_requests_for_another_host_name_are_refused(web):
    status, _, _ = web.request("GET", "/api/status", headers={"Host": "evil.example.com"})
    assert status == 403
    status, _, _ = web.request("GET", "/", headers={"Host": "attacker.test:8765"})
    assert status == 403


def test_changes_need_the_page_token_and_a_local_origin(web):
    status, data = web.post("/api/tickets", {"text": TICKET_TEXT, "org": "demo-prod"}, token=False)
    assert status == 403 and "token" in data["error"]["message"]
    status, _ = web.post("/api/tickets", {"text": TICKET_TEXT, "org": "demo-prod"}, headers={"X-SFTI-Token": "wrong"})
    assert status == 403
    status, _ = web.post("/api/tickets", {"text": TICKET_TEXT, "org": "demo-prod"},
                         headers={"Origin": "https://evil.example.com"})
    assert status == 403
    assert web.get("/api/tickets")[1]["tickets"] == []


def test_server_listens_on_loopback_only(web, project):
    server = create_server(App(root=project.root, demo=False), 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    import inspect
    assert "host" not in inspect.signature(create_server).parameters


def test_bad_requests(web):
    assert web.get("/api/nothing")[0] == 404
    assert web.post("/api/nothing")[0] == 404
    status, _, _ = web.request("POST", "/api/tickets", headers={"Content-Type": "application/json",
                                                               "X-SFTI-Token": web.app.token,
                                                               "Content-Length": "5000000"})
    assert status == 413
    for key in ("..%2F..%2Fetc", "a%2Fb", "%2E%2E"):
        assert web.get(f"/api/tickets/{key}")[0] in (400, 404)
    assert web.get("/api/tickets/NOPE-1")[0] == 400


# --- tickets ------------------------------------------------------------------------------


def test_key_and_title_detection():
    assert tickets.detect_key(TICKET_TEXT) == "DEMO-101"
    assert tickets.detect_key("Ticket: abc-42\nSomething is broken in billing") == "ABC-42"
    assert tickets.detect_key("see SUP-7 and OPS-9") == "SUP-7"
    assert tickets.detect_key("nothing that looks like a key") is None
    assert tickets.detect_title(TICKET_TEXT) == "Premium customer cases are not set to High priority"
    assert tickets.detect_title("Subject: mail jane.doe@customer.test bounced\n") == "mail [email] bounced"


def test_submit_a_ticket(web, project):
    status, made = submit(web)
    assert status == 200 and made["ticket"]["key"] == "DEMO-101"
    assert made["ticket"]["org"] == "demo-prod" and made["ticket"]["sandbox"] == "demo-sandbox"
    folder = project.ticket("DEMO-101")
    assert (folder / "attachments" / "ticket-original.txt").read_text(encoding="utf-8") == TICKET_TEXT
    prompt = made["prompt"]
    assert "tickets/DEMO-101/attachments/ticket-original.txt" in prompt and "demo-prod" in prompt
    assert "unattended" in prompt and "advise mode" in prompt and "will be refused" in prompt
    assert "recall_org_knowledge" in prompt and "remember_org_fact" in prompt and "validate_report" in prompt

    _, detail = web.get("/api/tickets/DEMO-101")
    assert detail["stage"] == "new" and detail["title"] == "Premium customer cases are not set to High priority"
    assert [s["done"] for s in detail["steps"]] == [True, False, False, False, False, False, False]
    assert detail["report_html"] is None and detail["original_text"] == TICKET_TEXT and detail["prompt"] == prompt
    _, listing = web.get("/api/tickets")
    assert [t["key"] for t in listing["tickets"]] == ["DEMO-101"]

    status, again = submit(web)
    assert status == 400 and "already exists" in again["error"]["message"]


@pytest.mark.parametrize("body,fragment", [
    ({"text": "too short", "org": "demo-prod"}, "whole ticket"),
    ({"text": "A long enough description of a problem without any identifier in it at all.", "org": "demo-prod"},
     "No ticket key"),
    ({"text": TICKET_TEXT, "org": ""}, "Choose the org"),
    ({"text": TICKET_TEXT, "org": "demo-prod", "key": "../../etc"}, "Invalid ticket key"),
    ({"text": "x" * 200_001, "org": "demo-prod", "key": "BIG-1"}, "too long"),
    ({"org": "demo-prod"}, "whole ticket"),
])
def test_submit_validation(web, body, fragment):
    status, data = web.post("/api/tickets", body)
    assert status == 400 and fragment in data["error"]["message"]
    assert web.get("/api/tickets")[1]["tickets"] == []


def test_explicit_key_and_delete(web, project):
    status, made = submit(web, "The billing page shows the wrong total for invoices created last week.", key="bill-9")
    assert status == 200 and made["ticket"]["key"] == "BILL-9"
    assert project.ticket("BILL-9").is_dir()
    status, _, raw = web.request("DELETE", "/api/tickets/BILL-9", token=False)
    assert status == 403 and project.ticket("BILL-9").is_dir()
    status, _, raw = web.request("DELETE", "/api/tickets/BILL-9")
    assert status == 200 and json.loads(raw)["deleted"] == "BILL-9"
    assert not project.ticket("BILL-9").exists()
    assert web.request("DELETE", "/api/tickets/BILL-9")[0] == 400


# --- the recorded sample, end to end ------------------------------------------------------


def test_sample_run_produces_the_complete_result(web, project):
    submit(web)
    before = {alias: hash_org(web, alias) for alias in ("demo-prod", "demo-sandbox")}
    status, started = web.post("/api/tickets/DEMO-101/run", {"runner": "sample"})
    assert status == 200 and started["run"]["status"] == "running"
    detail = web.wait_until_finished("DEMO-101")

    assert detail["stage"] == "done" and detail["run"]["status"] == "finished" and detail["run"]["runner"] == "sample"
    assert detail["root_cause_status"] == "Confirmed"
    assert detail["evidence_check"]["ok"] is True and detail["evidence_check"]["citations"] == {"valid": 23, "broken": 0}
    assert all(step["done"] for step in detail["steps"])
    assert "stops processing the whole batch" in detail["root_cause_summary"]
    assert "<code>return;</code>" in detail["root_cause_summary_html"]
    assert "Evidence chain" in detail["root_cause_html"]
    assert '<span class="del">-                return;</span>' in detail["fix_html"]
    assert "-                return;\n+                continue;" in detail["fix_diff"]
    assert detail["comment_markdown"].startswith("<!-- Draft for DEMO-101")
    assert detail["has_report_html"] is True and len(detail["evidence"]) >= 9
    assert "<script" not in detail["report_html"]

    _, events = web.get("/api/tickets/DEMO-101/events?after=0")
    labels = [e["label"] for e in events["events"]]
    assert labels[0] == "Recorded sample" and labels[-1] == "Investigation finished"
    assert "Asking the org memory what is already known" in labels
    assert events["events"][-1]["kind"] == "done" and "23 citations verified" in events["events"][-1]["detail"]
    assert [e["n"] for e in events["events"]] == list(range(1, len(labels) + 1))
    assert web.get("/api/tickets/DEMO-101/events?after=5")[1]["events"][0]["n"] == 6

    status, headers, raw = web.request("GET", "/tickets/DEMO-101/report.html")
    assert status == 200 and b"Evidence check passed" in raw
    # Read-only from start to finish: neither simulated org changed.
    assert {alias: hash_org(web, alias) for alias in ("demo-prod", "demo-sandbox")} == before
    assert web.app.investigator().audit.entries() == [] or \
        all(e["operation"] == "run_apex_tests" for e in web.app.investigator().audit.entries())


def hash_org(web, alias: str) -> str:
    import hashlib
    base = web.app._demo_state / "orgs" / alias
    digest = hashlib.sha256()
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        digest.update(path.relative_to(base).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def test_sample_fills_the_org_memory_and_the_second_ticket_starts_from_it(web):
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "sample"})
    web.wait_until_finished("DEMO-101")

    _, known = web.get("/api/memory")
    assert len(known["orgs"]) == 1 and known["orgs"][0]["aliases"] == ["demo-prod"]
    org_id = known["orgs"][0]["org_id"]
    _, org_map = web.get(f"/api/memory/{org_id}")
    assert org_map["counts"]["components"] == 5 and org_map["counts"]["notes"] == 4
    assert org_map["objects"]["Case"]["triggers"] == ["CaseTrigger"] and org_map["tickets"] == ["DEMO-101"]
    status, verified = web.post(f"/api/memory/{org_id}/verify")
    assert status == 200 and verified["counts"] == {"unchanged": 5} and verified["stale_notes"] == []

    # A later ticket about something related finds it, already double-checked.
    found = web.app.investigator().recall_org_knowledge("demo-prod", "notification not sent for high priority case")
    assert any("Case_Premium_Notification" in n["text"] for n in found["notes"])
    assert found["freshness"]["checked"] is True
    # Replaying does not duplicate the remembered facts.
    web.post("/api/tickets/DEMO-101/run", {"runner": "sample"})
    web.wait_until_finished("DEMO-101")
    assert web.get(f"/api/memory/{org_id}")[1]["counts"]["notes"] == 4
    assert web.get("/api/memory/00DWs000000NopeMAS")[1]["counts"]["components"] == 0
    assert web.post("/api/memory/00DWs000000NopeMAS/verify")[0] == 400


def test_sample_is_only_offered_for_the_sample_ticket_on_demo_orgs(web):
    submit(web, "The billing page shows the wrong total for invoices created last week.", key="BILL-9")
    status, data = web.post("/api/tickets/BILL-9/run", {"runner": "sample"})
    assert status == 400 and "DEMO-101" in data["error"]["message"]
    assert web.post("/api/tickets/BILL-9/run", {"runner": "magic"})[0] == 400
    submit(web)
    web.post("/api/demo", {"enabled": False})
    assert web.post("/api/tickets/DEMO-101/run", {"runner": "sample"})[0] == 400


# --- Claude Code runs ---------------------------------------------------------------------

FAKE_CLAUDE = r'''
import json, os, sys, time
args = sys.argv[1:]
record = os.environ["FAKE_CLAUDE_RECORD"]
prompt = sys.stdin.read()
open(record, "w", encoding="utf-8").write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(),
    "sf": os.environ.get("SFTI_SF_EXECUTABLE"), "python": os.environ.get("SFTI_PYTHON")}))
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
if mode == "untrusted":
    sys.stderr.write("Ignoring 37 permissions.allow entries from .claude/settings.json: this workspace "
                     "has not been trusted. Run Claude Code interactively here once.\n")
    sys.stderr.flush()
    time.sleep(120)
if mode == "no-mcp":
    out({"type": "system", "subtype": "init", "mcp_servers": [{"name": "sf-investigator", "status": "failed"}], "tools": []})
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "no tools"})
    sys.exit(1)
out({"type": "system", "subtype": "init", "mcp_servers": [{"name": "sf-investigator", "status": "connected"}],
     "tools": ["Read", "mcp__sf-investigator__soql_query", "mcp__sf-investigator__org_info"]})
print("this line is not JSON and must be ignored")
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the ticket."},
     {"type": "tool_use", "name": "mcp__sf-investigator__org_info", "input": {"org": "demo-prod"}}]}})
out({"type": "assistant", "message": {"content": [
     {"type": "tool_use", "name": "mcp__sf-investigator__soql_query", "input": {"org": "demo-prod", "query": "SELECT Id FROM Case"}},
     {"type": "tool_use", "name": "Write", "input": {"file_path": "tickets/DEMO-101/report.md"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
     "content": [{"type": "text", "text": "Permission to use deploy tool was denied"}]}]}})
if mode == "hang":
    time.sleep(120)
if mode == "crash":
    sys.stderr.write("boom: something broke\n"); sys.exit(3)
out({"type": "result", "subtype": "success", "is_error": False, "result": "Root cause confirmed.", "num_turns": 12,
     "duration_ms": 183000, "total_cost_usd": 1.23})
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    record = tmp_path / "claude-call.json"
    if os.name == "nt":
        launcher = tmp_path / "claude.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("SFTI_CLAUDE_EXECUTABLE", str(launcher))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    return record


def test_agent_run_starts_claude_code_unattended_and_reports_progress(web, fake_claude, project):
    submit(web)
    status, started = web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    assert status == 200
    detail = web.wait_until_finished("DEMO-101")
    assert detail["run"]["status"] == "finished" and detail["run"]["runner"] == "agent"

    call = json.loads(fake_claude.read_text(encoding="utf-8"))
    assert call["args"] == ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
    # The prompt travels on standard input, never on the command line.
    assert call["prompt"] == detail["prompt"] and "DEMO-101" not in " ".join(call["args"])
    assert Path(call["cwd"]).resolve() == project.root.resolve()
    assert call["sf"] and "web-demo-state" in call["sf"]      # same (simulated) CLI as the page
    assert call["python"] == sys.executable                   # same Python for the MCP server
    # Nothing in the command grants approval for org writes.
    assert not any("bypass" in a.lower() or "dangerously" in a.lower() or "allowedTools" in a for a in call["args"])

    events = web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"]
    assert [(e["kind"], e["label"]) for e in events] == [
        ("info", "Starting Claude Code"),
        ("info", "Investigator started"),
        ("note", "Reading the ticket."),
        ("tool", "Checking whether the org is production"),
        ("tool", "Querying records"),
        ("tool", "Writing a file"),
        ("warn", "A step was refused or failed"),
        ("done", "Investigation finished"),
    ]
    assert events[1]["detail"] == "2 Salesforce tools available"
    assert events[4]["detail"] == "SELECT Id FROM Case" and events[5]["detail"] == "tickets/DEMO-101/report.md"
    assert events[-1]["turns"] == 12 and events[-1]["duration_seconds"] == 183
    stored = json.loads((project.ticket("DEMO-101") / "run.json").read_text())
    assert stored["status"] == "finished"


@pytest.mark.parametrize("mode,label,detail", [
    ("no-mcp", "The sf-investigator tools did not start", "SFTI_PYTHON"),
    ("crash", "Claude Code ended without finishing", "boom"),
])
def test_agent_failures_are_explained(web, fake_claude, monkeypatch, mode, label, detail):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    result = web.wait_until_finished("DEMO-101")
    assert result["stage"] == "failed" and result["run"]["status"] == "failed"
    errors = [e for e in web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"] if e["kind"] == "error"]
    assert errors[0]["label"] == label and detail in errors[0]["detail"]


def test_untrusted_folder_is_detected_and_explained_instead_of_running_without_tools(web, fake_claude, monkeypatch):
    """Claude Code ignores the project's allow list until the folder was trusted once interactively."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "untrusted")
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    result = web.wait_until_finished("DEMO-101", timeout=30)
    assert result["stage"] == "failed"
    errors = [e for e in web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"] if e["kind"] == "error"]
    assert len(errors) == 1 and errors[0]["label"] == "Claude Code has not been used in this folder yet"
    assert "run 'claude' once" in errors[0]["detail"] and "trusting the folder" in errors[0]["detail"]


def test_agent_run_without_claude_code_explains_the_manual_path(web, tmp_path, monkeypatch):
    monkeypatch.setenv("SFTI_CLAUDE_EXECUTABLE", "")
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    result = web.wait_until_finished("DEMO-101")
    event = web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"][-1]
    assert result["stage"] == "failed" and "not found" in event["label"] and "paste the prompt" in event["detail"]


def test_a_running_investigation_can_be_stopped(web, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    deadline = time.time() + 30
    while time.time() < deadline and len(web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"]) < 4:
        time.sleep(0.1)
    assert web.get("/api/tickets/DEMO-101")[1]["stage"] == "running"
    assert web.request("DELETE", "/api/tickets/DEMO-101")[0] == 400     # not while it runs
    assert web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})[1]["run"]["status"] == "running"  # no second run
    assert web.post("/api/tickets/DEMO-101/cancel")[1]["cancelled"] is True
    result = web.wait_until_finished("DEMO-101")
    assert result["run"]["status"] == "cancelled" and result["stage"] == "cancelled"
    assert web.post("/api/tickets/DEMO-101/cancel")[1]["cancelled"] is False


def test_time_limit_stops_a_run(project, fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    app = App(root=project.root, demo=True, demo_state=tmp_path / "limit-state")
    app.runner._timeout = 1.5
    tickets.create_ticket(app.investigator(), TICKET_TEXT, "demo-prod")
    run = app.runner.start("DEMO-101", "agent")
    deadline = time.time() + 30
    while run.status == "running" and time.time() < deadline:
        time.sleep(0.1)
    # The reason is recorded before the status changes, so it is there as soon as the run reads "failed".
    assert run.status == "failed" and any("time limit" in e["label"] for e in run.events)


def test_interrupted_run_is_reported_after_a_restart(project, tmp_path):
    app = App(root=project.root, demo=True, demo_state=tmp_path / "restart-state")
    tickets.create_ticket(app.investigator(), TICKET_TEXT, "demo-prod")
    folder = project.ticket("DEMO-101")
    (folder / "run.json").write_text(json.dumps({"status": "running", "runner": "agent"}))
    status = tickets.ticket_status(app.investigator(), "DEMO-101", app.runner.summary("DEMO-101", folder))
    assert status["stage"] == "interrupted"


@pytest.mark.parametrize("line", ["", "   ", "not json", "[1,2]", '"text"', '{"type": "unknown"}',
                                  '{"type": "assistant"}', '{"type": "assistant", "message": {"content": "x"}}',
                                  '{"type": "user", "message": null}'])
def test_stream_lines_that_carry_nothing_are_ignored(line):
    assert agent.events_from_stream_line(line) == []


def test_tool_labels_cover_every_tool_of_the_server():
    from mcp_server import server
    assert set(server.READ_ONLY_TOOLS) <= set(agent.TOOL_LABELS), set(server.READ_ONLY_TOOLS) - set(agent.TOOL_LABELS)
    event = agent.events_from_stream_line(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__sf-investigator__brand_new_tool", "input": {}}]}}))[0]
    assert event["label"] == "Brand new tool"


# --- results picked up however the investigation ran --------------------------------------


def test_status_follows_the_files_when_claude_code_is_used_directly(web, project):
    """Run by hand in Claude Code: the page shows the result because it reads the ticket folder."""
    submit(web)
    inv = web.app.investigator()
    folder = project.ticket("DEMO-101")
    assert web.get("/api/tickets/DEMO-101")[1]["stage"] == "new"
    demo.gather_evidence(inv, folder)
    middle = web.get("/api/tickets/DEMO-101")[1]
    assert middle["stage"] == "in_progress" and middle["steps"][2]["done"] and middle["report_html"] is None
    demo.install_expected(folder)
    done = web.get("/api/tickets/DEMO-101")[1]
    assert done["stage"] == "done" and done["root_cause_status"] == "Confirmed" and done["run"] is None
    assert done["ticket_html"].startswith("<h1>DEMO-101")


def test_status_survives_reading_a_file_while_it_is_being_rewritten(web, project):
    submit(web)
    folder = project.ticket("DEMO-101")
    for content in ("", "   \n", "\n\n# DEMO-101: title on a later line\n"):
        (folder / "ticket.md").write_text(content)
        status, detail = web.get("/api/tickets/DEMO-101")
        assert status == 200 and "stage" in detail
    (folder / "report.md").write_text("")
    (folder / "comment.md").write_text("")
    status, detail = web.get("/api/tickets/DEMO-101")
    assert status == 200 and detail["evidence_check"]["ok"] is False


def test_a_report_that_fails_the_evidence_check_is_shown_as_failing(web, project):
    submit(web)
    inv = web.app.investigator()
    demo.gather_evidence(inv, project.ticket("DEMO-101"))
    demo.install_expected(project.ticket("DEMO-101"))
    report = project.ticket("DEMO-101") / "report.md"
    report.write_text(report.read_text().replace("CaseEscalationService.cls:26-33", "CaseEscalationService.cls:260-330"))
    detail = web.get("/api/tickets/DEMO-101")[1]
    assert detail["evidence_check"]["ok"] is False
    assert "line out of range" in detail["evidence_check"]["problems"][0]
    assert detail["steps"][4]["done"] is False and "problem" in detail["steps"][4]["detail"]


def test_results_are_rendered_as_escaped_html(web, project):
    submit(web, "Key: XSS-1\nTitle: <img src=x onerror=alert(1)>\n\nThe page shows <script>alert(1)</script> to users.")
    folder = project.ticket("XSS-1")
    (folder / "ticket.md").write_text("# XSS-1: <img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n")
    detail = web.get("/api/tickets/XSS-1")[1]
    assert "<script>" not in detail["ticket_html"] and "<img" not in detail["ticket_html"]
    assert "&lt;script&gt;" in detail["ticket_html"]


# --- the interface cannot change an org ---------------------------------------------------

ORG_WRITE_NAMES = ("deploy_component", "rollback_change", "update_records", "execute_anonymous_apex",
                   "set_trace_flag", "authorize_write", "authorize_test_run", "_issue_permit", "rest_request",
                   "run_apex_tests_on_production")


def test_interface_code_never_calls_an_org_write():
    for path in (ROOT / "webapp").rglob("*"):
        if path.suffix in (".py", ".html"):
            text = path.read_text(encoding="utf-8")
            for name in ORG_WRITE_NAMES:
                assert name not in text, f"{path.relative_to(ROOT)} mentions {name}"
    replay = (ROOT / "simulator" / "demo.py").read_text(encoding="utf-8")
    for name in ORG_WRITE_NAMES:
        assert name not in replay


def test_no_route_reaches_a_write(web):
    submit(web)
    for path in ("/api/tickets/DEMO-101/deploy", "/api/tickets/DEMO-101/apply", "/api/deploy", "/api/orgs/demo-prod",
                 "/api/tickets/DEMO-101/rollback", "/api/update_records", "/api/config", "/api/mode"):
        assert web.post(path, {"org": "demo-sandbox"})[0] == 404, path
    assert web.get("/api/status")[1]["mode"] == "advise"


def test_jira_route_is_off_by_default(web):
    status, data = web.get("/api/jira/ABC-1")
    assert status == 502 and data["error"]["code"] == "jira_disabled"


def test_create_ticket_function_validates_too(project, tmp_path):
    app = App(root=project.root, demo=True, demo_state=tmp_path / "direct-state")
    with pytest.raises(InputValidationError):
        tickets.create_ticket(app.investigator(), TICKET_TEXT, "   ")
