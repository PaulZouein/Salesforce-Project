"""The PreToolUse hook: defense in depth behind the permission rules and the core guards.

These tests describe the hook script as it is. They do not change it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "guard.py"

spec = importlib.util.spec_from_file_location("sfti_guard_hook", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def run_hook(event, *, raw: str | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=raw if raw is not None else json.dumps(event),
        capture_output=True, text=True, cwd=cwd, env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)}, timeout=30,
    )


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


BLOCKED_COMMANDS = [
    "sf org list",
    "sf data query --query 'SELECT Id FROM Case' --target-org prod",
    "sfdx force:org:list",
    "cd tickets && sf project deploy start --target-org prod",
    "cd x; sf org list",
    "true || sf org list",
    "echo hi | sf apex run",
    "FOO=1 sf org list",
    "env FOO=1 sf org list",
    "sudo sf org list",
    "/usr/local/bin/sf org list",
    "sf.cmd org list",
    "C:\\Users\\me\\AppData\\Roaming\\npm\\sf.cmd org list",
    "echo $(sf org display --target-org prod)",
    "echo `sf org list`",
    "(sf org list)",
    "npx sf org list",
    "npx sfdx org list",
    "npx @salesforce/cli org list",
    "npx --yes @salesforce/cli data query",
    "python -c \"from core.app import Investigator; Investigator().deploy_component('prod', 'T', ['ApexClass:A'], reason='x')\"",
    "python3 - <<'EOF'\nfrom core.app import Investigator\nInvestigator().update_records('s', 'T', 'Case', [], reason='x')\nEOF",
    "echo 'import core.logs as l; l.set_trace_flag()' | python",
    "python scripts/run.py && python -c 'import core.org_guard as g; g.OrgGuard(c).authorize_write(\"prod\", \"x\")'",
    "python -c 'from core.sf_cli import _issue_permit'",
    "py -3 -c \"import core.changes; core.changes.ChangeManager.rollback_change\"",
    "echo '{}' > sfti.config.json",
    "sed -i 's/advise/apply/' sfti.config.json",
    "python -c \"open('sfti.config.json','w').write('{}')\"",
    "cp /tmp/x .claude/settings.json",
    "rm .claude/hooks/guard.py",
    "mv .mcp.json .mcp.json.bak",
    "cat x | tee .claude/settings.local.json",
    "cat evil.json > .mcp.json",
    "echo x >> .claude/hooks/guard.py",
]

ALLOWED_COMMANDS = [
    "grep sf README.md",
    "grep -rn 'sf org' docs/",
    "python -m pytest",
    "python -m pytest tests/test_changes.py -k deploy_component",
    "python3 -m pytest -q tests/test_guard_hook.py",
    "pytest -k 'update_records or rollback_change'",
    "cat sfti.config.json",
    "git add .claude/settings.json",
    "git diff .claude/hooks/guard.py",
    "git status",
    "ls .claude/hooks/",
    "grep -rn authorize_write core/ tests/",
    "python -m core.prereqs",
    "python -m simulator setup",
    "python scripts/generate_settings.py --check",
    "python scripts/call_tool.py org_info --org demo-prod",
    "echo sf",
    "ls sf-notes/",
    "cat docs/sfdx-notes.md",
    "git commit -m 'Describe set_trace_flag in the README'",
    "wc -l core/sf_cli.py",
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_blocked_bash_commands(command):
    with pytest.raises(guard.Blocked):
        guard.evaluate(bash(command))


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_allowed_bash_commands(command):
    guard.evaluate(bash(command))


@pytest.mark.xfail(strict=True, reason="Known false positive, recorded in docs/DECISIONS.md: the hook treats "
                                       "'.py' in a file name as a Python invocation. It errs on the side of blocking.")
def test_known_false_positive_reading_a_python_file_that_names_a_write_function():
    guard.evaluate(bash("grep -n deploy_component core/changes.py"))


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
@pytest.mark.parametrize("path", [
    "sfti.config.json", ".mcp.json", ".claude/settings.json", ".claude/settings.local.json",
    ".claude/hooks/guard.py", ".claude/hooks/new_hook.sh", "./sfti.config.json", "docs/../sfti.config.json",
    "audit/audit.db",
])
def test_protected_files_cannot_be_edited(tool, path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ROOT))
    for candidate in (path, str(ROOT / path)):
        with pytest.raises(guard.Blocked):
            guard.evaluate({"tool_name": tool, "tool_input": {"file_path": candidate}})


@pytest.mark.parametrize("path", [
    "README.md", "core/org_guard.py", "tickets/T-1/report.md", ".claude/skills/intake-ticket/SKILL.md",
    "tickets/T-1/metadata/force-app/main/default/classes/A.cls", "docs/sfti.config.json.md",
    "/tmp/elsewhere/sfti.config.json",
])
def test_other_files_can_be_edited(path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ROOT))
    guard.evaluate({"tool_name": "Edit", "tool_input": {"file_path": path}})
    guard.evaluate({"tool_name": "Write", "tool_input": {"file_path": path}})


def test_other_tools_are_ignored():
    guard.evaluate({"tool_name": "Read", "tool_input": {"file_path": "sfti.config.json"}})
    guard.evaluate({"tool_name": "mcp__sf-investigator__deploy_component", "tool_input": {"org": "prod"}})
    guard.evaluate({})


# --- the script as Claude Code runs it --------------------------------------------------


def test_exit_code_2_and_message_on_block():
    result = run_hook(bash("cd x && sf org list"))
    assert result.returncode == 2
    assert "must not be run directly" in result.stderr and "sf-investigator" in result.stderr
    result = run_hook({"tool_name": "Write", "tool_input": {"file_path": "sfti.config.json", "content": "{}"}})
    assert result.returncode == 2 and "only changed by the user" in result.stderr


def test_exit_code_0_when_allowed():
    result = run_hook(bash("python -m pytest"))
    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2, 3]", '"text"', "null", '{"tool_name": "Bash"}',
                                 '{"tool_name": "Bash", "tool_input": "oops"}',
                                 '{"tool_name": "Edit", "tool_input": {"file_path": 7}}',
                                 '{"tool_name": "Bash", "tool_input": {"command": null}}'])
def test_malformed_input_never_blocks(raw):
    """A broken hook must never lock the session (this is what happened in the first build)."""
    result = run_hook(None, raw=raw)
    assert result.returncode == 0, result.stderr


def test_internal_error_never_blocks(tmp_path):
    broken = tmp_path / "broken_copy.py"
    broken.write_text(HOOK.read_text().replace("def evaluate(event: dict) -> None:",
                                               "def evaluate(event: dict) -> None:\n    raise RuntimeError('bug')"))
    result = subprocess.run([sys.executable, str(broken)], input=json.dumps(bash("sf org list")),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0


# --- the hook command in the generated settings -----------------------------------------


def hook_command() -> str:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import generate_settings
    finally:
        sys.path.pop(0)
    return generate_settings.HOOK_COMMAND


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")


@needs_sh
def test_hook_command_is_a_noop_when_the_script_is_missing(tmp_path):
    """Without this, a checkout that has the settings but not the script blocks every tool call."""
    result = subprocess.run(["sh", "-c", hook_command()], input=json.dumps(bash("sf org list")),
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), "SFTI_PYTHON": sys.executable})
    assert result.returncode == 0, result.stderr


@needs_sh
def test_hook_command_blocks_when_the_script_is_present():
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), "SFTI_PYTHON": sys.executable}
    blocked = subprocess.run(["sh", "-c", hook_command()], input=json.dumps(bash("sf org list")),
                             capture_output=True, text=True, timeout=30, env=env)
    assert blocked.returncode == 2
    allowed = subprocess.run(["sh", "-c", hook_command()], input=json.dumps(bash("git status")),
                             capture_output=True, text=True, timeout=30, env=env)
    assert allowed.returncode == 0


@needs_sh
def test_hook_command_does_not_block_when_python_is_missing(tmp_path):
    """Exit code 127 (interpreter not found) is not 2, so it cannot block either."""
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), "SFTI_PYTHON": str(tmp_path / "no-python")}
    result = subprocess.run(["sh", "-c", hook_command()], input=json.dumps(bash("git status")),
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode != 2
