"""Phase 5, optional: read one Jira ticket. Read-only, and off unless the user configures it.

Scope, deliberately small:

* Jira Server / Data Center only, through its REST API with a personal access token. For Jira
  Cloud use Atlassian's own MCP server (see docs/GUIDE.md).
* One operation: fetch a ticket (fields and comments). There is no function here that creates,
  edits, comments on or transitions a ticket, and the HTTP helper can only send ``GET``.
* Off by default. It works only when the user has set ``jira_base_url`` in ``sfti.config.json``
  and exported a token in the ``SFTI_JIRA_TOKEN`` environment variable. The token is never
  written to a file, a result or an error message.
* People are returned as roles (``reporter``, ``assignee``, ``commenter-1``…), not names, and
  email addresses in the text are masked: ticket text goes into ``ticket.md``, which must not
  hold personal data.

Not verified against a live Jira instance: the request and response shapes follow the Jira
Server REST API v2 documentation.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import Settings
from .errors import InputValidationError, SfInvestigatorError

TOKEN_ENV = "SFTI_JIRA_TOKEN"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_COMMENTS = 200
FIELDS = "summary,description,status,priority,issuetype,created,updated,resolution,labels,components," \
         "environment,reporter,assignee,comment,attachment,issuelinks"

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}-\d{1,9}\Z")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_MENTION_RE = re.compile(r"\[~[^\]]+\]")


class JiraDisabledError(SfInvestigatorError):
    code = "jira_disabled"


class JiraError(SfInvestigatorError):
    code = "jira_error"


#: ``opener(request, timeout)`` returns ``(status, body_bytes)``. Injected in tests.
Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the token must not be sent to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return response.status, response.read(MAX_RESPONSE_BYTES + 1)


def validate_base_url(value: str) -> str:
    """Return the normalized base URL, or raise if it is not a plain https URL."""
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise InputValidationError("jira_base_url must be an https URL, for example https://jira.example.com.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InputValidationError("jira_base_url must not contain credentials, a query or a fragment.")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def status(settings: Settings) -> dict[str, Any]:
    """Whether the optional Jira reader is usable, without revealing the token."""
    configured = bool(settings.jira_base_url)
    return {
        "enabled": configured and bool(os.environ.get(TOKEN_ENV)),
        "base_url_configured": configured,
        "token_present": bool(os.environ.get(TOKEN_ENV)),
        "read_only": True,
    }


def _mask(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    return _MENTION_RE.sub("[user]", _EMAIL_RE.sub("[email]", text))


def _name(value: Any) -> Any:
    return value.get("name") if isinstance(value, dict) else None


def get_ticket(settings: Settings, ticket_key: str, *, opener: Opener | None = None,
               max_comments: int = 50) -> dict[str, Any]:
    """Fetch one ticket from Jira Server / Data Center. Sends a single ``GET`` request."""
    if not settings.jira_base_url:
        raise JiraDisabledError(
            "The Jira reader is off (Phase 5, optional).",
            hint='Paste the ticket instead, or set "jira_base_url" in sfti.config.json yourself and export '
                 f"{TOKEN_ENV} with a personal access token. It only ever reads.",
        )
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise JiraDisabledError(
            f"The Jira reader has a base URL but no token: {TOKEN_ENV} is not set.",
            hint="Create a personal access token in Jira and export it before starting Claude Code.",
        )
    if not isinstance(ticket_key, str) or not _KEY_RE.match(ticket_key):
        raise InputValidationError(f"Invalid Jira key: {ticket_key!r}.", hint="For example ABC-123.")
    max_comments = max(0, min(int(max_comments), MAX_COMMENTS))
    base = validate_base_url(settings.jira_base_url)
    url = f"{base}/rest/api/2/issue/{ticket_key}?fields={FIELDS}"
    request = urllib.request.Request(  # method fixed to GET: this module never writes to Jira
        url, method="GET", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    try:
        code, body = (opener or _default_opener)(request, TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        code, body = exc.code, b""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise JiraError(f"Could not reach Jira: {str(reason).replace(token, '[token]')}") from None
    if code in (301, 302, 303, 307, 308):
        raise JiraError("Jira answered with a redirect, which is not followed so the token stays with the "
                        "configured host.", hint="Check jira_base_url (scheme, host and context path).")
    if code == 401 or code == 403:
        raise JiraError(f"Jira refused the request (HTTP {code}).",
                        hint=f"Check the token in {TOKEN_ENV} and that it may read {ticket_key}.")
    if code == 404:
        raise JiraError(f"{ticket_key} was not found in Jira (HTTP 404), or the token cannot see it.")
    if code != 200:
        raise JiraError(f"Jira answered HTTP {code}.")
    if len(body) > MAX_RESPONSE_BYTES:
        raise JiraError("The Jira response is too large to read.")
    try:
        issue = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        raise JiraError("Jira did not return JSON.", hint="Check jira_base_url: it may point at a login page.") from None
    if not isinstance(issue, dict):
        raise JiraError("Unexpected response from Jira.")

    fields = issue.get("fields") or {}
    roles: dict[str, str] = {}

    def role(person: Any, default: str | None = None) -> str | None:
        if not isinstance(person, dict):
            return None
        identity = str(person.get("key") or person.get("name") or person.get("accountId") or
                       person.get("displayName") or "")
        if identity not in roles:
            roles[identity] = default or f"commenter-{sum(1 for r in roles.values() if r.startswith('commenter-')) + 1}"
        return roles[identity]

    reporter = role(fields.get("reporter"), "reporter")
    assignee = role(fields.get("assignee"), "assignee")
    all_comments = ((fields.get("comment") or {}).get("comments")) or []
    comments = [
        {"when": c.get("created"), "who": role(c.get("author")), "body": _mask(c.get("body"))}
        for c in all_comments[:max_comments] if isinstance(c, dict)
    ]
    out: dict[str, Any] = {
        "key": issue.get("key") or ticket_key,
        "summary": _mask(fields.get("summary")),
        "type": _name(fields.get("issuetype")),
        "status": _name(fields.get("status")),
        "priority": _name(fields.get("priority")),
        "resolution": _name(fields.get("resolution")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "labels": fields.get("labels") or [],
        "components": [_name(c) for c in fields.get("components") or [] if _name(c)],
        "environment": _mask(fields.get("environment")),
        "reporter": reporter,
        "assignee": assignee,
        "description": _mask(fields.get("description")),
        "comments": comments,
        "comment_count": len(all_comments),
        "attachments": [
            {"filename": a.get("filename"), "size": a.get("size"), "created": a.get("created")}
            for a in fields.get("attachment") or [] if isinstance(a, dict)
        ],
        "links": [
            {"type": _name(l.get("type")), "key": (l.get("outwardIssue") or l.get("inwardIssue") or {}).get("key")}
            for l in fields.get("issuelinks") or [] if isinstance(l, dict)
        ],
        "source": "Jira (read-only)",
        "people": "People are shown as roles, and email addresses and mentions in the text are masked.",
        "next_step": "Use intake-ticket to write tickets/<KEY>/ticket.md from this. Attachments are not "
                     "downloaded: ask the user to drop the ones that matter into attachments/.",
    }
    if len(all_comments) > max_comments:
        out["comments_truncated"] = True
    return out
