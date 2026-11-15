"""Phase 5, optional: the read-only Jira reader. No network: the HTTP opener is injected."""

from __future__ import annotations

import inspect
import json
import urllib.error

import pytest

from core import jira
from core.config import Settings
from core.errors import InputValidationError

TOKEN = "pat-SECRET-TOKEN-VALUE"
ENABLED = Settings(jira_base_url="https://jira.example.com/jira")

ISSUE = {
    "key": "ABC-123",
    "fields": {
        "summary": "Premium cases not High",
        "description": "Reported by jane.doe@customer.test. See [~jdoe] for details.",
        "issuetype": {"name": "Bug"}, "status": {"name": "Open"}, "priority": {"name": "High"},
        "created": "2026-11-12T09:40:00.000+0000", "updated": "2026-11-12T15:02:00.000+0000",
        "labels": ["salesforce"], "components": [{"name": "Service Cloud"}],
        "reporter": {"name": "jdoe", "displayName": "Jane Doe", "emailAddress": "jane.doe@customer.test"},
        "assignee": {"name": "asmith", "displayName": "Alex Smith", "emailAddress": "alex.smith@corp.test"},
        "comment": {"comments": [
            {"created": "2026-11-12T10:05:00.000+0000", "author": {"name": "bkim", "displayName": "Bo Kim"},
             "body": "I think the flow is broken. Mail me: bo.kim@corp.test"},
            {"created": "2026-11-12T10:32:00.000+0000", "author": {"name": "jdoe", "displayName": "Jane Doe"},
             "body": "The flow was last activated on 20 October."},
            {"created": "2026-11-12T11:15:00.000+0000", "author": {"name": "bkim", "displayName": "Bo Kim"},
             "body": "All wrong cases were created at 2 am."},
        ]},
        "attachment": [{"filename": "error.png", "size": 1234, "created": "2026-11-12T09:41:00.000+0000",
                        "author": {"displayName": "Jane Doe"}, "content": "https://jira.example.com/secure/1"}],
        "issuelinks": [{"type": {"name": "Relates"}, "outwardIssue": {"key": "ABC-100"}}],
    },
}


class FakeJira:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.error = status, error
        self.body = json.dumps(ISSUE if body is None else body).encode() if not isinstance(body, bytes) else body
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.status, self.body


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv(jira.TOKEN_ENV, TOKEN)


def test_off_by_default_and_no_request_is_made(monkeypatch):
    fake = FakeJira()
    with pytest.raises(jira.JiraDisabledError) as err:
        jira.get_ticket(Settings(), "ABC-123", opener=fake)
    assert "Paste the ticket" in err.value.hint
    monkeypatch.delenv(jira.TOKEN_ENV)
    with pytest.raises(jira.JiraDisabledError):
        jira.get_ticket(ENABLED, "ABC-123", opener=fake)
    assert fake.requests == []
    assert jira.status(Settings()) == {"enabled": False, "base_url_configured": False, "token_present": False,
                                       "read_only": True}


def test_one_get_request_to_the_configured_host():
    fake = FakeJira()
    jira.get_ticket(ENABLED, "ABC-123", opener=fake)
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.full_url.startswith("https://jira.example.com/jira/rest/api/2/issue/ABC-123?fields=")
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"


def test_people_become_roles_and_emails_are_masked():
    out = jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira())
    assert (out["key"], out["type"], out["status"], out["priority"]) == ("ABC-123", "Bug", "Open", "High")
    assert out["reporter"] == "reporter" and out["assignee"] == "assignee"
    assert [c["who"] for c in out["comments"]] == ["commenter-1", "reporter", "commenter-1"]
    assert out["description"] == "Reported by [email]. See [user] for details."
    assert out["comments"][0]["body"] == "I think the flow is broken. Mail me: [email]"
    assert out["attachments"] == [{"filename": "error.png", "size": 1234, "created": "2026-11-12T09:41:00.000+0000"}]
    assert out["links"] == [{"type": "Relates", "key": "ABC-100"}]
    text = json.dumps(out)
    for personal in ("Jane", "Doe", "Alex", "Smith", "Bo Kim", "jdoe", "asmith", "bkim", "@customer.test", "@corp.test"):
        assert personal not in text
    assert TOKEN not in text


def test_comment_limit():
    out = jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira(), max_comments=1)
    assert len(out["comments"]) == 1 and out["comment_count"] == 3 and out["comments_truncated"] is True


@pytest.mark.parametrize("key", ["", "abc-1", "ABC", "ABC-1/../../admin", "ABC-1?expand=x", "ABC-1 OR 1=1", None, 5,
                                 "../rest/api/2/user"])
def test_key_validation_blocks_path_tricks(key):
    fake = FakeJira()
    with pytest.raises(InputValidationError):
        jira.get_ticket(ENABLED, key, opener=fake)
    assert fake.requests == []


@pytest.mark.parametrize("status,fragment", [(401, "refused"), (403, "refused"), (404, "not found"),
                                             (500, "HTTP 500"), (302, "redirect")])
def test_http_errors_are_typed_and_never_show_the_token(status, fragment):
    with pytest.raises(jira.JiraError) as err:
        jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira(status=status))
    assert fragment in err.value.message
    assert TOKEN not in json.dumps(err.value.to_dict())


def test_network_and_format_errors():
    with pytest.raises(jira.JiraError) as err:
        jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira(error=urllib.error.URLError(f"boom {TOKEN}")))
    assert TOKEN not in err.value.message and "[token]" in err.value.message
    with pytest.raises(jira.JiraError):
        jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira(body=b"<html>login</html>"))
    with pytest.raises(jira.JiraError):
        jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira(body=[1, 2]))
    http_error = urllib.error.HTTPError("https://jira.example.com", 403, "Forbidden", {}, None)
    with pytest.raises(jira.JiraError):
        jira.get_ticket(ENABLED, "ABC-123", opener=FakeJira(error=http_error))


def test_base_url_validation():
    assert jira.validate_base_url("https://jira.example.com/") == "https://jira.example.com"
    for bad in ("http://jira.example.com", "https://u:p@jira.example.com", "jira.example.com", "https://x/?a=b"):
        with pytest.raises(InputValidationError):
            jira.validate_base_url(bad)


def test_redirects_are_not_followed():
    handler = jira._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/") is None


def test_the_module_can_only_read():
    """No function writes to Jira and the only HTTP method in the source is GET."""
    public = {name for name, obj in vars(jira).items() if inspect.isfunction(obj) and not name.startswith("_")}
    assert public == {"get_ticket", "status", "validate_base_url"}
    source = inspect.getsource(jira)
    for method in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"', "'POST'", "'PUT'", "'DELETE'"):
        assert method not in source
    assert source.count('method="GET"') == 1 and "data=" not in source


def test_only_a_read_tool_is_exposed(inv):
    from mcp_server import server
    jira_tools = [name for name in server.TOOLS if "jira" in name]
    assert jira_tools == ["jira_get_ticket"] and "jira_get_ticket" in server.READ_ONLY_TOOLS
    config = inv.get_config()
    assert config["jira"]["enabled"] is False and config["jira"]["read_only"] is True
    assert TOKEN not in json.dumps(config)
    with pytest.raises(jira.JiraDisabledError):
        inv.jira_get_ticket("ABC-123")
