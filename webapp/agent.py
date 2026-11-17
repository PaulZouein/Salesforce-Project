"""Running an investigation for a ticket, and reporting its progress as plain events.

Two runners:

* ``agent``: starts Claude Code without a terminal (``claude -p``) in the project folder, with the
  prompt on standard input. Claude Code loads ``CLAUDE.md`` and the skills as in an interactive
  session. The command line narrows what that unattended session can reach:

  - only the project's own MCP server is attached (``--strict-mcp-config``), so connectors the
    user has for other purposes are out of reach;
  - the read-only investigator tools are allowed by name, taken from the server's own list;
  - the tools that change an org are removed altogether, together with web access. Nobody is
    there to approve a write, so none can happen;
  - file edits are accepted so that it can write the ticket folder. The project's permission
    rules still deny edits to the safety files.

  With **Use fewer tokens** the run also uses Sonnet (unless a model was chosen), medium effort,
  no subagents and no skill listing, and is told to economize. A spending limit, when set, is
  passed to Claude Code (``--max-budget-usd``), which stops the run when it is reached.
* ``sample``: replays the recorded DEMO-101 investigation against the simulated org, without the
  AI agent (see ``simulator.demo``).

Events are kept in memory and appended to ``tickets/<KEY>/run-events.jsonl``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.app import Investigator

from .tickets import MODEL_RE, agent_prompt, has_progress, read_request, utc_now
from .usage import UsageBook, UsageMeter

TOOL_PREFIX = "mcp__sf-investigator__"
DEFAULT_TIMEOUT_SECONDS = 45 * 60
MAX_EVENTS = 2000

#: Claude Code's own tools that an unattended investigation may use without asking. No shell.
BUILTIN_ALLOWED = ("Read", "Glob", "Grep", "Skill", "Task", "Agent", "TodoWrite", "ToolSearch")
#: Removed from an unattended investigation, on top of the org write tools: no shell, no web.
BUILTIN_REFUSED = ("Bash", "WebFetch", "WebSearch")
#: Model names offered on the page. Empty means "whatever Claude Code uses by default".
MODEL_CHOICES = {"": "Claude Code's default", "sonnet": "Sonnet: faster, uses less", "opus": "Opus: most thorough"}
#: What "Use fewer tokens" changes. Each item is a command-line option; see build_command.
ECONOMY_MODEL = "sonnet"
ECONOMY_EFFORT = "medium"
ECONOMY_REFUSED = ("Task", "Agent")  # subagents start a fresh context and re-read what the main run has read

#: What each tool call means, in words a non-developer can follow.
TOOL_LABELS = {
    "get_config": "Reading the settings",
    "list_orgs": "Listing the connected orgs",
    "org_info": "Checking whether the org is production",
    "init_ticket": "Creating the ticket folder",
    "list_tickets": "Listing earlier tickets",
    "search_tickets": "Looking for earlier tickets on the same subject",
    "jira_get_ticket": "Reading the ticket from Jira",
    "recall_org_knowledge": "Asking the org memory what is already known",
    "remember_org_fact": "Saving what was learned about the org",
    "forget_org_fact": "Removing a note that no longer holds",
    "org_map": "Reading the org map",
    "verify_org_knowledge": "Checking the org memory against the org",
    "retrieve_components": "Retrieving source from the org",
    "list_files": "Listing retrieved files",
    "search_metadata": "Searching the retrieved source",
    "read_file": "Reading source",
    "summarize_flow": "Outlining a flow",
    "scan_apex": "Scanning Apex for bug-prone patterns",
    "workspace_diff": "Producing the fix as a diff",
    "soql_query": "Querying records",
    "tooling_query": "Querying setup data",
    "describe_object": "Looking at an object and its automation",
    "find_dependencies": "Finding what uses a component",
    "recent_org_changes": "Checking what changed in the org recently",
    "record_history": "Reading a record's field history",
    "list_debug_logs": "Listing debug logs",
    "get_debug_log": "Downloading a debug log",
    "analyze_debug_log": "Analysing a debug log",
    "run_apex_tests": "Running Apex tests",
    "list_changes": "Listing applied changes",
    "audit_log": "Reading the audit log",
    "write_template": "Starting the report",
    "evidence_index": "Listing the evidence collected",
    "validate_report": "Checking the report's evidence",
    "validate_comment": "Checking the draft comment",
    "export_report_html": "Exporting the report",
}
BUILTIN_LABELS = {
    "Read": "Reading a file", "Write": "Writing a file", "Edit": "Editing a file", "Glob": "Looking for files",
    "Grep": "Searching files", "Skill": "Following a skill", "Agent": "Tracing the execution path",
    "Task": "Tracing the execution path", "TodoWrite": "Planning", "Bash": "Running a command",
    "ToolSearch": "Loading tools",
}


def tool_permissions(*, economy: bool = False) -> tuple[list[str], list[str]]:
    """``(allowed, refused)`` tool names for an unattended investigation.

    Both come from the MCP server's own registry, so a tool that changes an org can never be in
    the allowed list: every write tool is in the refused one.
    """
    from mcp_server.server import READ_ONLY_TOOLS, WRITE_TOOLS

    builtin_refused = list(BUILTIN_REFUSED) + (list(ECONOMY_REFUSED) if economy else [])
    allowed = [TOOL_PREFIX + name for name in READ_ONLY_TOOLS if name not in WRITE_TOOLS] + \
        [name for name in BUILTIN_ALLOWED if name not in builtin_refused]
    refused = [TOOL_PREFIX + name for name in WRITE_TOOLS] + builtin_refused
    return allowed, refused


def chosen_model(request: dict[str, Any]) -> str | None:
    """The model for this run: the ticket's choice, else ``SFTI_CLAUDE_MODEL``, else Sonnet when the ticket
    asks to use fewer tokens, else Claude Code's default."""
    value = str(request.get("model") or os.environ.get("SFTI_CLAUDE_MODEL") or "").strip()
    if not value and request.get("economy"):
        value = ECONOMY_MODEL
    return value if value and MODEL_RE.match(value) else None


def build_command(executable: str, root: Path, model: str | None = None, *, economy: bool = False,
                  budget_usd: float | None = None) -> list[str]:
    """The exact command line of an unattended investigation. The module text says what each part is for."""
    allowed, refused = tool_permissions(economy=economy)
    command = [executable, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
    server_config = Path(root) / ".mcp.json"
    if server_config.is_file():
        command += ["--mcp-config", str(server_config), "--strict-mcp-config"]
    command += ["--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(refused)]
    if economy:
        # The investigator reads the project's step descriptions as files (the prompt names them), so the
        # list of every skill on the computer does not need to be in its context.
        command += ["--effort", ECONOMY_EFFORT, "--disable-slash-commands"]
    if budget_usd:
        command += ["--max-budget-usd", f"{float(budget_usd):.2f}"]
    if model:
        command += ["--model", model]
    return command


def find_claude() -> str | None:
    """Path of the Claude Code command, or ``None``."""
    return os.environ.get("SFTI_CLAUDE_EXECUTABLE") or shutil.which("claude")


def claude_version(executable: str | None = None) -> str | None:
    executable = executable or find_claude()
    if not executable:
        return None
    try:
        proc = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else None


def _describe_tool(name: str, tool_input: Any) -> dict[str, Any]:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name.startswith(TOOL_PREFIX):
        short = name[len(TOOL_PREFIX):]
        label = TOOL_LABELS.get(short, short.replace("_", " ").capitalize())
        detail = None
        for field in ("query", "pattern", "path", "object_name", "name", "log_id", "class_names", "components", "text"):
            if tool_input.get(field):
                value = tool_input[field]
                detail = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
                break
        return {"kind": "tool", "tool": short, "label": label, "detail": (detail or "")[:220] or None}
    label = BUILTIN_LABELS.get(name, name)
    detail = tool_input.get("file_path") or tool_input.get("skill") or tool_input.get("description") or \
        tool_input.get("pattern") or tool_input.get("command")
    return {"kind": "tool", "tool": name, "label": label, "detail": (str(detail)[:220] if detail else None)}


_SKILL_FILE_RE = re.compile(r"(?:^|[\\/])\.claude[\\/]skills[\\/]([\w-]+)[\\/]SKILL\.md$")


def tidy_event(event: dict[str, Any], root: Path) -> dict[str, Any]:
    """Make a progress line readable: paths inside the project lose the long prefix (which also names
    the user's home folder), and reading one of the project's own step descriptions is called that."""
    detail = event.get("detail")
    if event.get("kind") != "tool" or not isinstance(detail, str):
        return event
    for prefix in {str(root), root.as_posix()}:
        for separator in ("/", "\\"):
            detail = detail.replace(prefix + separator, "")
    skill = _SKILL_FILE_RE.search(detail)
    if skill and event.get("tool") == "Read":
        return {**event, "label": "Reading how to do this step", "detail": skill.group(1).replace("-", " ")}
    return {**event, "detail": detail}


def parse_stream_line(line: str) -> dict[str, Any] | None:
    """One line of Claude Code's ``stream-json`` output, or ``None`` if it is not a JSON object."""
    line = line.strip()
    if not line:
        return None
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def events_from_stream_line(line: str) -> list[dict[str, Any]]:
    """Turn one line of Claude Code's ``stream-json`` output into zero or more interface events."""
    message = parse_stream_line(line)
    return events_from_message(message) if message else []


def events_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The interface events of one stream message."""
    kind = message.get("type")
    events: list[dict[str, Any]] = []
    if kind == "system" and message.get("subtype") == "init":
        servers = message.get("mcp_servers") or []
        ours = next((s for s in servers if isinstance(s, dict) and s.get("name") == "sf-investigator"), None)
        if ours is None or ours.get("status") != "connected":
            events.append({"kind": "error", "fatal": True, "label": "The sf-investigator tools did not start",
                           "detail": "Claude Code could not connect to the project's MCP server. Open 'Get "
                                     "started' and press 'Check again': the project must be installed in the "
                                     "Python that runs this page (the start script does that)."})
        else:
            tools = [t for t in message.get("tools") or [] if str(t).startswith(TOOL_PREFIX)]
            events.append({"kind": "info", "label": "Investigator started",
                           "detail": f"{len(tools)} Salesforce tools available"
                                     + (f", model {message['model']}" if message.get("model") else "")})
    elif kind == "assistant":
        for block in ((message.get("message") or {}).get("content")) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                events.append(_describe_tool(str(block.get("name")), block.get("input")))
            elif block.get("type") == "text" and str(block.get("text", "")).strip():
                events.append({"kind": "note", "label": str(block["text"]).strip()[:600]})
    elif kind == "user":
        for block in ((message.get("message") or {}).get("content")) or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                events.append({"kind": "warn", "label": "A step was refused or failed", "detail": str(content)[:300]})
    elif kind == "result":
        ok = message.get("subtype") == "success" and not message.get("is_error")
        figures = {
            "turns": message.get("num_turns"),
            "duration_seconds": round(message.get("duration_ms", 0) / 1000) if message.get("duration_ms") else None,
            "cost_usd": message.get("total_cost_usd"),
        }
        if message.get("subtype") == "error_max_budget_usd":
            events.append({"kind": "warn", "label": "Stopped at your spending limit", "budget_stop": True,
                           "detail": "What it found so far is kept in the ticket folder. Raise the limit, or "
                                     "remove it, and start it again: it continues from where it stopped.",
                           **figures})
        else:
            events.append({
                "kind": "done" if ok else "error",
                "label": "Investigation finished" if ok else "The investigation stopped before finishing",
                "detail": (str(message.get("result") or "")[:1500] or None),
                **figures,
            })
    return events


class Run:
    """One investigation in progress or finished."""

    def __init__(self, key: str, runner: str, folder: Path, *, options: dict[str, Any] | None = None,
                 on_finish: Callable[["Run"], None] | None = None):
        self.key, self.runner, self.folder = key, runner, folder
        self.options = options or {}
        self.meter = UsageMeter()
        self._on_finish = on_finish
        self._finished_once = False
        self.status = "running"
        self.started_at = utc_now()
        self.finished_at: str | None = None
        self.events: list[dict[str, Any]] = []
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._count = 0
        self._log = folder / "run-events.jsonl"
        try:
            self._log.write_text("", encoding="utf-8")
        except OSError:
            pass

    def add(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._count += 1
            event = {"n": self._count, "at": utc_now(), **{k: v for k, v in event.items() if v is not None}}
            self.events.append(event)
            # A very long run keeps its newest events in memory (the end matters most); the file has all.
            del self.events[:-MAX_EVENTS]
            try:
                with self._log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event) + "\n")
            except OSError:
                pass

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = utc_now()
        try:
            (self.folder / "run.json").write_text(json.dumps(self.summary(), indent=2), encoding="utf-8")
        except OSError:
            pass
        if self._on_finish and not self._finished_once:
            self._finished_once = True
            try:
                self._on_finish(self)
            except Exception:  # noqa: BLE001 - keeping count must never break a run
                pass

    def summary(self) -> dict[str, Any]:
        last = self.events[-1] if self.events else None
        return {"status": self.status, "runner": self.runner, "started_at": self.started_at,
                "finished_at": self.finished_at, "events": self._count,
                "options": self.options or None,
                "usage": self.meter.snapshot() if self.runner == "agent" else None,
                "last": {k: last[k] for k in ("kind", "label", "detail") if k in last} if last else None}


def load_finished_run(folder: Path) -> dict[str, Any] | None:
    """The stored summary of an earlier run. A run that was still running when the server stopped is 'interrupted'."""
    try:
        data = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("status") == "running":
        data["status"] = "interrupted"
    return data if isinstance(data, dict) else None


def load_events(folder: Path) -> list[dict[str, Any]]:
    events = []
    try:
        for line in (folder / "run-events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return events[-MAX_EVENTS:]


class Runner:
    """Starts and tracks investigations. One per ticket at a time."""

    def __init__(self, root: Path, investigator: Callable[[], Investigator], child_env: Callable[[], dict[str, str]],
                 *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.root = Path(root)
        self._investigator = investigator
        self._child_env = child_env
        self._timeout = timeout
        self.runs: dict[str, Run] = {}
        self.book = UsageBook(self.root)

    def _record(self, run: Run) -> None:
        """Keep what a run used. Runs that never reached Claude (the recorded sample, a missing install) cost nothing."""
        if run.runner != "agent" or not run.meter.used:
            return
        self.book.record({"key": run.key, "runner": run.runner, "status": run.status, "started_at": run.started_at,
                          "finished_at": run.finished_at, **{k: v for k, v in run.options.items() if v is not None},
                          **{k: v for k, v in run.meter.snapshot().items() if k in ("tokens", "cost_usd", "basis",
                                                                                      "models")}})

    def summaries(self) -> dict[str, dict[str, Any]]:
        return {key: run.summary() for key, run in self.runs.items()}

    def summary(self, key: str, folder: Path) -> dict[str, Any] | None:
        if key in self.runs:
            return self.runs[key].summary()
        return load_finished_run(folder)

    def events(self, key: str, folder: Path, after: int = 0) -> list[dict[str, Any]]:
        events = self.runs[key].events if key in self.runs else load_events(folder)
        return [e for e in events if e.get("n", 0) > after]

    def is_running(self, key: str) -> bool:
        return key in self.runs and self.runs[key].status == "running"

    def start(self, key: str, runner: str) -> Run:
        if self.is_running(key):
            return self.runs[key]
        inv = self._investigator()
        folder = inv.workspace.ticket_dir(key)
        options = None
        if runner == "agent":
            request = read_request(folder)
            options = {"model": chosen_model(request), "economy": bool(request.get("economy")),
                       "budget_usd": request.get("budget_usd")}
        run = Run(key, runner, folder, options=options, on_finish=self._record)
        self.runs[key] = run
        (folder / "run.json").write_text(json.dumps(run.summary(), indent=2), encoding="utf-8")
        target = self._run_sample if runner == "sample" else self._run_agent
        threading.Thread(target=target, args=(run, inv), name=f"run-{key}", daemon=True).start()
        return run

    def cancel(self, key: str) -> bool:
        run = self.runs.get(key)
        if not run or run.status != "running":
            return False
        run.add({"kind": "warn", "label": "Stopped by you"})
        run.status = "cancelled"
        if run.process and run.process.poll() is None:
            from core.sf_cli import _kill_tree
            _kill_tree(run.process)
        run.finish("cancelled")
        return True

    # -- the recorded sample -----------------------------------------------------------------

    def _run_sample(self, run: Run, inv: Investigator) -> None:
        from simulator import demo

        try:
            run.add({"kind": "info", "label": "Recorded sample",
                     "detail": "Replaying the DEMO-101 investigation against the simulated org. The evidence is "
                               "gathered live by the real tools; the written analysis is the stored sample. "
                               "The AI agent is not used."})

            def on_step(tool: str, what: str) -> None:
                if run.status != "running":
                    raise InterruptedError
                run.add({"kind": "tool", "tool": tool, "label": what})
                time.sleep(float(os.environ.get("SFTI_SAMPLE_STEP_DELAY", "0.35")))

            demo.gather_evidence(inv, run.folder, on_step)
            on_step("write", "Writing the ticket summary, the report and the draft comment")
            demo.install_expected(run.folder)
            on_step("validate_report", "Checking that every citation in the report resolves")
            check = inv.validate_report(run.key)
            inv.export_report_html(run.key)
            inv.validate_comment(run.key)
            on_step("remember_org_fact", "Saving what was learned about the org")
            added = demo.remember_org_facts(inv)
            run.add({"kind": "done", "label": "Investigation finished",
                     "detail": f"Evidence check {'passed' if check['ok'] else 'failed'}: "
                               f"{check['citations']['valid']} citations verified. "
                               f"{len(added)} new fact(s) added to the org memory."})
            run.finish("finished")
        except InterruptedError:
            pass
        except Exception as exc:  # noqa: BLE001 - report every failure to the page instead of dying silently
            run.add({"kind": "error", "label": "The sample could not be replayed",
                     "detail": str(getattr(exc, "message", exc))[:600]})
            run.finish("failed")

    # -- Claude Code -------------------------------------------------------------------------

    def _run_agent(self, run: Run, inv: Investigator) -> None:
        executable = find_claude()
        if not executable:
            run.add({"kind": "error", "label": "Claude Code was not found on this computer",
                     "detail": "Install Claude Code, or open it in this folder yourself and paste the prompt "
                               "shown on this page. The result appears here either way."})
            run.finish("failed")
            return
        request = read_request(run.folder)
        prompt = agent_prompt(request, resume=has_progress(run.folder))
        try:
            command = build_command(executable, self.root, chosen_model(request), economy=bool(request.get("economy")),
                                    budget_usd=request.get("budget_usd"))
        except ImportError as exc:
            run.add({"kind": "error", "label": "The sf-investigator tools are not installed",
                     "detail": f"{exc}. Run the start script again (see QUICKSTART.md), then start the investigation again."})
            run.finish("failed")
            return
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                command, cwd=str(self.root), env=self._child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kwargs)
        except OSError as exc:
            run.add({"kind": "error", "label": "Claude Code could not be started", "detail": str(exc)[:300]})
            run.finish("failed")
            return
        run.process = process
        run.add({"kind": "info", "label": "Starting Claude Code", "detail": "This usually takes a few minutes."})
        timer = threading.Timer(self._timeout, lambda: self._time_out(run))
        timer.daemon = True
        timer.start()
        stderr_tail: list[str] = []

        def drain_stderr() -> None:
            # Kept for the error message if the run fails. A line saying the folder "has not been
            # trusted" is expected and harmless here: the tools are allowed on the command line.
            assert process.stderr is not None
            for line in process.stderr:
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-20]

        threading.Thread(target=drain_stderr, daemon=True).start()
        finished_ok = budget_stop = False
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                if run.status != "running":
                    break  # stopped, timed out or failed for good: what it still prints is not reported
                message = parse_stream_line(line)
                if message is None:
                    continue
                if run.meter.take(message) == "plan" and run.meter.plan:
                    self.book.save_plan(run.meter.plan)
                for event in events_from_message(message):
                    fatal = event.pop("fatal", False)
                    if event.pop("budget_stop", False):
                        budget_stop = True
                    if event["kind"] == "done":
                        finished_ok = True
                    run.add(tidy_event(event, self.root))
                    if fatal:
                        # Without its tools the investigator could only guess. Stop instead.
                        run.status = "failed"
                        from core.sf_cli import _kill_tree
                        _kill_tree(process)
                        run.finish("failed")
                        break
            process.wait()
        except (OSError, ValueError) as exc:
            run.add({"kind": "error", "label": "Lost contact with Claude Code", "detail": str(exc)[:300]})
        finally:
            timer.cancel()
        if run.status != "running":
            return  # cancelled or timed out; already recorded
        if budget_stop:
            run.finish("incomplete")
            return
        if finished_ok and process.returncode == 0:
            if (run.folder / "report.md").is_file():
                run.finish("finished")
            else:
                # It said it was done, but there is nothing to show. Say so instead of "finished".
                run.add({"kind": "warn", "label": "It stopped before writing a report",
                         "detail": "What it gathered is kept in the ticket folder, and its last message is just "
                                   "above. Add what it is missing under 'Add information', or start it again to "
                                   "continue from where it stopped."})
                run.finish("incomplete")
        else:
            if not any(e["kind"] == "error" for e in run.events):
                run.add({"kind": "error", "label": "Claude Code ended without finishing",
                         "detail": (" ".join(stderr_tail)[-600:] or f"exit code {process.returncode}")})
            run.finish("failed")

    def _time_out(self, run: Run) -> None:
        if run.status != "running":
            return
        # Record why before the status changes, so nobody sees "failed" without the reason.
        run.add({"kind": "error", "label": "Stopped: the investigation ran longer than the time limit",
                 "detail": f"{int(self._timeout // 60)} minutes. What was written so far is kept."})
        run.status = "failed"
        if run.process and run.process.poll() is None:
            from core.sf_cli import _kill_tree
            _kill_tree(run.process)
        run.finish("failed")
