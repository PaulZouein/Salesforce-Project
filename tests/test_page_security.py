"""Report text cannot run code in the page: escaped links, no inline script, a sandboxed printable report.

Found by an independent safety review: a Markdown link whose address contained a quote closed the href
and added an event handler, which ran in the page that holds the API token.
"""

from __future__ import annotations

import re
import threading
import urllib.request

import pytest

from core.report import markdown_to_html
from webapp.server import PAGE_CSP, REPORT_CSP, STATIC, App, create_server

ATTACK = ('[here](https://e.invalid/"style="animation-name:spin"onanimationstart="location=\'javascript:alert(1)\'")')


def test_a_link_cannot_add_attributes_or_run_script():
    html = markdown_to_html(f"Root cause: see {ATTACK}.")
    assert "&quot;" in html and "<a " not in html     # the attack stays text: no link is made
    for tag in re.findall(r"<a\b[^>]*>", html):
        assert re.fullmatch(r'<a href="https?://[^"<>\s]*" rel="noopener noreferrer">', tag), tag
    assert not re.search(r"<[^>]+\son\w+=", html), html
    assert "javascript:" not in "".join(re.findall(r'href="([^"]*)"', html))


@pytest.mark.parametrize("markdown", [
    "[x](javascript:alert(1))",
    "[x](data:text/html,<script>alert(1)</script>)",
    "[x](https://ok.example/a\"onmouseover=\"x)",
    "<img src=x onerror=alert(1)>",
    "`<script>alert(1)</script>`",
])
def test_nothing_but_plain_web_links_becomes_markup(markdown):
    html = markdown_to_html(markdown)
    assert "<script" not in html and "<img" not in html
    assert not re.search(r"<[^>]+\son\w+=", html)
    assert all(h.startswith(("http://", "https://")) for h in re.findall(r'href="([^"]*)"', html))


def test_ordinary_links_and_query_strings_still_work():
    html = markdown_to_html("See [the docs](https://help.example.com/s/article?id=1&lang=en).")
    assert '<a href="https://help.example.com/s/article?id=1&amp;lang=en" rel="noopener noreferrer">the docs</a>' in html


def test_the_page_runs_no_inline_script():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script\b[^>]*>", page) == ['<script src="static/app.js">']
    assert [body.strip() for body in re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S)] == [""]
    script_src = re.search(r"script-src ([^;]+)", PAGE_CSP).group(1)
    assert script_src.strip() == "'self'" and "unsafe-inline" not in script_src


def _serve(app):
    server = create_server(app, 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_the_server_sends_the_policies_and_the_script(project, tmp_path):
    app = App(root=project.root, demo=True, demo_state=tmp_path / "state")
    server, base = _serve(app)
    try:
        with urllib.request.urlopen(base + "/") as page:
            assert page.headers["Content-Security-Policy"] == PAGE_CSP
            assert app.token in page.read().decode()
        with urllib.request.urlopen(base + "/static/app.js") as script:
            assert script.headers["Content-Type"].startswith("text/javascript")
            assert "meta[name=\"sfti-token\"]" in script.read().decode()
        folder = project.root / "tickets" / "SEC-1"
        folder.mkdir(parents=True)
        (folder / "report.html").write_text("<html><body><script>alert(1)</script></body></html>", encoding="utf-8")
        with urllib.request.urlopen(base + "/tickets/SEC-1/report.html") as report:
            policy = report.headers["Content-Security-Policy"]
        assert policy == REPORT_CSP and policy.startswith("sandbox;") and "script-src" not in policy
        assert "default-src 'none'" in policy
    finally:
        server.shutdown()
        server.server_close()
