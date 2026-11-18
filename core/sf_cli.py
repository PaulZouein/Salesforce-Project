"""Subprocess wrapper around the Salesforce CLI (``sf``).

Responsibilities:

* run ``sf <command> --json`` without a shell, with a timeout, on Linux, macOS and Windows;
* classify every command as *read*, *test* or *write* before it runs. Anything that is not
  on the read allowlist is a write and needs a :class:`Permit` issued by ``org_guard``;
* strip secrets (access tokens and similar) from everything the CLI returns;
* turn CLI failures into typed errors with a clear message.

Flags and JSON shapes were checked against ``@salesforce/cli`` 2.158.7 (``--help`` output and
the plugin sources). docs/VERIFICATION.md has the list.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .errors import (
    InputValidationError,
    ProductionWriteBlockedError,
    SfCliError,
    SfCliNotFoundError,
    SfCliOutputError,
    SfCliTimeoutError,
    UnsafeArgumentError,
    WritePermitError,
)

DEFAULT_TIMEOUT_SECONDS = 120.0

# --- Command classification -------------------------------------------------------------

#: Flags accepted on every command.
GLOBAL_FLAGS = frozenset({"--json", "--api-version"})

#: Commands that only read from the org, with the exact flags the wrapper may pass.
#: Anything else is a write. ``org list --clean`` (removes local authorizations) is excluded
#: by not being listed.
READ_COMMANDS: dict[tuple[str, ...], frozenset[str]] = {
    ("org", "list"): frozenset({"--skip-connection-status", "--all"}),
    ("org", "display"): frozenset({"--target-org"}),
    ("data", "query"): frozenset({"--target-org", "--file", "--use-tooling-api", "--all-rows"}),
    ("sobject", "describe"): frozenset({"--target-org", "--sobject", "--use-tooling-api"}),
    ("project", "retrieve", "start"): frozenset({"--target-org", "--metadata", "--wait", "--ignore-conflicts"}),
    ("apex", "get", "log"): frozenset({"--target-org", "--log-id", "--output-dir"}),
}

#: Commands that run Apex tests. They commit no business data but do enqueue jobs in the
#: org, so they need a "test" permit.
TEST_COMMANDS: dict[tuple[str, ...], frozenset[str]] = {
    ("apex", "run", "test"): frozenset(
        {"--target-org", "--class-names", "--tests", "--result-format", "--code-coverage", "--wait", "--synchronous"}
    ),
}

KIND_READ = "read"
KIND_TEST = "test"
KIND_WRITE = "write"

#: Org classifications on which a write permit may exist at all.
WRITABLE_CLASSIFICATIONS = frozenset({"sandbox", "scratch", "developer"})

SECRET_KEYS = frozenset(
    k.lower()
    for k in (
        "accessToken",
        "access_token",
        "refreshToken",
        "refresh_token",
        "sfdxAuthUrl",
        "password",
        "clientSecret",
        "client_secret",
        "privateKey",
        "authCode",
    )
)

_TARGET_ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]{0,128}$")
_WINDOWS_METACHARS = set('&|<>^%"!')


def validate_target_org(target: str) -> str:
    """Return ``target`` if it is a plausible alias or username, else raise."""
    if not isinstance(target, str) or not _TARGET_ORG_RE.match(target):
        raise InputValidationError(
            f"Invalid org alias or username: {target!r}.",
            hint="Use an alias or username from list_orgs. Letters, digits and . _ @ + - only.",
        )
    return target


# --- Connecting an org from an authorization URL ------------------------------------------

#: A Salesforce DX authorization URL as ``sf org auth show-sfdx-auth-url`` prints it and as
#: ``AuthInfo.parseSfdxAuthUrl`` in ``@salesforce/core`` (CLI 2.158.7) reads it:
#: ``force://<client id>:<client secret, may be empty>:<refresh token>@<org host[:port]>``.
#: The refresh token in it works like a password.
_AUTH_URL_RE = re.compile(
    r"force://([a-zA-Z0-9._-]+={0,2}):([a-zA-Z0-9._-]*={0,2}):([a-zA-Z0-9._-]+={0,2})@([a-zA-Z0-9._-]+)(?::(\d{1,5}))?/?"
)
MAX_AUTH_URL_LENGTH = 4096
_NETWORK_FAILURE_RE = re.compile(
    r"\b(403|407|tunnel\w*|ECONN\w*|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|getaddrinfo|socket hang up|certificate|"
    r"network|proxy)\b",
    re.IGNORECASE,
)
_REFUSED_TOKEN_RE = re.compile(r"invalid_grant|expired|revoked|refresh token|inactive", re.IGNORECASE)


@dataclass(frozen=True)
class AuthUrl:
    """A checked authorization URL. ``host`` is the org's address and is not secret; the rest is."""

    value: str
    host: str
    secrets: tuple[str, ...]

    def __repr__(self) -> str:  # never show the secret parts, not even in a traceback
        return f"AuthUrl(host={self.host!r})"


def read_auth_url(value: Any) -> AuthUrl:
    """Check that ``value`` is a Salesforce DX authorization URL and return it, or raise.

    Error messages never repeat the value.
    """
    if not isinstance(value, str) or not value.strip():
        raise InputValidationError(
            "Paste the authorization URL.",
            hint="On a computer where the org is connected, run 'sf org auth show-sfdx-auth-url --target-org <alias>' "
            "and copy the line that starts with force://.",
        )
    text = value.strip().strip('"').strip("'").strip()
    if len(text) > MAX_AUTH_URL_LENGTH:
        raise InputValidationError("This is too long to be an authorization URL.")
    match = _AUTH_URL_RE.fullmatch(text)
    if not match:
        raise InputValidationError(
            "This is not a Salesforce authorization URL.",
            hint="It is one line that starts with force:// and ends with @ and the org's address, as printed by "
            "'sf org auth show-sfdx-auth-url'. An access token or a password does not work here.",
        )
    _client_id, client_secret, refresh_token, host, _port = match.groups()
    if "." not in host or host.startswith((".", "-")) or ".." in host:
        raise InputValidationError("The org address at the end of the authorization URL is not valid.")
    secrets = tuple(s for s in (refresh_token, client_secret) if len(s) >= 8)
    return AuthUrl(value=text, host=host.lower(), secrets=(text, *secrets))


def _without_secrets(text: str, auth: AuthUrl) -> str:
    for secret in auth.secrets:
        text = text.replace(secret, "[removed]")
    return re.sub(r"force://\S+", "force://[removed]", text)


def _connect_hint(message: str, host: str) -> str:
    if _NETWORK_FAILURE_RE.search(message):
        return (f"This computer could not reach {host}. If the investigator runs in a cloud session or behind a "
                f"company proxy, its network settings must allow {host}.")
    if _REFUSED_TOKEN_RE.search(message):
        return ("Salesforce refused the authorization: it was revoked, or it expired. On the other computer, log in "
                "again ('sf org login web') and show the authorization URL again.")
    return "Check that the org is connected on the other computer ('sf org list'), then copy its authorization again."


def command_tokens(args: Sequence[str]) -> tuple[str, ...]:
    """Leading non-flag tokens, e.g. ``("data", "query")``."""
    tokens: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            break
        tokens.append(arg)
    return tuple(tokens)


def _flags(args: Sequence[str]) -> list[str]:
    return [a.split("=", 1)[0] for a in args if a.startswith("-")]


def classify_command(args: Sequence[str]) -> str:
    """Classify a command as read, test or write. Unknown commands are writes."""
    tokens = command_tokens(args)
    for table, kind in ((READ_COMMANDS, KIND_READ), (TEST_COMMANDS, KIND_TEST)):
        allowed = table.get(tokens)
        if allowed is None:
            continue
        for flag in _flags(args):
            if flag not in allowed and flag not in GLOBAL_FLAGS:
                raise UnsafeArgumentError(
                    f"Flag {flag} is not allowed on 'sf {' '.join(tokens)}'.",
                    hint="Only flags on the wrapper's allowlist can be passed to read and test commands.",
                )
        return kind
    return KIND_WRITE


def _flag_value(args: Sequence[str], flag: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


# --- Permits ----------------------------------------------------------------------------

_PERMIT_KEY = object()


class Permit:
    """Proof that ``org_guard`` authorized a write or a test run against one org.

    A permit is bound to the target org and can be used for a fixed number of commands.
    Only :func:`_issue_permit` can create one; ``org_guard`` is the only caller.
    """

    __slots__ = ("kind", "target", "org_id", "classification", "operation", "uses_left")

    def __init__(self, key: object, *, kind: str, target: str, org_id: str, classification: str, operation: str, uses: int):
        if key is not _PERMIT_KEY:
            raise WritePermitError("Permits can only be issued by org_guard.")
        if kind not in (KIND_WRITE, KIND_TEST):
            raise WritePermitError(f"Unknown permit kind {kind!r}.")
        if kind == KIND_WRITE and classification not in WRITABLE_CLASSIFICATIONS:
            # Defense in depth: even a buggy guard cannot mint a write permit for production.
            raise ProductionWriteBlockedError(
                f"Refusing to issue a write permit for an org classified as {classification!r}."
            )
        if not 1 <= uses <= 20:
            raise WritePermitError("A permit must cover between 1 and 20 commands.")
        self.kind = kind
        self.target = target
        self.org_id = org_id
        self.classification = classification
        self.operation = operation
        self.uses_left = uses

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Permit({self.kind}, {self.target}, {self.operation}, uses_left={self.uses_left})"


def _issue_permit(*, kind: str, target: str, org_id: str, classification: str, operation: str, uses: int = 1) -> Permit:
    """Create a permit. For ``org_guard`` only."""
    return Permit(
        _PERMIT_KEY, kind=kind, target=target, org_id=org_id, classification=classification, operation=operation, uses=uses
    )


# --- Process handling -------------------------------------------------------------------


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], "str | None", float, dict[str, str]], ProcessResult]


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            # sf.cmd -> cmd.exe -> node: kill the whole tree, not just cmd.exe.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def default_runner(
    argv: list[str], cwd: str | None, timeout: float, env: dict[str, str], *, input_text: str | None = None
) -> ProcessResult:
    """Run ``argv`` without a shell and return its output. Raises ``TimeoutError`` on timeout.

    ``input_text`` is written to the process's standard input; without it, standard input is empty.
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise TimeoutError from None
    return ProcessResult(proc.returncode, stdout or "", stderr or "")


def scrub_secrets(value: Any) -> Any:
    """Recursively drop keys that hold credentials."""
    if isinstance(value, dict):
        return {k: scrub_secrets(v) for k, v in value.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [scrub_secrets(v) for v in value]
    return value


def parse_json_output(stdout: str) -> dict[str, Any] | None:
    """Parse the CLI's JSON envelope, tolerating warning lines printed before it."""
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"^\{", text, flags=re.MULTILINE):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


class SfCli:
    """Runs Salesforce CLI commands.

    ``runner`` and ``windows`` exist so tests can inject canned output and exercise the
    Windows rules on any platform.
    """

    def __init__(
        self,
        executable: str | None = None,
        *,
        runner: Runner | None = None,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        windows: bool | None = None,
    ):
        self._executable = executable
        self._runner = runner or default_runner
        self._default_timeout = default_timeout
        self._windows = (os.name == "nt") if windows is None else windows

    # -- executable ----------------------------------------------------------------------

    def executable(self) -> str:
        exe = self._executable or os.environ.get("SFTI_SF_EXECUTABLE") or shutil.which("sf")
        if not exe:
            raise SfCliNotFoundError(
                "The Salesforce CLI ('sf') was not found on PATH.",
                hint="Install it with 'npm install --global @salesforce/cli' and reopen your terminal.",
            )
        return exe

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "SF_AUTOUPDATE_DISABLE": "true",
                "SF_DISABLE_TELEMETRY": "true",
                "SF_SKIP_NEW_VERSION_CHECK": "true",
                "NO_COLOR": "1",
                "FORCE_COLOR": "0",
            }
        )
        return env

    def version(self, timeout: float = 60.0) -> str:
        """Return the first line of ``sf --version``."""
        exe = self.executable()
        try:
            result = self._runner([exe, "--version"], None, timeout, self._environment())
        except TimeoutError:
            raise SfCliTimeoutError(f"'sf --version' did not finish within {timeout:.0f}s.") from None
        except OSError as exc:
            raise SfCliNotFoundError(f"Could not start the Salesforce CLI: {exc}") from None
        if result.returncode != 0:
            raise SfCliError(_tail(result.stderr or result.stdout), exit_code=result.returncode, command="sf --version")
        return (result.stdout.strip().splitlines() or [""])[0]

    # -- argument checks -----------------------------------------------------------------

    def _check_arguments(self, args: Sequence[str]) -> None:
        for arg in args:
            if not isinstance(arg, str):
                raise UnsafeArgumentError(f"CLI arguments must be strings; got {type(arg).__name__}.")
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise UnsafeArgumentError(
                    "CLI arguments cannot contain line breaks or NUL characters.",
                    hint="Pass free text through a file instead.",
                )
            if self._windows and _WINDOWS_METACHARS.intersection(arg):
                bad = "".join(sorted(_WINDOWS_METACHARS.intersection(arg)))
                raise UnsafeArgumentError(
                    f"Argument {arg!r} contains characters that cmd.exe interprets ({bad}).",
                    hint="On Windows the CLI runs through cmd.exe. Pass this value through a file, "
                    "or move the project to a path without these characters.",
                )

    def _check_permit(self, kind: str, args: Sequence[str], permit: Permit | None) -> None:
        if kind == KIND_READ:
            return
        label = "sf " + " ".join(command_tokens(args))
        if permit is None:
            raise WritePermitError(
                f"'{label}' is classified as a {kind} command and needs a permit from org_guard.",
                hint="Call org_guard.authorize_write() or authorize_test_run() first.",
            )
        if not isinstance(permit, Permit):
            raise WritePermitError("Invalid permit object.")
        if permit.kind != kind:
            raise WritePermitError(f"'{label}' needs a {kind} permit; got a {permit.kind} permit.")
        if permit.uses_left <= 0:
            raise WritePermitError("This permit has already been used.")
        target = _flag_value(args, "--target-org")
        if target != permit.target:
            raise WritePermitError(
                f"The permit was issued for org {permit.target!r} but the command targets {target!r}."
            )
        if kind == KIND_WRITE and permit.classification not in WRITABLE_CLASSIFICATIONS:
            raise ProductionWriteBlockedError(f"Writes are blocked on {permit.classification} orgs.")
        permit.uses_left -= 1

    # -- running -------------------------------------------------------------------------

    def run_envelope(
        self,
        args: Sequence[str],
        *,
        permit: Permit | None = None,
        timeout: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        accept_statuses: Iterable[int] = (0,),
    ) -> dict[str, Any]:
        """Run ``sf <args> --json`` and return the scrubbed JSON envelope.

        ``accept_statuses`` lists envelope statuses that carry a usable ``result`` even though
        they are not 0 (for example 100 when Apex tests fail, 1 when a deploy fails).
        """
        args = list(args)
        if not args:
            raise InputValidationError("No CLI command given.")
        self._check_arguments(args)
        kind = classify_command(args)
        target = _flag_value(args, "--target-org")
        if target is not None:
            validate_target_org(target)
        self._check_permit(kind, args, permit)

        if "--json" not in args:
            args.append("--json")
        exe = self.executable()
        label = "sf " + " ".join(command_tokens(args))
        limit = timeout or self._default_timeout
        try:
            proc = self._runner([exe, *args], str(cwd) if cwd is not None else None, limit, self._environment())
        except TimeoutError:
            raise SfCliTimeoutError(
                f"'{label}' did not finish within {limit:.0f}s and was stopped.",
                hint="Retry with a narrower request or a longer timeout.",
            ) from None
        except FileNotFoundError:
            raise SfCliNotFoundError(
                f"Could not start the Salesforce CLI at {exe!r}.",
                hint="Check that 'sf --version' works in your terminal.",
            ) from None
        except OSError as exc:
            raise SfCliError(f"Could not start '{label}': {exc}", command=label) from None

        envelope = parse_json_output(proc.stdout)
        if envelope is None:
            raise SfCliOutputError(
                f"'{label}' did not return JSON (exit code {proc.returncode}).",
                details={"stdout": _tail(proc.stdout), "stderr": _tail(proc.stderr)},
            )
        envelope = scrub_secrets(envelope)
        status = envelope.get("status", proc.returncode)
        accepted = set(accept_statuses)
        if status in accepted and "result" in envelope:
            return envelope
        if status == 0 and "result" in envelope:
            return envelope

        name = str(envelope.get("name") or envelope.get("code") or "Error")
        message = str(envelope.get("message") or f"'{label}' failed with status {status}.").strip()
        data = envelope.get("data")
        if data is None:
            data = envelope.get("result")
        raise SfCliError(
            message,
            name=name,
            exit_code=status if isinstance(status, int) else proc.returncode,
            command=label,
            data=data,
            hint=_hint_for(name, message),
        )

    def run(
        self,
        args: Sequence[str],
        *,
        permit: Permit | None = None,
        timeout: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        accept_statuses: Iterable[int] = (0,),
    ) -> Any:
        """Run a command and return the ``result`` part of its JSON envelope."""
        return self.run_envelope(
            args, permit=permit, timeout=timeout, cwd=cwd, accept_statuses=accept_statuses
        ).get("result")

    # -- connecting an org -----------------------------------------------------------------

    def connect_with_auth_url(self, alias: str, auth_url: str | AuthUrl, *, timeout: float = 180.0) -> dict[str, Any]:
        """Save an org's authorization on this computer from a Salesforce DX authorization URL.

        Runs ``sf org login sfdx-url --sfdx-url-stdin - --alias <alias> --json`` with the URL on
        standard input, so it never appears in a process list, a log or an error message.

        It changes nothing in any org: it is the one command here that is neither a read nor an
        org write, which is why it has its own method and is not on the read allowlist
        (``run`` still treats ``org login`` as a write). Claude Code is not given it: only the
        local page calls it, when the user pastes an authorization there.
        """
        validate_target_org(alias)
        auth = auth_url if isinstance(auth_url, AuthUrl) else read_auth_url(auth_url)
        args = ["org", "login", "sfdx-url", "--sfdx-url-stdin", "-", "--alias", alias, "--json"]
        self._check_arguments(args)
        exe = self.executable()
        label = "sf org login sfdx-url"
        try:
            proc = self._runner([exe, *args], None, timeout, self._environment(), input_text=auth.value + "\n")
        except TimeoutError:
            raise SfCliTimeoutError(
                f"Connecting to {auth.host} did not finish within {timeout:.0f}s and was stopped.",
                hint=_connect_hint("network", auth.host),
            ) from None
        except FileNotFoundError:
            raise SfCliNotFoundError(
                f"Could not start the Salesforce CLI at {exe!r}.",
                hint="Check that 'sf --version' works in a terminal.",
            ) from None
        except OSError as exc:
            raise SfCliError(f"Could not start '{label}': {exc}", command=label) from None

        envelope = parse_json_output(proc.stdout)
        if envelope is None:
            raise SfCliOutputError(
                f"'{label}' did not return JSON (exit code {proc.returncode}).",
                details={"stdout": _without_secrets(_tail(proc.stdout), auth),
                         "stderr": _without_secrets(_tail(proc.stderr), auth)},
            )
        envelope = scrub_secrets(envelope)
        status = envelope.get("status", proc.returncode)
        result = envelope.get("result")
        if status == 0 and isinstance(result, dict) and result.get("orgId"):
            return {"alias": alias, "org_id": str(result["orgId"]), "host": auth.host}
        message = _without_secrets(
            str(envelope.get("message") or f"'{label}' failed with status {status}.").strip(), auth
        )
        raise SfCliError(
            message,
            name=str(envelope.get("name") or envelope.get("code") or "Error"),
            exit_code=status if isinstance(status, int) else proc.returncode,
            command=label,
            hint=_connect_hint(message, auth.host),
        )


def _hint_for(name: str, message: str) -> str | None:
    text = f"{name} {message}"
    if "NamedOrgNotFound" in text or "No authorization information found" in text:
        return "That org is not authenticated on this machine. Run list_orgs, or 'sf org login web --alias <alias>'."
    if "INVALID_SESSION_ID" in text or "expired" in text.lower() and "token" in text.lower():
        return "The org session has expired. Re-authenticate with 'sf org login web --alias <alias>'."
    if "MALFORMED_QUERY" in text or "INVALID_FIELD" in text or "INVALID_TYPE" in text:
        return "Check the object and field API names with describe_object."
    if "Nonexistent flag" in text:
        return "The installed CLI does not support a flag this tool uses. Run 'python -m core.prereqs'."
    return None
