"""Connecting an org from an authorization URL pasted on the page, and the page behind a trusted proxy.

The authorization URL works like a password. These tests check that it only ever travels on the
Salesforce CLI's standard input: never in its arguments, an answer, an error or the window.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

import pytest

from core.errors import InputValidationError, SfCliError, SfCliOutputError, SfCliTimeoutError, WritePermitError
from core.sf_cli import ProcessResult, SfCli, default_runner, read_auth_url
from webapp import __main__ as web_main
from webapp.server import STATIC, App, check_allowed_host, create_server

TOKEN = "5Aep861TSESvWeug_xvFHRBTTbf_YrTWgEyjBJAbCdEfGhIjKlMnOpQrStUvWx.yZ0123456789"
HOST = "acme-dev-ed.develop.my.salesforce.com"
URL = f"force://PlatformCLI::{TOKEN}@{HOST}"
ORG_ID = "00D5g000000AbCdEAG"


class LoginRunner:
    """Stands in for the subprocess runner of ``sf org login sfdx-url``."""

    def __init__(self, result: ProcessResult | BaseException | None = None):
        self.calls: list[dict] = []
        self.result = result or ProcessResult(0, json.dumps({"status": 0, "result": {
            "orgId": ORG_ID, "username": "someone@example.com", "instanceUrl": f"https://{HOST}",
            "accessToken": "00D!secret-access", "refreshToken": TOKEN}}), "")

    def __call__(self, argv, cwd, timeout, env, *, input_text=None):
        if list(argv[1:3]) == ["org", "list"]:     # the page first checks whether the name is already in use
            return ProcessResult(0, json.dumps({"status": 0, "result": {"other": [{"alias": "taken", "username": "t@x.example"}]}}), "")
        self.calls.append({"argv": list(argv), "input": input_text, "env": env})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _shown(value) -> str:
    return json.dumps(value, default=str) if not isinstance(value, str) else value


# --- reading the authorization URL --------------------------------------------------------


@pytest.mark.parametrize("pasted, host", [
    (URL, HOST),
    (f"  {URL}\n", HOST),
    (f'"{URL}"', HOST),
    (f"force://PlatformCLI:abc123secretXYZ:{TOKEN}@{HOST}", HOST),
    (f"force://3MVG9.client_id==::{TOKEN}@ACME.my.salesforce.com:8443/", "acme.my.salesforce.com"),
])
def test_an_authorization_url_is_read_and_only_its_host_is_shown(pasted, host):
    auth = read_auth_url(pasted)
    assert auth.host == host and auth.value.startswith("force://") and auth.value.endswith(("com", "/"))
    assert TOKEN not in repr(auth) and "force://" not in repr(auth)


@pytest.mark.parametrize("pasted", [
    "", "   ", None, 42,
    TOKEN,                                            # a refresh token alone
    "00D5g000000AbCd!AQ4AQFakeAccessToken",           # an access token
    f"https://{HOST}",
    f"force://PlatformCLI::{TOKEN}",                  # no org address
    f"force://PlatformCLI::{TOKEN}@localhost",        # not an org address
    f"force://PlatformCLI::{TOKEN}@{HOST} and more",
    f"force://PlatformCLI::{TOKEN}@{HOST}\nforce://second",
    f"force://PlatformCLI::{TOKEN}@{HOST}/path",
    "force://" + "a" * 5000 + f"::{TOKEN}@{HOST}",
])
def test_anything_else_is_refused_without_repeating_it(pasted):
    with pytest.raises(InputValidationError) as caught:
        read_auth_url(pasted)
    shown = _shown(caught.value.to_dict())
    assert TOKEN not in shown and "force://Platform" not in shown and "FakeAccessToken" not in shown


# --- the command ----------------------------------------------------------------------------


def test_the_url_goes_to_the_cli_on_standard_input_only():
    runner = LoginRunner()
    connected = SfCli(executable="sf-test", runner=runner).connect_with_auth_url("my-dev", URL)
    assert connected == {"alias": "my-dev", "org_id": ORG_ID, "host": HOST}
    (call,) = runner.calls
    assert call["argv"] == ["sf-test", "org", "login", "sfdx-url", "--sfdx-url-stdin", "-", "--alias", "my-dev", "--json"]
    assert call["input"] == URL + "\n"
    assert TOKEN not in " ".join(call["argv"]) and TOKEN not in _shown(connected)
    assert call["env"]["SF_DISABLE_TELEMETRY"] == "true"


def test_a_bad_alias_or_url_runs_nothing():
    runner = LoginRunner()
    cli = SfCli(executable="sf-test", runner=runner)
    for alias, url in (("my dev", URL), ("x;rm", URL), ("-flag", URL), ("my-dev", "not a url")):
        with pytest.raises(InputValidationError):
            cli.connect_with_auth_url(alias, url)
    assert runner.calls == []


def test_login_is_still_a_write_for_the_generic_runner():
    """The dedicated method is the only way to run it: through run(), 'org login' needs a write permit."""
    runner = LoginRunner()
    with pytest.raises(WritePermitError):
        SfCli(executable="sf-test", runner=runner).run(["org", "login", "sfdx-url", "--sfdx-url-file", "auth.json"])
    assert runner.calls == []


@pytest.mark.parametrize("message, expected", [
    (f"Error authenticating with the refresh token due to: request to https://{HOST}/services/oauth2/token failed, "
     "reason: tunneling socket could not be established, statusCode=403", f"could not reach {HOST}"),
    ("Error authenticating with the refresh token due to: expired access/refresh token", "revoked, or it expired"),
    ("Something else went wrong", "sf org list"),
])
def test_failures_explain_themselves_and_never_carry_the_url(message, expected):
    leaky = f"{message} (while using {URL}, token {TOKEN})"
    runner = LoginRunner(ProcessResult(1, json.dumps({"status": 1, "name": "RefreshTokenAuthError",
                                                      "message": leaky}), f"stderr with {TOKEN}"))
    with pytest.raises(SfCliError) as caught:
        SfCli(executable="sf-test", runner=runner).connect_with_auth_url("my-dev", URL)
    shown = _shown(caught.value.to_dict())
    assert expected in shown and TOKEN not in shown and "force://PlatformCLI" not in shown
    assert caught.value.to_dict()["command"] == "sf org login sfdx-url"


def test_unreadable_output_and_timeouts_carry_no_secret():
    runner = LoginRunner(ProcessResult(1, f"crash near {URL}", f"trace {TOKEN}"))
    with pytest.raises(SfCliOutputError) as caught:
        SfCli(executable="sf-test", runner=runner).connect_with_auth_url("my-dev", URL)
    assert TOKEN not in _shown(caught.value.to_dict())
    with pytest.raises(SfCliTimeoutError) as caught:
        SfCli(executable="sf-test", runner=LoginRunner(TimeoutError())).connect_with_auth_url("my-dev", URL, timeout=5)
    assert HOST in caught.value.message and TOKEN not in _shown(caught.value.to_dict())


def test_a_token_cut_by_the_length_limit_leaves_nothing_behind():
    """Found by an independent review: output was cut to 600 characters before the token was removed, so a cut
    through the token left part of it in the error the page shows."""
    for pad in range(0, 700, 37):
        noisy = ("x" * pad) + f" while using {URL} " + ("y" * (560 - len(TOKEN) // 2))
        runner = LoginRunner(ProcessResult(1, noisy, noisy))
        with pytest.raises(SfCliOutputError) as caught:
            SfCli(executable="sf-test", runner=runner).connect_with_auth_url("my-dev", URL)
        shown = _shown(caught.value.to_dict())
        assert not any(TOKEN[i:i + 12] in shown for i in range(0, len(TOKEN) - 12)), pad
    runner = LoginRunner(ProcessResult(1, json.dumps({"status": 1, "name": f"Bad {TOKEN}", "message": "no"}), ""))
    with pytest.raises(SfCliError) as caught:
        SfCli(executable="sf-test", runner=runner).connect_with_auth_url("my-dev", URL)
    assert TOKEN not in _shown(caught.value.to_dict())


def test_the_default_runner_writes_standard_input(tmp_path):
    script = "import sys, json; print(json.dumps({'got': sys.stdin.read()}))"
    result = default_runner([sys.executable, "-c", script], str(tmp_path), 60, {}, input_text="hello\n")
    assert json.loads(result.stdout) == {"got": "hello\n"}
    # Without input, standard input is empty, as before.
    result = default_runner([sys.executable, "-c", script], str(tmp_path), 60, {})
    assert json.loads(result.stdout) == {"got": ""}


# --- the page -------------------------------------------------------------------------------


class Page:
    def __init__(self, app: App):
        self.app = app
        self.server = create_server(app, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def call(self, method, path, body=None, headers=None, token=True):
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        if token:
            all_headers["X-SFTI-Token"] = self.app.token
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def page(project, monkeypatch):
    monkeypatch.setenv("SFTI_SF_EXECUTABLE", "sf-test")  # never the real CLI: the runner below stands in
    app = App(root=project.root, allowed_hosts=["preview.example.com"])
    runner = LoginRunner()
    app.investigator().cli._runner = runner
    page = Page(app)
    page.runner = runner
    yield page
    page.close()


def test_connecting_from_the_page(page, capfd):
    status, raw = page.call("POST", "/api/orgs/connect", {"alias": " my-dev ", "auth_url": URL})
    assert status == 200, raw
    assert json.loads(raw)["connected"] == {"alias": "my-dev", "org_id": ORG_ID, "host": HOST}
    assert page.runner.calls[0]["input"] == URL + "\n"
    window = capfd.readouterr().err
    assert f"Connected the org 'my-dev' ({ORG_ID}) at {HOST}" in window
    assert TOKEN not in raw and TOKEN not in window


def test_the_page_refuses_bad_input_and_foreign_requests(page, capfd):
    status, raw = page.call("POST", "/api/orgs/connect", {"alias": "my-dev", "auth_url": TOKEN})
    assert status == 400 and "not a Salesforce authorization URL" in raw and TOKEN not in raw
    status, raw = page.call("POST", "/api/orgs/connect", {"alias": "", "auth_url": URL})
    assert status == 400 and "short name" in raw
    status, _ = page.call("POST", "/api/orgs/connect", {"alias": "my-dev", "auth_url": URL}, token=False)
    assert status == 403
    status, _ = page.call("POST", "/api/orgs/connect", {"alias": "my-dev", "auth_url": URL},
                          headers={"Origin": "https://evil.example.net"})
    assert status == 403
    assert page.runner.calls == []
    assert TOKEN not in capfd.readouterr().err


def test_a_failed_connection_says_why_without_the_url(page, capfd):
    page.runner.result = ProcessResult(1, json.dumps({"status": 1, "name": "RefreshTokenAuthError", "message":
                                                      f"tunneling socket could not be established, statusCode=403 {URL}"}), "")
    status, raw = page.call("POST", "/api/orgs/connect", {"alias": "my-dev", "auth_url": URL})
    assert status == 502 and f"could not reach {HOST}" in raw and TOKEN not in raw
    window = capfd.readouterr().err
    assert "was not connected" in window and TOKEN not in window


def test_the_demo_does_not_connect_real_orgs(project, tmp_path, monkeypatch):
    monkeypatch.delenv("SFTI_SF_EXECUTABLE", raising=False)
    page = Page(App(root=project.root, demo=True, demo_state=tmp_path / "demo-state"))
    try:
        status, raw = page.call("POST", "/api/orgs/connect", {"alias": "my-dev", "auth_url": URL})
        assert status == 400 and "simulated orgs" in raw
    finally:
        page.close()


def test_only_the_allowed_proxy_name_is_answered(page, capfd):
    assert page.call("GET", "/api/status", headers={"Host": "preview.example.com"})[0] == 200
    assert page.call("GET", "/api/status", headers={"Host": "PREVIEW.example.com:443"})[0] == 200
    assert page.call("GET", "/api/status", headers={"Host": "other.example.com"})[0] == 403
    assert page.call("GET", "/api/status", headers={"Host": "other.example.com"})[0] == 403
    status, _ = page.call("POST", "/api/demo", {"enabled": False}, headers={"Origin": "https://preview.example.com"})
    assert status == 200
    window = capfd.readouterr().err
    assert window.count("--allow-host other.example.com") == 1  # said once, not for every request


@pytest.mark.parametrize("name", ["*.example.com", "localhost", "127.0.0.1", "http://x.example.com", "x.example.com:80",
                                  "", "a b.example.com", "-x.example.com"])
def test_allowed_names_are_exact_host_names(name):
    with pytest.raises(InputValidationError):
        check_allowed_host(name)


def test_the_start_command_refuses_a_bad_allowed_name(capsys):
    assert web_main.main(["--no-browser", "--allow-host", "*.example.com"]) == 2
    assert "not a host name" in capsys.readouterr().err


def test_the_page_offers_the_paste_option_and_asks_relative_addresses():
    page = "".join((STATIC / name).read_text(encoding="utf-8") for name in ("index.html", "app.js"))
    assert "Paste an authorization instead" in page and 'type="password" id="auth-url"' in page
    assert "sf org auth show-sfdx-auth-url --target-org" in page
    assert 'fetch(path.replace(/^\\//, ""), init)' in page
    assert 'href="/tickets/' not in page


# --- logging in with the browser, from the page ---------------------------------------------


@pytest.mark.parametrize("given, url", [
    ("https://login.salesforce.com", "https://login.salesforce.com"),
    ("test.salesforce.com/", "https://test.salesforce.com"),
    (f"https://{HOST.upper()}/", f"https://{HOST}"),
])
def test_a_login_address_is_normalized(given, url):
    from core.sf_cli import validate_login_url

    assert validate_login_url(given) == url


@pytest.mark.parametrize("given, fragment", [
    ("https://acme.lightning.force.com", "Lightning address"),
    ("https://evil.example.com", "not a Salesforce login address"),
    ("https://salesforce.com.evil.example.com", "not a Salesforce login address"),
    ("https://acme.my.salesforce.com/path?x=1", "not a Salesforce login address"),
])
def test_other_login_addresses_are_refused(given, fragment):
    from core.sf_cli import validate_login_url

    with pytest.raises(InputValidationError) as refused:
        validate_login_url(given)
    assert fragment in refused.value.message


def test_logging_in_from_the_page_runs_the_browser_login_without_prompts(page, capfd):
    status, raw = page.call("POST", "/api/orgs/login", {"alias": "my-dev", "kind": "developer"})
    assert status == 200, raw
    assert json.loads(raw)["connected"] == {"alias": "my-dev", "org_id": ORG_ID, "host": "login.salesforce.com",
                                            "username": "someone@example.com"}
    argv = page.runner.calls[0]["argv"]
    assert argv[1:] == ["org", "login", "web", "--alias", "my-dev", "--instance-url", "https://login.salesforce.com",
                        "--no-prompt", "--json"]
    assert TOKEN not in raw and "finish in the browser window" in capfd.readouterr().err
    status, raw = page.call("POST", "/api/orgs/login", {"alias": "acme", "kind": "custom", "domain": HOST})
    assert status == 200 and page.runner.calls[1]["argv"][7] == f"https://{HOST}"


def test_the_page_login_asks_before_reusing_a_name_and_refuses_bad_input(page):
    status, raw = page.call("POST", "/api/orgs/login", {"alias": "taken", "kind": "sandbox"})
    assert status == 400 and json.loads(raw)["error"]["details"] == {"alias_in_use": "taken"}
    assert page.call("POST", "/api/orgs/login", {"alias": "taken", "kind": "sandbox", "replace": True})[0] == 200
    for body, fragment in (({"alias": "x", "kind": "mars"}, "Choose what kind"),
                           ({"alias": "", "kind": "sandbox"}, "short name"),
                           ({"alias": "x", "kind": "custom", "domain": "https://x.lightning.force.com"}, "Lightning")):
        status, raw = page.call("POST", "/api/orgs/login", body)
        assert status == 400 and fragment in raw, body
    assert page.call("POST", "/api/orgs/login", {"alias": "x", "kind": "sandbox"}, token=False)[0] == 403


def test_a_login_left_unfinished_stops_with_a_clear_message(page):
    page.runner.result = TimeoutError()
    status, raw = page.call("POST", "/api/orgs/login", {"alias": "my-dev", "kind": "sandbox"})
    assert status != 200 and "was not finished within 5 minutes" in raw
