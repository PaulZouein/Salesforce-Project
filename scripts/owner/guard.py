#!/usr/bin/env python3
"""PreToolUse hook: a second line of defense behind the permission rules.

PROPOSED REPLACEMENT for .claude/hooks/guard.py, prepared for the owner (the assistant may not
edit the installed hook). See scripts/owner/README.md for what changes and how to apply it.

Blocks four things, whatever permission mode Claude Code is running in:

1. Running the Salesforce CLI directly from Bash. All org access must go through the
   sf-investigator MCP server, where production writes are blocked in code.
2. Calling the core write functions from Python in Bash, which would skip the approval prompt
   that the MCP write tools always show. Running the test suite (``pytest``, ``python -m pytest``)
   is not that, wherever the function names appear in its arguments.
3. Running anything under ``tickets/``: those folders hold evidence from the org and the ticket
   (debug logs, query results, attachments), which is read, never executed.
4. Changing the files that define the safety settings (sfti.config.json, .mcp.json,
   .claude/settings.json and this hook). Only the user edits those.

Exit code 2 blocks the tool call and shows the message to the assistant. Any failure of
the hook itself exits 0, so a broken hook can never lock a session.

This is a heuristic guard, not a sandbox: it stops mistakes and shortcuts, and a disguised
command can get past it. The hard guarantee (no writes to production through the tools) lives
in core/org_guard.py and does not depend on it.
"""

from __future__ import annotations

import json
import os
import re
import sys

PROTECTED = ("sfti.config.json", ".mcp.json", ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/")
WRAPPERS = {"sudo", "env", "time", "nohup", "xargs", "command", "exec", "builtin", "then", "do", "else"}
RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "conda"}  # "<runner> run <command>"
READ_ONLY_COMMANDS = {"cat", "head", "tail", "less", "more", "grep", "rg", "wc", "ls", "stat", "file", "diff", "git"}
CLI_NAMES = {"sf", "sfdx"}
PYTHON_NAMES = re.compile(r"^(python[\d.]*|pypy[\d.]*|py|ipython)$")
TEST_RUNNERS = {"pytest", "py.test"}
INTERPRETERS = TEST_RUNNERS | {
    "node", "deno", "bun", "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd",
    "ruby", "perl", "php", "java", "lua", "rscript", "osascript", "source", ".",
}
WRITE_FUNCTIONS = re.compile(
    r"\b(deploy_component|update_records|execute_anonymous_apex|rollback_change|set_trace_flag|"
    r"authorize_write|authorize_test_run|_issue_permit|rest_request)\b"
)


class Blocked(Exception):
    pass


def first_words(segment: str) -> list[str]:
    words = segment.strip().split()
    while words:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in WRAPPERS or words[0].startswith("-"):
            words = words[1:]
        elif base_name(words[0]) in RUNNERS and len(words) > 1 and words[1] == "run":
            words = words[2:]
        else:
            break
    return words


def base_name(word: str) -> str:
    name = re.split(r"[\\/]", word.strip("\"'"))[-1].lower()
    return re.sub(r"\.(cmd|exe|bat|ps1)$", "", name)


def runs_python(words: list[str]) -> bool:
    return bool(words) and PYTHON_NAMES.match(base_name(words[0])) is not None


def runs_tests(words: list[str]) -> bool:
    """``pytest …``, or a Python interpreter told ``-m pytest`` straight away."""
    if not words:
        return False
    if base_name(words[0]) in TEST_RUNNERS:
        return True
    return runs_python(words) and words[1:3] == ["-m", "pytest"]


def check_bash(command: str) -> None:
    segments = [first_words(s) for s in re.split(r"[;&|\n`]|\$\(|\(|\)", command)]
    if WRITE_FUNCTIONS.search(command) and any(runs_python(w) and not runs_tests(w) for w in segments):
        raise Blocked(
            "Blocked: org-changing functions must be called through the sf-investigator MCP write tools, "
            "which ask the user for approval every time."
        )
    inside_tickets = False
    for segment in re.split(r"[;&|\n`]|\$\(|\(|\)", command):
        words = first_words(segment)
        if not words:
            continue
        head = base_name(words[0])
        if head in CLI_NAMES:
            raise Blocked(
                "Blocked: the Salesforce CLI must not be run directly. Use the sf-investigator MCP tools "
                "(soql_query, retrieve_components, deploy_component, ...); they enforce the production "
                "write block and the audit log."
            )
        if head in ("npx", "pnpx", "bunx", "yarn", "pnpm") and any(
            base_name(w) in CLI_NAMES or "@salesforce/cli" in w for w in words[1:4]
        ):
            raise Blocked("Blocked: the Salesforce CLI must not be run directly. Use the sf-investigator MCP tools.")
        normalized = segment.replace("\\", "/")
        if head in ("cd", "pushd") and re.search(r"(^|[\s/\"'])tickets(/|\s|$|[\"'])", normalized):
            inside_tickets = True
        executes = head in INTERPRETERS or runs_python(words)
        if executes and (inside_tickets or re.search(r"(^|[\s\"'=/])tickets/", normalized)):
            raise Blocked(
                "Blocked: files under tickets/ hold evidence from the org and the ticket (debug logs, query "
                "results, attachments). They are read with the Read tool or read_file, never run."
            )
        mentions = [p for p in PROTECTED if p in normalized]
        if mentions and (head not in READ_ONLY_COMMANDS or re.search(r">|\btee\b", segment)):
            raise Blocked(
                f"Blocked: {mentions[0]} holds safety settings and is only changed by the user. "
                "Explain the change you would like and let the user make it."
            )


def check_file(path: str) -> None:
    if not path:
        return
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    absolute = os.path.abspath(path if os.path.isabs(path) else os.path.join(project, path))
    try:
        relative = os.path.relpath(absolute, project).replace("\\", "/")
    except ValueError:
        return
    if relative.startswith(".."):
        return
    for protected in PROTECTED:
        if relative == protected or (protected.endswith("/") and relative.startswith(protected)):
            raise Blocked(
                f"Blocked: {relative} holds safety settings and is only changed by the user. "
                "Explain the change you would like and let the user make it."
            )
    if relative.startswith("audit/"):
        raise Blocked("Blocked: the audit database is written only by the tool itself.")


def evaluate(event: dict) -> None:
    """Raise ``Blocked`` when the tool call in ``event`` must not run."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    if tool == "Bash":
        check_bash(str(tool_input.get("command", "")))
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        check_file(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""))


def main() -> int:
    try:
        evaluate(json.load(sys.stdin))
    except Blocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - a broken hook must never lock the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
