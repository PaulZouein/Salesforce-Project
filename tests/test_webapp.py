"""The local web interface: guided set-up, ticket submission, runs, results and the org memory view.

The server runs in a thread on a free loopback port, against the simulated orgs. Claude Code is
replaced by a small stand-in program that speaks the same ``stream-json`` protocol.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
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
    assert "unattended" in prompt and "advise mode" in prompt and "not available here" in prompt
    assert "recall_org_knowledge" in prompt and "remember_org_fact" in prompt and "validate_report" in prompt
    assert ".claude/skills/<name>/SKILL.md" in prompt and "follow-up-NN.txt" in prompt
    assert "already been worked on" not in prompt

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
if mode == "no-mcp":
    out({"type": "system", "subtype": "init", "mcp_servers": [{"name": "sf-investigator", "status": "failed"}], "tools": []})
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "no tools"})
    sys.exit(1)
out({"type": "system", "subtype": "init", "mcp_servers": [{"name": "sf-investigator", "status": "connected"}],
     "model": os.environ.get("FAKE_CLAUDE_MODEL"),
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
if mode == "flood":
    for i in range(int(os.environ["FAKE_CLAUDE_FLOOD"])):
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "note %d" % i}]}})
if mode != "no-report":
    with open(os.path.join("tickets", "DEMO-101", "report.md"), "w", encoding="utf-8") as handle:
        handle.write("# Investigation report: DEMO-101\n\n## Root cause\n\n**Not confirmed.** Stand-in report.\n")
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

    from mcp_server import server
    call = json.loads(fake_claude.read_text(encoding="utf-8"))
    args = call["args"]
    assert args[:6] == ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
    # The prompt travels on standard input, never on the command line.
    assert "new ticket, DEMO-101" in call["prompt"] and "DEMO-101" not in " ".join(args)
    assert Path(call["cwd"]).resolve() == project.root.resolve()
    assert call["sf"] and "web-demo-state" in call["sf"]      # same (simulated) CLI as the page
    assert call["python"] == sys.executable                   # same Python for the MCP server
    # What the unattended session may use is spelled out: every read-only tool, and not one that changes an org.
    allowed = args[args.index("--allowedTools") + 1].split(",")
    refused = args[args.index("--disallowedTools") + 1].split(",")
    assert {agent.TOOL_PREFIX + name for name in server.READ_ONLY_TOOLS} <= set(allowed)
    for name in server.WRITE_TOOLS:
        assert agent.TOOL_PREFIX + name in refused and agent.TOOL_PREFIX + name not in allowed
    for builtin in ("Bash", "WebFetch", "WebSearch"):
        assert builtin in refused and builtin not in allowed
    assert not any("bypass" in a.lower() or "dangerously" in a.lower() for a in args)
    assert "--model" not in args and "--mcp-config" not in args   # no model chosen; this test project has no .mcp.json

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
    assert stored["status"] == "finished" and stored["events"] == 8


def test_unattended_command_attaches_only_the_projects_own_tool_server(project, fake_claude, tmp_path):
    """With the project's server file present, nothing else the user has connected is reachable."""
    (project.root / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    command = agent.build_command("claude", project.root, "sonnet")
    position = command.index("--mcp-config")
    assert command[position + 1] == str(project.root / ".mcp.json") and command[position + 2] == "--strict-mcp-config"
    assert command[-2:] == ["--model", "sonnet"]
    allowed, refused = agent.tool_permissions()
    assert not set(allowed) & set(refused)
    assert not [name for name in allowed if any(write in name for write in ORG_WRITE_NAMES)]


def test_model_choice_is_stored_with_the_ticket_and_passed_on(web, fake_claude, monkeypatch):
    status, made = submit(web, model="sonnet")
    assert status == 200 and made["ticket"]["model"] == "sonnet"
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    web.wait_until_finished("DEMO-101")
    args = json.loads(fake_claude.read_text(encoding="utf-8"))["args"]
    assert args[-2:] == ["--model", "sonnet"]
    # Changed when starting again; an empty choice goes back to Claude Code's default, or to the environment's.
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent", "model": ""})
    web.wait_until_finished("DEMO-101")
    assert "--model" not in json.loads(fake_claude.read_text(encoding="utf-8"))["args"]
    assert web.get("/api/tickets/DEMO-101")[1]["model"] is None
    monkeypatch.setenv("SFTI_CLAUDE_MODEL", "opus")
    assert web.get("/api/status")[1]["default_model"] == "opus"
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    web.wait_until_finished("DEMO-101")
    assert json.loads(fake_claude.read_text(encoding="utf-8"))["args"][-2:] == ["--model", "opus"]
    assert set(web.get("/api/status")[1]["models"]) == {"", "sonnet", "opus"}


@pytest.mark.parametrize("model", ["--dangerously-skip-permissions", "a b", "x" * 61, "son;net", "-p"])
def test_a_model_name_cannot_smuggle_an_option(web, model):
    status, data = submit(web, model=model)
    assert status == 400 and "model" in data["error"]["message"]
    assert web.get("/api/tickets")[1]["tickets"] == []
    assert agent.chosen_model({"model": model}) is None


def test_a_run_that_ends_without_a_report_is_not_called_finished(web, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no-report")
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    result = web.wait_until_finished("DEMO-101")
    assert result["run"]["status"] == "incomplete" and result["stage"] == "incomplete"
    last = web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"][-1]
    assert last["kind"] == "warn" and last["label"] == "It stopped before writing a report"
    assert "Add information" in last["detail"]


def test_a_very_long_run_keeps_its_newest_events_and_its_count(web, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "flood")
    monkeypatch.setenv("FAKE_CLAUDE_FLOOD", "40")
    monkeypatch.setattr(agent, "MAX_EVENTS", 25)
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    result = web.wait_until_finished("DEMO-101")
    events = web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"]
    assert result["run"]["events"] == 48 and len(events) == 25      # 8 as in a short run, plus 40 notes
    assert [e["n"] for e in events] == list(range(24, 49))          # numbering goes on; the oldest are dropped
    assert events[-1]["kind"] == "done"                             # the end of the run is what is kept
    assert web.get("/api/tickets/DEMO-101/events?after=46")[1]["events"][0]["n"] == 47


@pytest.mark.parametrize("mode,label,detail", [
    ("no-mcp", "The sf-investigator tools did not start", "Check again"),
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


def test_a_folder_never_opened_in_claude_code_still_works(web, fake_claude, monkeypatch):
    """Claude Code ignores the project's allow list until the folder was trusted once in a terminal. The page
    names the read-only tools on the command line instead, so that first-time step is not needed."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "untrusted")
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    result = web.wait_until_finished("DEMO-101", timeout=30)
    assert result["run"]["status"] == "finished"
    events = web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"]
    assert not [e for e in events if e["kind"] == "error"] and events[-1]["kind"] == "done"


def test_without_its_tools_the_run_is_stopped_at_once(web, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no-mcp")
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    web.wait_until_finished("DEMO-101")
    events = web.get("/api/tickets/DEMO-101/events?after=0")[1]["events"]
    # One reason, and nothing after it: no second, vaguer error from the process being stopped.
    assert [e["kind"] for e in events] == ["info", "error"] and "fatal" not in events[-1]


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
    status, second = web.post("/api/tickets/DEMO-101/run", {"runner": "agent", "note": "one more thing to check"})
    assert status == 400 and "already being investigated" in second["error"]["message"]   # no second run
    assert web.get("/api/tickets/DEMO-101")[1]["attachments"] == []                        # and its note is not kept
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


# --- adding information, attaching files, taking the result away ----------------------------


def test_follow_up_information_is_kept_and_the_next_run_continues_from_what_is_there(web, fake_claude, project):
    submit(web)
    status, saved = web.post("/api/tickets/DEMO-101/note", {"text": "  It also happens for cases created from email.  "})
    assert status == 200 and saved["saved"] == "attachments/follow-up-01.txt"
    note = (project.ticket("DEMO-101") / "attachments" / "follow-up-01.txt").read_text(encoding="utf-8")
    assert note.startswith("Added by the user on 20") and note.endswith("It also happens for cases created from email.\n")
    # Nothing was gathered yet, so the first run is still a first run.
    assert "already been worked on" not in web.get("/api/tickets/DEMO-101")[1]["prompt"]

    web.post("/api/tickets/DEMO-101/run", {"runner": "agent"})
    web.wait_until_finished("DEMO-101")
    status, _ = web.post("/api/tickets/DEMO-101/run", {"runner": "agent", "note": "The batch size is 200."})
    assert status == 200
    detail = web.wait_until_finished("DEMO-101")
    assert detail["attachments"] == ["follow-up-01.txt", "follow-up-02.txt"]
    prompt = json.loads(fake_claude.read_text(encoding="utf-8"))["prompt"]
    assert prompt.startswith("Ticket DEMO-101 was submitted through the web interface and has already been worked on.")
    assert "evidence_index" in prompt and "instead of starting again" in prompt and "follow-up-NN.txt" in prompt
    assert prompt == detail["prompt"]

    for body, fragment in (({"text": ""}, "Write what"), ({"text": "x" * 20_001}, "too long"), ({}, "Write what")):
        status, data = web.post("/api/tickets/DEMO-101/note", body)
        assert status == 400 and fragment in data["error"]["message"]
    assert web.post("/api/tickets/NOPE-1/note", {"text": "hello there"})[0] == 400


def test_files_can_be_attached_to_a_ticket(web, project):
    submit(web)
    picture = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()
    status, saved = web.post("/api/tickets/DEMO-101/attachments", {"name": "Error screen (1).PNG", "content_base64": picture})
    assert status == 200 and saved == {"ok": True, "saved": "attachments/Error-screen-1-.PNG", "bytes": 108}
    assert (project.ticket("DEMO-101") / "attachments" / "Error-screen-1-.PNG").read_bytes().startswith(b"\x89PNG")
    # A path in the name is reduced to the file name; nothing is written outside the ticket's attachments.
    status, saved = web.post("/api/tickets/DEMO-101/attachments",
                             {"name": "..\\..\\..\\sfti.config.json", "content_base64": base64.b64encode(b"{}").decode()})
    assert status == 200 and saved["saved"] == "attachments/sfti.config.json"
    assert not (project.root / "sfti.config.json").exists()
    status, saved = web.post("/api/tickets/DEMO-101/attachments",
                             {"name": "../../outside.txt", "content_base64": base64.b64encode(b"hello").decode()})
    assert saved["saved"] == "attachments/outside.txt" and not (project.root / "outside.txt").exists()
    assert web.get("/api/tickets/DEMO-101")[1]["attachments"] == ["Error-screen-1-.PNG", "outside.txt", "sfti.config.json"]

    text = base64.b64encode(b"hello").decode()
    for body, fragment in (
        ({"name": "tool.exe", "content_base64": text}, "not accepted"),
        ({"name": "page.html", "content_base64": text}, "not accepted"),
        ({"name": "no-extension", "content_base64": text}, "not accepted"),
        ({"name": "", "content_base64": text}, "no name"),
        ({"name": "empty.txt", "content_base64": ""}, "empty"),
        ({"name": "broken.txt", "content_base64": "not base64 !!"}, "could not be read"),
        ({"name": "ticket-original.txt", "content_base64": text}, "used by the investigator"),
        ({"name": "follow-up-07.txt", "content_base64": text}, "used by the investigator"),
    ):
        status, data = web.post("/api/tickets/DEMO-101/attachments", body)
        assert status == 400 and fragment in data["error"]["message"], body["name"]
    assert (project.ticket("DEMO-101") / "attachments" / "ticket-original.txt").read_text(encoding="utf-8") == TICKET_TEXT
    assert web.post("/api/tickets/DEMO-101/attachments", {"name": "a.txt", "content_base64": text}, token=False)[0] == 403


def test_attachment_size_and_count_are_limited(web, project, monkeypatch):
    submit(web)
    monkeypatch.setattr(tickets, "MAX_ATTACHMENT_BYTES", 1000)
    monkeypatch.setattr(tickets, "MAX_ATTACHMENTS", 2)
    big = base64.b64encode(b"x" * 1001).decode()
    status, data = web.post("/api/tickets/DEMO-101/attachments", {"name": "big.log", "content_base64": big})
    assert status == 400 and "too large" in data["error"]["message"]
    small = base64.b64encode(b"x").decode()
    assert web.post("/api/tickets/DEMO-101/attachments", {"name": "1.txt", "content_base64": small})[0] == 200
    assert web.post("/api/tickets/DEMO-101/attachments", {"name": "2.txt", "content_base64": small})[0] == 200
    status, data = web.post("/api/tickets/DEMO-101/attachments", {"name": "3.txt", "content_base64": small})
    assert status == 400 and "2 attached files at most" in data["error"]["message"]
    assert web.post("/api/tickets/DEMO-101/attachments", {"name": "2.txt", "content_base64": small})[0] == 200  # replace
    # The request itself is refused before it is read when it is larger than any accepted file could be.
    status, _, _ = web.request("POST", "/api/tickets/DEMO-101/attachments",
                               headers={"Content-Type": "application/json", "X-SFTI-Token": web.app.token,
                                        "Content-Length": "50000000"})
    assert status == 413


def test_the_result_can_be_downloaded_as_one_file(web):
    submit(web)
    assert web.request("GET", "/tickets/DEMO-101/result.zip")[0] == 404      # nothing to take away yet
    web.post("/api/tickets/DEMO-101/run", {"runner": "sample"})
    web.wait_until_finished("DEMO-101")
    status, headers, raw = web.request("GET", "/tickets/DEMO-101/result.zip")
    assert status == 200 and headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"] == 'attachment; filename="DEMO-101-result.zip"'
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert sorted(archive.namelist()) == ["DEMO-101/comment.md", "DEMO-101/fix.diff", "DEMO-101/report.html",
                                              "DEMO-101/report.md", "DEMO-101/ticket.md"]
        assert b"Root cause" in archive.read("DEMO-101/report.md")
    # The evidence, the retrieved source and the original ticket text stay on this computer.
    assert web.request("GET", "/tickets/..%2F..%2Fx/result.zip")[0] == 404
    assert web.request("GET", "/tickets/NOPE-1/result.zip")[0] == 404


# --- answers that take seconds are remembered for a moment ------------------------------------


def test_slow_views_are_remembered_briefly_and_asked_again_on_request(web, monkeypatch):
    inv = web.app.investigator()
    calls = {"orgs": 0, "info": 0}
    list_orgs, org_info = inv.list_orgs, inv.org_info

    def counted_list():
        calls["orgs"] += 1
        return list_orgs()

    def counted_info(alias):
        calls["info"] += 1
        return org_info(alias)

    monkeypatch.setattr(inv, "list_orgs", counted_list)
    monkeypatch.setattr(inv, "org_info", counted_info)
    for _ in range(3):
        assert len(web.get("/api/orgs")[1]["orgs"]) == 3
        assert web.get("/api/orgs/demo-prod")[1]["classification"] == "production"
    assert calls == {"orgs": 1, "info": 1}
    assert len(web.get("/api/orgs?refresh=1")[1]["orgs"]) == 3
    web.get("/api/orgs/demo-prod")
    assert calls == {"orgs": 2, "info": 2}           # "I have connected it" asks everything again
    # A failure is never remembered.
    assert web.get("/api/orgs/nobody")[0] == 502 and web.get("/api/orgs/nobody")[0] == 502
    assert calls["info"] == 4


def test_what_is_remembered_is_for_display_only(web):
    """The page's short memory must never decide a write: the guard asks the org itself each time."""
    import inspect
    from core import org_guard
    assert "ViewCache" not in inspect.getsource(org_guard) and "cache" not in inspect.getsource(org_guard.OrgGuard.authorize_write)
    web.get("/api/orgs/demo-prod")
    web.post("/api/demo", {"enabled": True, "reset": True})
    assert web.app.cache._items == {}                # switching orgs forgets everything


def test_org_memory_can_be_searched_from_the_page(web):
    submit(web)
    web.post("/api/tickets/DEMO-101/run", {"runner": "sample"})
    web.wait_until_finished("DEMO-101")
    org_id = web.get("/api/memory")[1]["orgs"][0]["org_id"]
    status, found = web.get(f"/api/memory/{org_id}/search?q=case%20priority%20premium")
    assert status == 200 and found["notes"] and found["components"]
    assert any("CaseEscalationService" in c["component"] for c in found["components"])
    status, nothing = web.get(f"/api/memory/{org_id}/search?q=zebra")
    assert status == 200 and nothing["notes"] == [] and nothing["components"] == []
    status, empty = web.get(f"/api/memory/{org_id}/search?q=")
    assert status == 400 and "few words" in empty["error"]["message"]


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
