"""Call a read-only investigator tool from the command line, for debugging.

    python scripts/call_tool.py --list
    python scripts/call_tool.py org_info org=demo-prod
    python scripts/call_tool.py soql_query org=demo-prod query="SELECT Id, Priority FROM Case" limit=5
    python scripts/call_tool.py search_metadata ticket_key=DEMO-101 pattern=return --json '{"max_results": 5}'

Arguments are ``name=value`` pairs; values that parse as JSON (numbers, true/false, lists) are
used as such, anything else is text. ``--json`` adds a JSON object of arguments.

Write tools are refused here, always: they change an org and must go through Claude Code, where
every call asks for approval. There is no flag to allow them.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_arguments(pairs: list[str]) -> dict:
    arguments: dict = {}
    index = 0
    while index < len(pairs):
        item = pairs[index]
        if item == "--json":
            index += 1
            if index >= len(pairs):
                raise ValueError("--json needs a JSON object")
            extra = json.loads(pairs[index])
            if not isinstance(extra, dict):
                raise ValueError("--json needs a JSON object")
            arguments.update(extra)
        elif "=" in item:
            name, value = item.split("=", 1)
            try:
                arguments[name] = json.loads(value)
            except ValueError:
                arguments[name] = value
        else:
            raise ValueError(f"Expected name=value, got {item!r}")
        index += 1
    return arguments


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from mcp_server import server

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "--list":
        for name in server.READ_ONLY_TOOLS:
            doc = (server.TOOLS[name].__doc__ or "").strip().splitlines()[0]
            print(f"{name:24} {doc}")
        print(f"\nNot callable from here (write tools): {', '.join(server.WRITE_TOOLS)}")
        return 0

    name = argv[0]
    if name in server.WRITE_TOOLS:
        print(f"Refused: {name} is a write tool. Write tools run only through Claude Code, "
              "where each call asks for your approval.", file=sys.stderr)
        return 3
    if name not in server.READ_ONLY_TOOLS:
        print(f"Unknown tool {name!r}. Use --list.", file=sys.stderr)
        return 2
    try:
        arguments = parse_arguments(argv[1:])
        inspect.signature(server.TOOLS[name]).bind(**arguments)
        text = server.TOOLS[name](**arguments)
    except (TypeError, ValueError) as exc:
        print(f"Bad arguments for {name}: {exc}", file=sys.stderr)
        return 2
    print(text)
    return 0 if json.loads(text).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
