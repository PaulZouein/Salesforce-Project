"""``python -m simulator setup`` — build the simulated orgs and print how to use them."""

from __future__ import annotations

import sys

from .build import DEFAULT_STATE, build_state


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("setup", "reset"):
        print("Usage: python -m simulator setup")
        return 2
    launcher = build_state(DEFAULT_STATE)
    print("Simulated orgs created: demo-sandbox (sandbox), demo-prod (production), demo-dev (Developer Edition).")
    print("They contain the planted bug from samples/planted-bug. No real org is involved.")
    print()
    print("To point the investigator at them, set this before starting Claude Code:")
    if launcher.suffix == ".cmd":
        print(f'  PowerShell:  $env:SFTI_SF_EXECUTABLE = "{launcher}"')
        print(f'  cmd.exe:     set SFTI_SF_EXECUTABLE={launcher}')
    else:
        print(f'  export SFTI_SF_EXECUTABLE="{launcher}"')
    print()
    print("Unset the variable to go back to the real Salesforce CLI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
