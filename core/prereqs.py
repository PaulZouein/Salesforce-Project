"""Prerequisites checker.

Run with ``python -m core.prereqs`` (add ``--json`` for machine-readable output).
Exit code 0 when everything required is present, 1 otherwise.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from typing import Any

from .config import CONFIG_FILENAME, load_settings, project_root
from .errors import SfInvestigatorError
from .org_guard import OrgGuard
from .sf_cli import SfCli

MIN_PYTHON = (3, 11)


def _check(name: str, ok: bool, detail: str, *, required: bool = True, hint: str | None = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail, **({"hint": hint} if hint and not ok else {})}


def run_checks(cli: SfCli | None = None) -> list[dict[str, Any]]:
    cli = cli or SfCli()
    checks: list[dict[str, Any]] = []

    version = sys.version_info
    checks.append(_check(
        "Python",
        version >= MIN_PYTHON,
        f"{version.major}.{version.minor}.{version.micro}",
        hint=f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.",
    ))

    git = shutil.which("git")
    git_detail = "not found"
    if git:
        try:
            git_detail = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            git, git_detail = None, f"could not run git: {exc}"
    checks.append(_check("git", bool(git), git_detail, hint="Install git from https://git-scm.com/downloads."))

    sf_ok = False
    try:
        sf_detail = cli.version()
        sf_ok = True
        sf_hint = None
    except SfInvestigatorError as exc:
        sf_detail, sf_hint = exc.message, exc.hint
    checks.append(_check("Salesforce CLI (sf)", sf_ok, sf_detail,
                         hint=sf_hint or "Install with: npm install --global @salesforce/cli"))

    has_mcp = importlib.util.find_spec("mcp") is not None
    checks.append(_check("mcp Python package", has_mcp, "installed" if has_mcp else "not installed",
                         hint="Install with: pip install -e ."))

    if sf_ok:
        try:
            orgs = OrgGuard(cli).list_orgs()
            names = ", ".join(str(o.get("alias") or o.get("username")) for o in orgs[:8])
            checks.append(_check(
                "Authenticated org", bool(orgs),
                f"{len(orgs)} found" + (f": {names}" if names else ""),
                hint="Log in with: sf org login web --alias <alias> "
                     "(add --instance-url https://test.salesforce.com for a sandbox).",
            ))
        except SfInvestigatorError as exc:
            checks.append(_check("Authenticated org", False, exc.message, hint=exc.hint))
    else:
        checks.append(_check("Authenticated org", False, "skipped: the Salesforce CLI is not available"))

    settings = load_settings()
    config_exists = (project_root() / CONFIG_FILENAME).exists()
    detail = f"mode={settings.mode}" + ("" if config_exists else " (no config file; safe defaults)")
    if settings.warnings:
        detail += "; " + " ".join(settings.warnings)
    checks.append(_check(f"Settings ({CONFIG_FILENAME})", not settings.warnings, detail, required=False,
                         hint="Fix the listed problems; until then the safe defaults apply."))
    return checks


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    checks = run_checks()
    failed = [c for c in checks if c["required"] and not c["ok"]]
    if "--json" in argv:
        print(json.dumps({"ok": not failed, "checks": checks}, indent=2))
        return 1 if failed else 0
    width = max(len(c["name"]) for c in checks)
    for c in checks:
        mark = "OK  " if c["ok"] else ("FAIL" if c["required"] else "WARN")
        print(f"[{mark}] {c['name'].ljust(width)}  {c['detail']}")
        if c.get("hint"):
            print(f"       {' ' * width}  -> {c['hint']}")
    print()
    print("All prerequisites are met." if not failed else f"{len(failed)} required prerequisite(s) missing.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
