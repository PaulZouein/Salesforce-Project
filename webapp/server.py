"""HTTP server for the local interface. Standard library only.

It listens on 127.0.0.1 only. Requests whose ``Host`` header is not a loopback name are refused
(so a web page elsewhere cannot reach it by pointing a domain name at 127.0.0.1), and every
request that changes something needs the token that is embedded in the served page.

There is deliberately no route that changes a Salesforce org.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from core import prereqs
from core.app import Investigator
from core.config import project_root
from core.errors import InputValidationError, SfInvestigatorError, WorkspaceError
from core.sf_cli import SfCli
from core.workspace import validate_ticket_key

from . import __version__, agent, tickets

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1_000_000
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

#: What to do when a prerequisite is missing, per operating system.
FIXES: dict[str, dict[str, list[str]]] = {
    "Python": {
        "windows": ["Install Python 3.11 or newer from https://www.python.org/downloads/ (tick \"Add python.exe to PATH\")."],
        "macos": ["brew install python@3.13", "or install from https://www.python.org/downloads/"],
        "linux": ["sudo apt install python3 python3-venv   # Debian, Ubuntu", "sudo dnf install python3            # Fedora"],
    },
    "git": {
        "windows": ["winget install --id Git.Git -e", "or install from https://git-scm.com/download/win"],
        "macos": ["xcode-select --install", "or: brew install git"],
        "linux": ["sudo apt install git", "sudo dnf install git"],
    },
    "Salesforce CLI (sf)": {
        "windows": ["npm install --global @salesforce/cli", "Then close and reopen the terminal and run: sf --version"],
        "macos": ["npm install --global @salesforce/cli", "Then open a new terminal and run: sf --version"],
        "linux": ["npm install --global @salesforce/cli", "Then open a new terminal and run: sf --version"],
    },
    "mcp Python package": {
        "windows": ["python -m pip install -e \".[dev]\""],
        "macos": ["python -m pip install -e \".[dev]\""],
        "linux": ["python -m pip install -e \".[dev]\""],
    },
    "Claude Code": {
        "windows": ["npm install --global @anthropic-ai/claude-code", "Then run: claude   (and sign in once)"],
        "macos": ["npm install --global @anthropic-ai/claude-code", "Then run: claude   (and sign in once)"],
        "linux": ["npm install --global @anthropic-ai/claude-code", "Then run: claude   (and sign in once)"],
    },
}


def operating_system() -> str:
    name = platform.system().lower()
    return "windows" if name.startswith("win") else "macos" if name == "darwin" else "linux"


class App:
    """State shared by all requests: the project root, demo mode, the investigator and the runs."""

    def __init__(self, root: Path | None = None, *, demo: bool = False, demo_state: Path | None = None):
        self.root = Path(root).resolve() if root else project_root()
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._demo_state = Path(demo_state) if demo_state else None
        self._launcher: Path | None = None
        self.demo = False
        self._inv: Investigator | None = None
        self.runner = agent.Runner(self.root, self.investigator, self.child_env)
        configured = os.environ.get("SFTI_SF_EXECUTABLE", "")
        if demo or (configured and "simulator" in Path(configured).parts):
            self.set_demo(True)

    # -- investigator and demo mode ----------------------------------------------------------

    def investigator(self) -> Investigator:
        with self._lock:
            if self._inv is None:
                cli = SfCli(executable=str(self._launcher)) if self.demo and self._launcher else SfCli()
                self._inv = Investigator(root=self.root, cli=cli)
            return self._inv

    def set_demo(self, enabled: bool, *, reset: bool = False) -> dict[str, Any]:
        from simulator.build import DEFAULT_STATE, build_state, write_launcher

        with self._lock:
            if enabled:
                state = self._demo_state or DEFAULT_STATE
                launcher = state / "bin" / ("sf.cmd" if os.name == "nt" else "sf")
                if reset or not (state / "orgs").is_dir():
                    launcher = build_state(state)
                elif not launcher.is_file():
                    launcher = write_launcher(state.resolve())
                self._launcher = launcher
            self.demo = bool(enabled)
            self._inv = None
        return {"demo": self.demo}

    def child_env(self) -> dict[str, str]:
        """Environment for Claude Code: same Python and same Salesforce CLI as this server."""
        env = dict(os.environ)
        env.setdefault("SFTI_PYTHON", sys.executable)
        env["PYTHONUTF8"] = "1"
        if self.demo and self._launcher:
            env["SFTI_SF_EXECUTABLE"] = str(self._launcher)
        return env

    # -- read-only views ---------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        inv = self.investigator()
        config = inv.get_config()
        claude = agent.find_claude()
        return {
            "version": __version__,
            "project": self.root.name,
            "os": operating_system(),
            "demo": self.demo,
            "mode": config["mode"],
            "config_warnings": config["warnings"],
            "jira": config["jira"],
            "claude": {"available": bool(claude), "version": agent.claude_version(claude) if claude else None},
            "tickets": len(inv.workspace.list_tickets()),
            "sample_ticket_key": "DEMO-101",
        }

    def prerequisites(self) -> dict[str, Any]:
        inv = self.investigator()
        checks = prereqs.run_checks(inv.cli)
        claude = agent.find_claude()
        version = agent.claude_version(claude) if claude else None
        checks.insert(4, {"name": "Claude Code", "ok": bool(version), "required": False,
                          "detail": version or "not found",
                          **({} if version else {"hint": "Needed to investigate from this page. Without it you can "
                                                         "still open Claude Code yourself and paste the prompt."})})
        system = operating_system()
        for check in checks:
            if not check["ok"] and check["name"] in FIXES:
                check["fix"] = FIXES[check["name"]][system]
        return {"os": system, "ok": all(c["ok"] for c in checks if c["required"]), "checks": checks, "demo": self.demo}

    def orgs(self) -> dict[str, Any]:
        out = self.investigator().list_orgs()
        out["demo"] = self.demo
        return out

    def org(self, alias: str) -> dict[str, Any]:
        info = self.investigator().org_info(alias)
        return {k: info[k] for k in ("target", "alias", "org_id", "name", "instance_url", "classification",
                                     "writes_allowed", "classification_reason", "what_is_allowed", "org_memory")
                if k in info}

    def sample_ticket(self) -> dict[str, Any]:
        from simulator import demo
        return {"key": demo.KEY, "text": demo.TICKET_TEXT.read_text(encoding="utf-8"), "org": demo.PROD,
                "sandbox": demo.SANDBOX}

    def memory_orgs(self) -> dict[str, Any]:
        return {"orgs": self.investigator().knowledge.known_orgs()}

    def memory_map(self, org_id: str) -> dict[str, Any]:
        return self.investigator().knowledge.org_map(org_id)

    def memory_verify(self, org_id: str) -> dict[str, Any]:
        """Double-check one org's memory. Reads from the org; changes nothing there."""
        inv = self.investigator()
        aliases = inv.knowledge.load(org_id)["aliases"]
        last_error: SfInvestigatorError | None = None
        for alias in aliases:
            try:
                if (inv.guard.org_info(alias).org_id or "")[:15] != org_id[:15]:
                    continue
                return inv.verify_org_knowledge(alias)
            except SfInvestigatorError as exc:
                last_error = exc
        raise WorkspaceError(
            "None of the aliases remembered for this org is connected to it right now.",
            hint=(last_error.message if last_error else "Connect the org with the Salesforce CLI and try again."))

    def jira_text(self, key: str) -> dict[str, Any]:
        ticket = self.investigator().jira_get_ticket(key)
        lines = [f"Key: {ticket['key']}", f"Title: {ticket.get('summary') or ''}", f"Type: {ticket.get('type') or ''}",
                 f"Status: {ticket.get('status') or ''}", f"Priority: {ticket.get('priority') or ''}",
                 f"Created: {ticket.get('created') or ''}", f"Updated: {ticket.get('updated') or ''}",
                 f"Environment: {ticket.get('environment') or ''}", "", "Description", "-----------",
                 str(ticket.get("description") or ""), "", "Comments", "--------"]
        for comment in ticket.get("comments", []):
            lines += [f"[{comment.get('when')}] {comment.get('who')}:", str(comment.get("body") or ""), ""]
        if ticket.get("attachments"):
            lines += ["Attachments (not downloaded)", "----------------------------"]
            lines += [f"- {a.get('filename')}" for a in ticket["attachments"]]
        return {"key": ticket["key"], "text": "\n".join(lines)}


_TICKET_ROUTE = re.compile(r"^/api/tickets/([^/]+)(?:/(run|cancel|events))?$")
_ORG_ROUTE = re.compile(r"^/api/orgs/([^/]+)$")
_MEMORY_ROUTE = re.compile(r"^/api/memory/(00D[A-Za-z0-9]{12,15})(?:/(verify))?$")
_JIRA_ROUTE = re.compile(r"^/api/jira/([^/]+)$")
_REPORT_ROUTE = re.compile(r"^/tickets/([^/]+)/report\.html$")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "sfti-web"
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
            if os.environ.get("SFTI_WEB_LOG"):
                super().log_message(format, *args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str, **extra: Any) -> None:
            self._json({"ok": False, "error": {"message": message, **extra}}, status)

        def _local_only(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
                else (self.headers.get("Host") or "").split("]")[0] + "]"
            if host not in LOOPBACK_HOSTS:
                self._error(HTTPStatus.FORBIDDEN, "This interface only answers on this computer.")
                return False
            return True

        def _body(self) -> dict[str, Any] | None:
            if self.headers.get("X-SFTI-Token") != app.token:
                self._error(HTTPStatus.FORBIDDEN, "Missing or wrong page token. Reload the page.")
                return None
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
                self._error(HTTPStatus.FORBIDDEN, "Requests from other sites are refused.")
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The request is too large.")
                return None
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._error(HTTPStatus.BAD_REQUEST, "The request is not valid JSON.")
                return None
            if not isinstance(data, dict):
                self._error(HTTPStatus.BAD_REQUEST, "The request must be a JSON object.")
                return None
            return data

        def _guarded(self, action: Any) -> None:
            try:
                self._json({"ok": True, **action()})
            except (InputValidationError, WorkspaceError) as exc:
                self._json({"ok": False, "error": exc.to_dict()}, HTTPStatus.BAD_REQUEST)
            except SfInvestigatorError as exc:
                self._json({"ok": False, "error": exc.to_dict()}, HTTPStatus.BAD_GATEWAY)
            except Exception as exc:  # noqa: BLE001 - one bad request must not take the server down
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

        # -- GET -----------------------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - name required by the base class
            if not self._local_only():
                return
            url = urlsplit(self.path)
            path, query = unquote(url.path), parse_qs(url.query)
            inv = app.investigator
            if path in ("/", "/index.html"):
                page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__SFTI_TOKEN__", app.token)
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._guarded(app.status)
            if path == "/api/prereqs":
                return self._guarded(app.prerequisites)
            if path == "/api/orgs":
                return self._guarded(app.orgs)
            if path == "/api/sample":
                return self._guarded(app.sample_ticket)
            if path == "/api/memory":
                return self._guarded(app.memory_orgs)
            if path == "/api/tickets":
                return self._guarded(lambda: {"tickets": tickets.list_tickets(inv(), app.runner.summaries())})
            match = _ORG_ROUTE.match(path)
            if match:
                return self._guarded(lambda: app.org(match.group(1)))
            match = _MEMORY_ROUTE.match(path)
            if match and not match.group(2):
                return self._guarded(lambda: app.memory_map(match.group(1)))
            match = _JIRA_ROUTE.match(path)
            if match:
                return self._guarded(lambda: app.jira_text(match.group(1)))
            match = _TICKET_ROUTE.match(path)
            if match and match.group(2) in (None, "events"):
                key = match.group(1)

                def view() -> dict[str, Any]:
                    validate_ticket_key(key)
                    folder = inv().workspace.ticket_dir(key)
                    if match.group(2) == "events":
                        after = int((query.get("after") or ["0"])[0] or 0)
                        return {"events": app.runner.events(key, folder, after),
                                "run": app.runner.summary(key, folder)}
                    return tickets.ticket_detail(inv(), key, app.runner.summary(key, folder))

                return self._guarded(view)
            match = _REPORT_ROUTE.match(path)
            if match:
                try:
                    validate_ticket_key(match.group(1))
                    report = inv().workspace.ticket_dir(match.group(1)) / "report.html"
                    return self._send(200, report.read_bytes(), "text/html; charset=utf-8")
                except (SfInvestigatorError, OSError):
                    return self._error(HTTPStatus.NOT_FOUND, "No exported report for this ticket yet.")
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

        do_HEAD = do_GET  # noqa: N815

        # -- POST and DELETE -------------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if not self._local_only():
                return
            body = self._body()
            if body is None:
                return
            path = unquote(urlsplit(self.path).path)
            inv = app.investigator
            if path == "/api/demo":
                return self._guarded(lambda: app.set_demo(bool(body.get("enabled")), reset=bool(body.get("reset"))))
            if path == "/api/tickets":
                def create() -> dict[str, Any]:
                    request = tickets.create_ticket(inv(), body.get("text"), body.get("org"), key=body.get("key"),
                                                    sandbox=body.get("sandbox"))
                    return {"ticket": request, "prompt": tickets.agent_prompt(request)}
                return self._guarded(create)
            match = _MEMORY_ROUTE.match(path)
            if match and match.group(2) == "verify":
                return self._guarded(lambda: app.memory_verify(match.group(1)))
            match = _TICKET_ROUTE.match(path)
            if match and match.group(2) == "run":
                key = match.group(1)

                def start() -> dict[str, Any]:
                    validate_ticket_key(key)
                    runner = body.get("runner")
                    if runner not in ("agent", "sample"):
                        raise InputValidationError("runner must be 'agent' or 'sample'.")
                    if runner == "sample":
                        from simulator import demo
                        if key != demo.KEY or not app.demo:
                            raise InputValidationError(
                                "The recorded sample exists only for the sample ticket DEMO-101 on the demo orgs.")
                    return {"run": app.runner.start(key, runner).summary()}

                return self._guarded(start)
            if match and match.group(2) == "cancel":
                return self._guarded(lambda: {"cancelled": app.runner.cancel(match.group(1))})
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._local_only():
                return
            if self._body() is None:
                return
            match = _TICKET_ROUTE.match(unquote(urlsplit(self.path).path))
            if match and not match.group(2):
                key = match.group(1)

                def delete() -> dict[str, Any]:
                    validate_ticket_key(key)
                    if app.runner.is_running(key):
                        raise WorkspaceError("Stop the investigation before deleting the ticket.")
                    app.runner.runs.pop(key, None)
                    return tickets.delete_ticket(app.investigator(), key)

                return self._guarded(delete)
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

    return Handler


def create_server(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind to the loopback interface only. There is no option to listen elsewhere."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server
