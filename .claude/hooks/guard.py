#!/usr/bin/env python3
"""PreToolUse hook: a second line of defense behind the permission rules.

Blocks three things, whatever permission mode Claude Code is running in:

1. Running the Salesforce CLI directly from Bash. All org access must go through the
   sf-investigator MCP server, where production writes are blocked in code.
2. Calling the core write functions from Bash, which would skip the approval prompt that
   the MCP write tools always show.
3. Changing the files that define the safety settings (sfti.config.json, .mcp.json,
   .claude/settings.json and this hook). Only the user edits those.

Exit code 2 blocks the tool call and shows the message to the assistant. Any failure of
the hook itself exits 0, so a broken hook can never lock a session.

This is a heuristic guard, not a sandbox: it stops mistakes and shortcuts. The hard
guarantee (no writes to production) lives in core/org_guard.py and does not depend on it.
"""

from __future__ import annotations

import json
import os
import re
import sys

PROTECTED = ("sfti.config.json", ".mcp.json", ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/")
WRAPPERS = {"sudo", "env", "time", "nohup", "xargs", "command", "exec", "builtin", "then", "do", "else"}
READ_ONLY_COMMANDS = {"cat", "head", "tail", "less", "more", "grep", "rg", "wc", "ls", "stat", "file", "diff", "git"}
CLI_NAMES = {"sf", "sfdx"}
WRITE_FUNCTIONS = re.compile(
    r"\b(deploy_component|update_records|execute_anonymous_apex|rollback_change|set_trace_flag|"
    r"authorize_write|authorize_test_run|_issue_permit|rest_request)\b"
)


class Blocked(Exception):
    pass


def first_words(segment: str) -> list[str]:
    words = segment.strip().split()
    while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in WRAPPERS or words[0].startswith("-")):
        words = words[1:]
    return words


def base_name(word: str) -> str:
    name = re.split(r"[\\/]", word.strip("\"'"))[-1].lower()
    return re.sub(r"\.(cmd|exe|bat|ps1)$", "", name)


def check_bash(command: str) -> None:
    if WRITE_FUNCTIONS.search(command) and re.search(r"\bpython[\d.]*\b|\bpy\b", command) and "pytest" not in command:
        raise Blocked(
            "Blocked: org-changing functions must be called through the sf-investigator MCP write tools, "
            "which ask the user for approval every time."
        )
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
