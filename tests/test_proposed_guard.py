"""The hardened hook prepared for the owner in scripts/owner/guard.py (not installed by the assistant).

It must do everything the installed hook does (the same lists of commands are run through it), and
also: no test-suite loophole for calling write functions, no running of evidence under tickets/,
and no false positive for reading a Python file that names a write function.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from .test_guard_hook import ALLOWED_COMMANDS, BLOCKED_COMMANDS, bash

ROOT = Path(__file__).resolve().parent.parent
PROPOSED = ROOT / "scripts" / "owner" / "guard.py"
INSTALLED = ROOT / ".claude" / "hooks" / "guard.py"

spec = importlib.util.spec_from_file_location("sfti_proposed_guard", PROPOSED)
proposed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proposed)

NEWLY_BLOCKED = [
    # A debug log saved as evidence can carry code; pytest's doctest collection would run it, unprompted
    # while "python -m pytest" was allowed without asking (safety review, finding 2).
    "python -m pytest --doctest-glob=*.log tickets/T-1/logs",
    "pytest --doctest-modules tickets/T-1/attachments",
    "python3 tickets/T-1/attachments/fix.py",
    "bash tickets/T-1/apex/run.sh",
    "node tickets/T-1/attachments/x.js",
    "cd tickets/T-1 && python x.py",
    "source tickets/T-1/attachments/env.sh",
    "uv run python -c 'from core.changes import ChangeManager as C; C.deploy_component'",
    # The word pytest elsewhere no longer excuses a Python call that names a write function.
    "python -m pytest -q && python -c 'from core.app import Investigator; Investigator().update_records()'",
    "python -c 'import pytest; from core.logs import set_trace_flag'",
]

NEWLY_ALLOWED = [
    "grep -n deploy_component core/changes.py",  # the installed hook's known false positive
    "grep -rn rollback_change core/*.py tests/*.py",
    "cat tickets/T-1/logs/07LWs00000L0001MAA.log",
    "ls tickets/T-1/queries",
    "git -C tickets/T-1/metadata log --oneline",
    "python -m pytest tests/test_changes.py -k 'rollback_change or update_records'",
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS + NEWLY_BLOCKED)
def test_the_proposed_hook_blocks(command):
    with pytest.raises(proposed.Blocked):
        proposed.evaluate(bash(command))


@pytest.mark.parametrize("command", ALLOWED_COMMANDS + NEWLY_ALLOWED)
def test_the_proposed_hook_allows(command):
    proposed.evaluate(bash(command))


@pytest.mark.parametrize("path", [".claude/settings.json", ".claude/hooks/guard.py", "sfti.config.json", ".mcp.json",
                                  "audit/audit.db"])
def test_the_proposed_hook_protects_the_same_files(path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ROOT))
    with pytest.raises(proposed.Blocked):
        proposed.evaluate({"tool_name": "Write", "tool_input": {"file_path": path}})
    proposed.evaluate({"tool_name": "Write", "tool_input": {"file_path": "tickets/T-1/report.md"}})


def test_the_proposed_hook_keeps_the_installed_lists():
    """Same protected files, CLI names and write functions as the installed hook (also once it is installed)."""
    from .test_guard_hook import guard as installed

    assert proposed.PROTECTED == installed.PROTECTED and proposed.CLI_NAMES == installed.CLI_NAMES
    assert proposed.WRITE_FUNCTIONS.pattern == installed.WRITE_FUNCTIONS.pattern
    assert INSTALLED.is_file() and PROPOSED.is_file()
