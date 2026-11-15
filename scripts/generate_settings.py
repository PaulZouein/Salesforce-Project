"""Generate ``.claude/settings.json`` from the MCP server's own tool lists.

The permission lists can never drift from the server: read-only tools go to ``allow``,
write tools go to ``ask``, and nothing else is added.

Usage::

    python scripts/generate_settings.py            # full settings, guard enabled
    python scripts/generate_settings.py --no-guard # without the hook and edit protection
    python scripts/generate_settings.py --check    # exit 1 if the file is out of date

``--no-guard`` exists for working on the tool itself: with the guard on, the assistant
cannot edit the settings, the hook or ``sfti.config.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PREFIX = "mcp__sf-investigator__"
TARGET = ROOT / ".claude" / "settings.json"

# Does nothing when the script is missing, so a partial checkout cannot block every tool call.
HOOK_COMMAND = (
    '[ -f "$CLAUDE_PROJECT_DIR/.claude/hooks/guard.py" ] || exit 0; '
    '${SFTI_PYTHON:-python} "$CLAUDE_PROJECT_DIR/.claude/hooks/guard.py"'
)

SAFE_BASH = [
    "Bash(python -m core.prereqs:*)",
    "Bash(python3 -m core.prereqs:*)",
    "Bash(python -m pytest:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(python -m simulator:*)",
    "Bash(python3 -m simulator:*)",
]

DENY_CLI = [
    "Bash(sf:*)",
    "Bash(sfdx:*)",
    "Bash(npx sf:*)",
    "Bash(npx sfdx:*)",
    "Bash(npx @salesforce/cli:*)",
]

DENY_PROTECTED_FILES = [
    "Edit(/sfti.config.json)",
    "Write(/sfti.config.json)",
    "Edit(/.claude/settings.json)",
    "Write(/.claude/settings.json)",
    "Edit(/.claude/hooks/**)",
    "Write(/.claude/hooks/**)",
    "Edit(/.mcp.json)",
    "Write(/.mcp.json)",
    "Read(/audit/**)",
]


def build(guard: bool = True) -> dict:
    from mcp_server import server

    settings: dict = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "enabledMcpjsonServers": ["sf-investigator"],
        "permissions": {
            "allow": [PREFIX + name for name in server.READ_ONLY_TOOLS] + SAFE_BASH,
            "ask": [PREFIX + name for name in server.WRITE_TOOLS],
            "deny": DENY_CLI + (DENY_PROTECTED_FILES if guard else []),
        },
    }
    if guard:
        hook = {"type": "command", "command": HOOK_COMMAND}
        settings["hooks"] = {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [hook]},
                {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [hook]},
            ]
        }
    return settings


def render(guard: bool = True) -> str:
    return json.dumps(build(guard), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    guard = "--no-guard" not in argv
    text = render(guard)
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET.relative_to(ROOT)} is out of date. Run: python scripts/generate_settings.py")
            return 1
        print("settings are up to date")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} (guard {'on' if guard else 'off'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
