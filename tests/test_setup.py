"""Getting started without a manual: the start scripts, the set-up step behind them and the release zip."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import bootstrap  # noqa: E402
import make_release  # noqa: E402

from webapp import __main__ as entry  # noqa: E402
from webapp.server import App, create_server  # noqa: E402

START_FILES = ("start.bat", "start-demo.bat", "claude-here.bat", "start.command", "start-demo.command",
               "claude-here.command")


# --- the start scripts ----------------------------------------------------------------------


def test_start_files_exist_for_both_systems_and_lead_to_the_same_set_up():
    for name in START_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert len(text.splitlines()) < 45, f"{name} should stay short enough to read before running it"
    assert "scripts\\bootstrap.py %*" in (ROOT / "start.bat").read_text(encoding="utf-8")
    assert 'scripts/bootstrap.py "$@"' in (ROOT / "start.command").read_text(encoding="utf-8")
    for name, option in (("start-demo.bat", "--demo"), ("claude-here.bat", "--claude"),
                         ("start-demo.command", "--demo"), ("claude-here.command", "--claude")):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert option in text and ("start.bat" in text or "start.command" in text)
    for name in START_FILES:
        if name.endswith(".command"):
            assert (ROOT / name).read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    # Neither script installs anything system-wide or asks for elevated rights.
    everything = "".join((ROOT / name).read_text(encoding="utf-8") for name in START_FILES).lower()
    for word in ("sudo", "runas", "curl ", "powershell", "invoke-webrequest", "npm install"):
        assert word not in everything


def test_the_start_script_of_this_system_finds_python_and_reaches_the_set_up():
    """Runs the real script. ``--help`` stops before anything is created."""
    if os.name == "nt":
        command = ["cmd", "/c", str(ROOT / "start.bat"), "--help"]
    else:
        command = ["sh", str(ROOT / "start.command"), "--help"]
    result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Set up and start the Salesforce Ticket Investigator" in result.stdout and "--demo" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="the macOS script; start.bat is exercised on Windows")
def test_start_script_explains_what_to_do_when_python_is_missing(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "start.command"
    # The same script, with no usable Python anywhere it looks.
    lines = (ROOT / "start.command").read_text(encoding="utf-8").splitlines()
    position = next(i for i, line in enumerate(lines) if line.startswith('PATH="$PATH:'))
    lines[position] = f'PATH="{tmp_path}"'
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            env={"PATH": str(tmp_path), "HOME": str(tmp_path)}, timeout=60)
    assert result.returncode == 1
    assert "Python 3.11 or newer was not found" in result.stdout and "python.org/downloads" in result.stdout


# --- the set-up step ------------------------------------------------------------------------


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    root = tmp_path / "copy"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "VENV", root / ".venv")
    monkeypatch.setattr(bootstrap, "STAMP", root / ".venv" / ".sfti-installed")
    return root


def test_install_runs_once_and_again_only_when_requirements_change(fake_project, monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda command, **kw: calls.append(command) or
                        subprocess.CompletedProcess(command, 0))
    (fake_project / ".venv").mkdir()
    python = Path("the-python")
    bootstrap.ensure_installed(python)
    bootstrap.ensure_installed(python)
    assert len(calls) == 1 and calls[0][:4] == ["the-python", "-m", "pip", "install"]
    assert calls[0][-2:] == ["--editable", str(fake_project)]
    (fake_project / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.4"\n', encoding="utf-8")
    bootstrap.ensure_installed(python)
    assert len(calls) == 2


def test_a_failed_install_is_explained_and_tried_again_next_time(fake_project, monkeypatch, capsys):
    (fake_project / ".venv").mkdir()
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 1))
    with pytest.raises(SystemExit) as stopped:
        bootstrap.ensure_installed(Path("the-python"))
    assert stopped.value.code == 1 and "pypi.org" in capsys.readouterr().out
    assert not bootstrap.STAMP.exists()          # so the next start installs again


def test_environment_is_created_once(fake_project, monkeypatch):
    made = []

    def create(command):
        made.append(command)
        bootstrap.venv_python().parent.mkdir(parents=True)
        bootstrap.venv_python().write_text("", encoding="utf-8")

    monkeypatch.setattr(bootstrap.subprocess, "check_call", create)
    assert bootstrap.ensure_environment() == bootstrap.venv_python() == bootstrap.ensure_environment()
    assert made == [[sys.executable, "-m", "venv", str(fake_project / ".venv")]]


def test_the_page_and_claude_code_get_the_python_that_has_the_project(fake_project, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(bootstrap, "ensure_environment", lambda: Path("venv-python"))
    monkeypatch.setattr(bootstrap, "ensure_installed", lambda python: None)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"/tools/{name}" if name != "sf" else None)
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda command, **kw: started.append((command, kw)) or 0)

    assert bootstrap.main(["--demo", "--port", "9001", "--no-browser"]) == 0
    command, options = started[-1]
    assert command == ["venv-python", "-m", "webapp", "--demo", "--port", "9001", "--no-browser"]
    assert options["env"]["SFTI_PYTHON"] == "venv-python" and options["cwd"] == str(fake_project)
    report = capsys.readouterr().out
    assert "missing  Salesforce CLI (sf)" in report and "npm install --global @salesforce/cli" in report
    assert "ok       Claude Code" in report

    assert bootstrap.main(["--claude"]) == 0
    command, options = started[-1]
    assert command == ["/tools/claude"] and options["env"]["SFTI_PYTHON"] == "venv-python"

    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    assert bootstrap.main(["--claude"]) == 1
    assert "npm install --global @anthropic-ai/claude-code" in capsys.readouterr().out


def test_an_old_python_is_told_what_to_install(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "MINIMUM", (99, 0))
    assert bootstrap.main([]) == 1
    assert "Python 99.0 or newer is needed" in capsys.readouterr().out


# --- starting the page ----------------------------------------------------------------------


def test_the_page_checks_itself(project, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(entry, "App", lambda demo=False: App(root=project.root, demo=demo, demo_state=tmp_path / "state"))
    assert entry.main(["--self-test", "--demo"]) == 0
    assert "the simulated orgs answer" in capsys.readouterr().out


def test_a_second_start_opens_the_copy_that_is_already_running(project, monkeypatch, capsys, tmp_path):
    app = App(root=project.root, demo=True, demo_state=tmp_path / "state")
    server = create_server(app, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opened = []
    monkeypatch.setattr(entry, "open_browser", opened.append)
    monkeypatch.setattr(entry, "App", lambda **kw: pytest.fail("a second copy must not be started"))
    try:
        assert entry.port_is_free(port) is False and entry.running_copy(port)["demo"] is True
        assert entry.main(["--port", str(port)]) == 0
        assert opened == [f"http://127.0.0.1:{port}/"]
        assert "already running" in capsys.readouterr().out
        assert entry.main(["--port", str(port), "--no-browser"]) == 0 and len(opened) == 1
    finally:
        server.shutdown()
        server.server_close()
    assert entry.port_is_free(port) is True


def test_a_port_used_by_another_program_is_skipped_or_reported(monkeypatch, capsys):
    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ok": True, "something": "else"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
    port = other.server_address[1]
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert entry.running_copy(port) is None            # it answers, but it is not this page
        monkeypatch.setattr(entry, "App", lambda **kw: pytest.fail("nothing should start"))
        assert entry.main(["--port", str(port)]) == 1
        assert f"port {port} already in use" in capsys.readouterr().err
    finally:
        other.shutdown()
        other.server_close()


# --- the release zip ------------------------------------------------------------------------


def test_release_holds_what_is_needed_and_nothing_private(tmp_path):
    archive = make_release.build(out_dir=tmp_path)
    assert archive.name == f"salesforce-ticket-investigator-{make_release.project_version()}.zip"
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        infos = {info.filename: info for info in zipped.infolist()}
        bat = zipped.read("salesforce-ticket-investigator/start.bat")
        command = zipped.read("salesforce-ticket-investigator/start.command")
    assert all(name.startswith("salesforce-ticket-investigator/") for name in names)
    relative = {name.split("/", 1)[1] for name in names}
    assert set(make_release.REQUIRED) <= relative and set(START_FILES) <= relative
    for needed in ("samples/planted-bug/org-data/tests.json", "samples/DEMO-101/ticket.txt", "simulator/fake_sf.py",
                   ".claude/agents/code-tracer.md", ".claude/hooks/guard.py", "scripts/call_tool.py", "docs/DECISIONS.md"):
        assert needed in relative, needed
    # Never in a release: what describes someone's org, local state, and development leftovers.
    for name in relative:
        parts = name.split("/")
        assert parts[0] not in ("tickets", "knowledge", "audit", "tests", "dist", ".git", ".github", ".venv"), name
        assert "__pycache__" not in parts and not name.endswith((".pyc", "settings.local.json")), name
        assert not name.startswith("simulator/state/"), name
    # Runnable as unpacked: Windows line endings in .bat, Unix ones and the "may be run" bit on .command.
    assert b"\r\n" in bat and b"\r\r\n" not in bat and b"\r" not in command
    assert (infos["salesforce-ticket-investigator/start.command"].external_attr >> 16) & 0o111
    assert not (infos["salesforce-ticket-investigator/README.md"].external_attr >> 16) & 0o111
    assert archive.stat().st_size < 5_000_000


def test_release_never_picks_up_private_folders_even_inside_included_ones(tmp_path):
    root = tmp_path / "project"
    for name, body in {
        "core/a.py": "x = 1", "core/__pycache__/a.cpython-313.pyc": "junk", "core/tickets/T-1/report.md": "private",
        "webapp/static/index.html": "<html>", "simulator/build.py": "", "simulator/state/orgs/prod/records.json": "{}",
        ".claude/settings.json": "{}", ".claude/settings.local.json": "{}", "docs/.DS_Store": "",
        "tickets/T-1/report.md": "private", "knowledge/00D.json": "{}", "audit/audit.db": "", "README.md": "readme",
        "notes-to-self.txt": "not listed, so not shipped",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    assert [p.as_posix() for p in make_release.release_files(root)] == [
        ".claude/settings.json", "README.md", "core/a.py", "simulator/build.py", "webapp/static/index.html"]


@pytest.mark.parametrize("settings,fragment", [
    ({"mode": "apply"}, "mode = 'apply'"),
    ({"mode": "advise", "allow_apex_tests_on_production": True}, "allow_apex_tests_on_production = True"),
    ({"mode": "advise", "allow_developer_edition_writes": True}, "allow_developer_edition_writes = True"),
    ({"mode": "advise", "jira_base_url": "https://jira.example.test"}, "Jira server"),
])
def test_release_is_refused_when_the_settings_are_not_the_cautious_defaults(tmp_path, settings, fragment):
    (tmp_path / "sfti.config.json").write_text(json.dumps(settings), encoding="utf-8")
    assert any(fragment in problem for problem in make_release.check_settings(tmp_path))
    with pytest.raises(SystemExit) as refused:
        make_release.build(root=tmp_path, out_dir=tmp_path / "dist")
    assert "Not building the release" in str(refused.value) and not (tmp_path / "dist").exists()


def test_the_settings_shipped_today_are_the_cautious_defaults():
    assert make_release.check_settings() == []


def test_quick_start_names_only_things_that_exist():
    text = (ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    for name in START_FILES:
        assert f"`{name}`" in text, name
    assert "The two modes" in text and "## The two modes" in (ROOT / "README.md").read_text(encoding="utf-8")
    page = (ROOT / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    for label in ("I have connected it", "Check again", "Load the sample ticket", "Replay the recorded sample",
                  "Investigate with Claude Code", "Investigate again", "Add information", "Download the result",
                  "New ticket", "Get started", "Org memory", "Root cause", "Ticket comment", "Evidence"):
        assert label in text and label in page, label
    assert len(text.splitlines()) < 130       # one page


def test_walk_through_shows_every_step_with_a_picture_that_exists():
    import re

    text = (ROOT / "docs" / "WALKTHROUGH.md").read_text(encoding="utf-8")
    pictures = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    assert len(pictures) == 16 and len(set(pictures)) == 16
    for picture in pictures:
        assert (ROOT / "docs" / picture).is_file(), picture
    assert [int(p.split("/")[-1][:2]) for p in pictures] == list(range(1, 17))      # in order, none skipped
    video = re.search(r"\]\((demo/[^)]+\.mp4)\)", text)
    assert video and (ROOT / "docs" / video.group(1)).is_file()
    assert 500_000 < (ROOT / "docs" / video.group(1)).stat().st_size < 12_000_000
    # The README's own pictures and links to the two guides exist too.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(((?:docs/|QUICKSTART)[^)#]+)\)", readme):
        assert (ROOT / target).is_file(), target


def test_release_carries_the_guides_but_not_the_video(tmp_path):
    archive = make_release.build(out_dir=tmp_path)
    with zipfile.ZipFile(archive) as zipped:
        names = {name.split("/", 1)[1] for name in zipped.namelist()}
    assert "docs/WALKTHROUGH.md" in names and "docs/images/steps/01-get-started.png" in names
    assert "samples/DEMO-101/live-run/first-run/report.md" in names
    assert not [name for name in names if name.endswith((".mp4", ".webm"))]
