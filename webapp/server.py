"""HTTP server for the local interface. Standard library only.

It listens on 127.0.0.1 only. Requests whose ``Host`` header is not a loopback name are refused
(so a web page elsewhere cannot reach it by pointing a domain name at 127.0.0.1), and every
request that changes something needs the token that is embedded in the served page. The one
exception is a name the user allows on purpose with ``--allow-host``, for a proxy they trust in
front of the page (the preview of a cloud session); refused names are printed in the window.

There is deliberately no route that changes a Salesforce org. ``/api/orgs/connect`` saves an
org's authorization on this computer, from an authorization URL the user pastes; it changes
nothing in the org.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import platform
import re
import secrets
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from core import prereqs
from core.app import Investigator
from core.config import project_root
from core.errors import InputValidationError, SfCliError, SfInvestigatorError, WorkspaceError
from core.sf_cli import SfCli, read_auth_url, validate_target_org
from core.workspace import validate_ticket_key

from . import __version__, agent, tickets, usage

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1_000_000
#: An attached file travels as base64 inside JSON, which is a third larger than the file.
MAX_UPLOAD_BODY_BYTES = tickets.MAX_ATTACHMENT_BYTES * 4 // 3 + 4096
#: A ticket may come with its PDF (base64 in JSON) and the text read from it.
MAX_TICKET_BODY_BYTES = tickets.MAX_TICKET_PDF_BYTES * 4 // 3 + tickets.MAX_TICKET_CHARS * 4 + 65536
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
#: The page runs only its own script file: no inline script, so injected markup cannot run code even if it
#: got past the HTML escaping. Styles stay inline (the page is one file).
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
#: An exported report: a sandboxed document with no script and nothing loaded from anywhere.
REPORT_CSP = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; "
              "base-uri 'none'; form-action 'none'")
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
_HOST_NAME_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")


def check_allowed_host(name: Any) -> str:
    """An exact host name for ``--allow-host``: no wildcard, no port, no scheme."""
    value = str(name or "").strip().lower()
    if not _HOST_NAME_RE.match(value) or value in _LOOPBACK_NAMES:
        raise InputValidationError(
            f"{str(name)[:80]!r} is not a host name this page can be allowed to answer for.",
            hint="Give one exact name, such as preview.example.com: no *, no port, no http://.")
    return value


def _host_name(header: str | None) -> str:
    """The host part of a ``Host`` header, lower case, without the port."""
    value = (header or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0]

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


class ViewCache:
    """Recent answers for the pages that run a command-line tool, which takes seconds each time.

    For display only. Nothing that decides whether a write is allowed ever reads from here: the
    guard asks the org itself, every time. A failure is never kept.
    """

    def __init__(self) -> None:
        self._items: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, seconds: float, compute: Callable[[], Any], *, refresh: bool = False) -> Any:
        with self._lock:
            hit = self._items.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < seconds:
            return hit[1]
        value = compute()
        with self._lock:
            self._items[key] = (time.monotonic(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class App:
    """State shared by all requests: the project root, demo mode, the investigator and the runs."""

    def __init__(self, root: Path | None = None, *, demo: bool = False, demo_state: Path | None = None,
                 allowed_hosts: Any = ()):
        self.root = Path(root).resolve() if root else project_root()
        self.token = secrets.token_urlsafe(24)
        #: Names besides this computer's own that the page answers for: only a proxy the user trusts
        #: in front of it (``--allow-host``), such as the preview of a cloud session. Empty by default.
        self.allowed_hosts = frozenset(check_allowed_host(name) for name in allowed_hosts)
        self._refused_names: set[str] = set()
        self._lock = threading.Lock()
        self._demo_state = Path(demo_state) if demo_state else None
        self._launcher: Path | None = None
        self.demo = False
        self._inv: Investigator | None = None
        self.cache = ViewCache()
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
        self.cache.clear()
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

    def _claude(self, *, refresh: bool = False) -> tuple[str | None, str | None]:
        """``(path, version)`` of Claude Code. Asking for the version starts a process, so it is remembered."""
        path = agent.find_claude()
        if not path:
            return None, None
        return path, self.cache.get(("claude", path), 600, lambda: agent.claude_version(path), refresh=refresh)

    def status(self) -> dict[str, Any]:
        inv = self.investigator()
        config = inv.get_config()
        claude, claude_version = self._claude()
        return {
            "version": __version__,
            "project": self.root.name,
            "os": operating_system(),
            "demo": self.demo,
            "mode": config["mode"],
            "config_warnings": config["warnings"],
            "jira": config["jira"],
            "claude": {"available": bool(claude), "version": claude_version},
            "models": agent.MODEL_CHOICES,
            "default_model": agent.chosen_model({}),
            "tickets": len(inv.workspace.list_tickets()),
            "sample_ticket_key": "DEMO-101",
        }

    def prerequisites(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.cache.get("prereqs", 60, lambda: self._prerequisites(refresh), refresh=refresh)

    def _prerequisites(self, refresh: bool) -> dict[str, Any]:
        inv = self.investigator()
        checks = prereqs.run_checks(inv.cli)
        _, version = self._claude(refresh=refresh)
        checks.insert(4, {"name": "Claude Code", "ok": bool(version), "required": False,
                          "detail": version or "not found",
                          **({} if version else {"hint": "Needed to investigate from this page. Without it you can "
                                                         "still open Claude Code yourself and paste the prompt."})})
        system = operating_system()
        for check in checks:
            if not check["ok"] and check["name"] in FIXES:
                check["fix"] = FIXES[check["name"]][system]
        # "ready", not "ok": in an answer of this server "ok" says whether the request itself worked.
        return {"os": system, "ready": all(c["ok"] for c in checks if c["required"]), "checks": checks,
                "demo": self.demo}

    def orgs(self, *, refresh: bool = False) -> dict[str, Any]:
        def ask() -> dict[str, Any]:
            out = self.investigator().list_orgs()
            out["demo"] = self.demo
            return out

        if refresh:
            self.cache.clear()  # a newly connected org also changes what each org page shows
        return self.cache.get("orgs", 30, ask, refresh=refresh)

    def org(self, alias: str, *, refresh: bool = False) -> dict[str, Any]:
        """What kind of org this is, for display. The memory counts are read fresh each time (they are local)."""
        def ask() -> dict[str, Any]:
            info = self.investigator().org_info(alias)
            return {k: info[k] for k in ("target", "alias", "org_id", "name", "instance_url", "classification",
                                         "writes_allowed", "classification_reason", "what_is_allowed", "org_memory")
                    if k in info}

        out = dict(self.cache.get(("org", alias), 60, ask, refresh=refresh))
        if out.get("org_id"):
            try:
                out["org_memory"] = self.investigator().knowledge.org_map(out["org_id"])["counts"]
            except SfInvestigatorError:
                pass
        return out

    def answers_for(self, name: str) -> bool:
        """Whether a request naming this host (``Host`` or ``Origin``) may be answered."""
        return name in _LOOPBACK_NAMES or name in LOOPBACK_HOSTS or name in self.allowed_hosts

    def note_refused(self, what: str, name: str) -> None:
        """Say once, in the window, which name was refused, so a trusted proxy can be allowed on purpose."""
        shown = re.sub(r"[^a-z0-9.\-\[\]:]", "?", name.lower())[:100] or "(none)"
        with self._lock:
            if shown in self._refused_names:
                return
            self._refused_names.add(shown)
        print(f"Refused a request with {what} {shown}: this page only answers on this computer. If {shown} is a "
              f"proxy you trust in front of it, start it with --allow-host {shown}", file=sys.stderr, flush=True)

    def connect_org(self, alias: Any, auth_url: Any, *, replace: bool = False) -> dict[str, Any]:
        """Save an org's authorization on this computer from the authorization URL the user pasted.

        It changes nothing in any org. The URL works like a password: it goes to the Salesforce CLI
        on standard input and is never written, logged or sent back.
        """
        if self.demo:
            raise InputValidationError("The demo uses simulated orgs.",
                                       hint="Press 'Use my own Salesforce orgs' first, then connect your org.")
        if not isinstance(alias, str) or not alias.strip():
            raise InputValidationError("Give the org a short name, for example my-dev.")
        alias = validate_target_org(alias.strip())
        auth = read_auth_url(auth_url)
        if not replace:
            # Pointing a name in use at another org changes what every later command with that name reaches.
            in_use = {str(o.get("alias") or "") for o in self.investigator().list_orgs().get("orgs", [])}
            if alias in in_use:
                raise InputValidationError(
                    f"The name {alias!r} is already used by a connected org.",
                    hint="Choose another short name, or confirm that this name should now point at the org you "
                         "are connecting.", details={"alias_in_use": alias})
        print(f"Connecting the org '{alias}' at {auth.host}...", file=sys.stderr, flush=True)
        try:
            connected = self.investigator().cli.connect_with_auth_url(alias, auth)
        except SfInvestigatorError as exc:
            print(f"The org '{alias}' at {auth.host} was not connected: {exc.message}", file=sys.stderr, flush=True)
            raise
        except Exception:  # noqa: BLE001 - say nothing that could carry the pasted value
            raise SfCliError("Connecting failed for an unexpected reason.", command="sf org login sfdx-url",
                             hint="Try again; if it fails again, connect the org from a terminal instead.") from None
        self.cache.clear()
        print(f"Connected the org '{alias}' ({connected['org_id']}) at {auth.host}.", file=sys.stderr, flush=True)
        return {"connected": connected}

    def sample_ticket(self) -> dict[str, Any]:
        from simulator import demo
        return {"key": demo.KEY, "text": demo.TICKET_TEXT.read_text(encoding="utf-8"), "org": demo.PROD,
                "sandbox": demo.SANDBOX}

    def memory_orgs(self) -> dict[str, Any]:
        return {"orgs": self.investigator().knowledge.known_orgs()}

    def memory_map(self, org_id: str) -> dict[str, Any]:
        return self.investigator().knowledge.org_map(org_id)

    def usage(self) -> dict[str, Any]:
        """What investigations cost: the runs going on now, the last one, today, and the plan's limits."""
        book = self.runner.book
        entries = book.entries()
        running = [{"key": key, "started_at": run.started_at, "options": run.options or None,
                    "usage": run.meter.snapshot()}
                   for key, run in list(self.runner.runs.items()) if run.status == "running" and run.runner == "agent"]
        return {"running": running, "last": entries[-1] if entries else None, "today": book.today(entries),
                "plan": book.load_plan(), "note": usage.PRICING_NOTE}

    def memory_search(self, org_id: str, words: str) -> dict[str, Any]:
        """Search one org's memory, as the investigator does at the start of a ticket. Local; asks no org."""
        if not words.strip():
            raise InputValidationError("Type a few words to search for, for example: case priority flow.")
        return self.investigator().knowledge.search(org_id, words[:300], limit=15)

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


_TICKET_ROUTE = re.compile(r"^/api/tickets/([^/]+)(?:/(run|cancel|events|note|attachments))?\Z")
_ORG_ROUTE = re.compile(r"^/api/orgs/([^/]+)\Z")
_MEMORY_ROUTE = re.compile(r"^/api/memory/(00D[A-Za-z0-9]{12,15})(?:/(verify|search))?\Z")
_JIRA_ROUTE = re.compile(r"^/api/jira/([^/]+)\Z")
_REPORT_ROUTE = re.compile(r"^/tickets/([^/]+)/report\.html\Z")
_RESULT_ROUTE = re.compile(r"^/tickets/([^/]+)/result\.zip\Z")
_PDF_ROUTE = re.compile(r"^/tickets/([^/]+)/ticket\.pdf\Z")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "sfti-web"
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
            if os.environ.get("SFTI_WEB_LOG"):
                super().log_message(format, *args)

        def _send(self, status: int, body: bytes, content_type: str, *, download_as: str | None = None,
                  csp: str = PAGE_CSP) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if download_as:
                self.send_header("Content-Disposition", f'attachment; filename="{download_as}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str, **extra: Any) -> None:
            self._json({"ok": False, "error": {"message": message, **extra}}, status)

        def _local_only(self) -> bool:
            host = _host_name(self.headers.get("Host"))
            if not app.answers_for(host):
                app.note_refused("host", host)
                self._error(HTTPStatus.FORBIDDEN, "This interface only answers on this computer.")
                return False
            return True

        def _body(self, limit: int = MAX_BODY_BYTES) -> dict[str, Any] | None:
            if self.headers.get("X-SFTI-Token") != app.token:
                self._error(HTTPStatus.FORBIDDEN, "Missing or wrong page token. Reload the page.")
                return None
            origin = self.headers.get("Origin")
            if origin:
                try:
                    origin_host = (urlsplit(origin).hostname or "").lower()
                except ValueError:
                    origin_host = ""
                if not app.answers_for(origin_host):
                    app.note_refused("origin", origin_host)
                    self._error(HTTPStatus.FORBIDDEN, "Requests from other sites are refused.")
                    return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > limit:
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
                # "ok" is set last: nothing a view returns can make a request that worked look failed.
                self._json({**action(), "ok": True})
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
            if path == "/static/app.js":
                return self._send(200, (STATIC / "app.js").read_bytes(), "text/javascript; charset=utf-8")
            if path == "/api/status":
                return self._guarded(app.status)
            refresh = (query.get("refresh") or [""])[0] == "1"
            if path == "/api/prereqs":
                return self._guarded(lambda: app.prerequisites(refresh=refresh))
            if path == "/api/orgs":
                return self._guarded(lambda: app.orgs(refresh=refresh))
            if path == "/api/sample":
                return self._guarded(app.sample_ticket)
            if path == "/api/memory":
                return self._guarded(app.memory_orgs)
            if path == "/api/usage":
                return self._guarded(app.usage)
            if path == "/api/tickets":
                return self._guarded(lambda: {"tickets": tickets.list_tickets(inv(), app.runner.summaries())})
            match = _ORG_ROUTE.match(path)
            if match:
                return self._guarded(lambda: app.org(match.group(1), refresh=refresh))
            match = _MEMORY_ROUTE.match(path)
            if match and not match.group(2):
                return self._guarded(lambda: app.memory_map(match.group(1)))
            if match and match.group(2) == "search":
                return self._guarded(lambda: app.memory_search(match.group(1), (query.get("q") or [""])[0]))
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
                    detail = tickets.ticket_detail(inv(), key, app.runner.summary(key, folder))
                    detail["usage_total"] = app.runner.book.for_ticket(key)
                    return detail

                return self._guarded(view)
            match = _REPORT_ROUTE.match(path)
            if match:
                try:
                    validate_ticket_key(match.group(1))
                    report = inv().workspace.ticket_dir(match.group(1)) / "report.html"
                    # Written by the investigator from ticket text and org data: shown in a sandbox, with no script.
                    return self._send(200, report.read_bytes(), "text/html; charset=utf-8", csp=REPORT_CSP)
                except (SfInvestigatorError, OSError):
                    return self._error(HTTPStatus.NOT_FOUND, "No exported report for this ticket yet.")
            match = _RESULT_ROUTE.match(path)
            if match:
                try:
                    key = validate_ticket_key(match.group(1))
                    return self._send(200, tickets.result_bundle(inv(), key), "application/zip",
                                      download_as=f"{key}-result.zip")
                except (SfInvestigatorError, OSError):
                    return self._error(HTTPStatus.NOT_FOUND, "No report for this ticket yet.")
            match = _PDF_ROUTE.match(path)
            if match:
                try:
                    key = validate_ticket_key(match.group(1))
                    pdf = inv().workspace.ticket_dir(key) / "attachments" / tickets.ORIGINAL_PDF_NAME
                    return self._send(200, pdf.read_bytes(), "application/pdf")
                except (SfInvestigatorError, OSError):
                    return self._error(HTTPStatus.NOT_FOUND, "This ticket was not given as a PDF.")
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

        do_HEAD = do_GET  # noqa: N815

        # -- POST and DELETE -------------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if not self._local_only():
                return
            path = unquote(urlsplit(self.path).path)
            limit = (MAX_UPLOAD_BODY_BYTES if path.endswith("/attachments")
                     else MAX_TICKET_BODY_BYTES if path in ("/api/tickets", "/api/ticket-pdf") else MAX_BODY_BYTES)
            body = self._body(limit)
            if body is None:
                return
            inv = app.investigator
            if path == "/api/demo":
                return self._guarded(lambda: app.set_demo(bool(body.get("enabled")), reset=bool(body.get("reset"))))
            if path == "/api/orgs/connect":
                return self._guarded(lambda: app.connect_org(body.get("alias"), body.get("auth_url"),
                                                             replace=body.get("replace") is True))
            if path == "/api/ticket-pdf":
                def read_pdf() -> dict[str, Any]:
                    found = tickets.read_pdf(_decode(body.get("content_base64")))
                    body_text = found["text"] if found["readable"] else ""
                    return {**found, "key": tickets.detect_key(body_text), "title": tickets.detect_title(body_text)}
                return self._guarded(read_pdf)
            if path == "/api/tickets":
                def create() -> dict[str, Any]:
                    pdf = _decode(body["pdf_base64"]) if body.get("pdf_base64") else None
                    request = tickets.create_ticket(inv(), body.get("text"), body.get("org"), key=body.get("key"),
                                                    sandbox=body.get("sandbox"), model=body.get("model"),
                                                    economy=bool(body.get("economy")),
                                                    budget_usd=body.get("budget_usd"), pdf=pdf)
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
                    if app.runner.is_running(key):
                        raise WorkspaceError("This ticket is already being investigated.")
                    folder = inv().workspace.ticket_dir(key)
                    if runner == "agent" and str(body.get("org") or "").strip():
                        tickets.set_org(folder, key, body.get("org"))
                    if runner == "agent" and not tickets.read_request(folder).get("org"):
                        raise InputValidationError(
                            "No org is recorded for this ticket: it was started in Claude Code, not on this page.",
                            hint="Give the alias of the org to investigate.", details={"needs_org": True})
                    if "budget_usd" in body:
                        tickets.check_budget(body.get("budget_usd"))  # refused before anything is changed
                    if "model" in body:
                        tickets.set_model(folder, body.get("model"))
                    if "economy" in body or "budget_usd" in body:
                        tickets.set_run_options(folder, economy=(bool(body["economy"]) if "economy" in body else None),
                                                budget_usd=body.get("budget_usd", "unchanged"))
                    if str(body.get("note") or "").strip():
                        tickets.add_follow_up(inv(), key, body["note"])
                    return {"run": app.runner.start(key, runner).summary()}

                return self._guarded(start)
            if match and match.group(2) == "note":
                return self._guarded(lambda: tickets.add_follow_up(inv(), validate_ticket_key(match.group(1)),
                                                                   body.get("text")))
            if match and match.group(2) == "attachments":
                def attach() -> dict[str, Any]:
                    key = validate_ticket_key(match.group(1))
                    return tickets.save_attachment(inv(), key, body.get("name"), _decode(body.get("content_base64")))

                return self._guarded(attach)
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


def _decode(value: Any) -> bytes:
    """A file sent by the page (base64 in JSON)."""
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError):
        raise InputValidationError("The file could not be read.") from None


def create_server(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind to the loopback interface only. There is no option to listen elsewhere."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server
