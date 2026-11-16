"""Set up and start the Salesforce Ticket Investigator in one step.

This is what ``start.bat`` (Windows) and ``start.command`` (macOS) run. It needs nothing but
Python itself, and it is safe to run again at any time:

1. creates a private Python environment in ``.venv`` (once);
2. installs the project into it (once, and again when the project's requirements change);
3. says which of the other tools are present (git, Salesforce CLI, Claude Code);
4. starts the local web page, which carries on from there.

Options:
    --demo         start on the simulated demo orgs (no Salesforce org needed)
    --claude       open Claude Code in this folder instead of the web page, with the investigator's tools
    --check        do steps 1 to 3, start the page once to see that it answers, and stop
    --port N       port for the web page (default 8765)
    --no-browser   do not open the browser
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
STAMP = VENV / ".sfti-installed"
MINIMUM = (3, 11)


def say(text: str = "") -> None:
    print(text, flush=True)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def requirements_fingerprint() -> str:
    """Changes when the project's requirements change, so that the install is repeated only then."""
    return hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()


def ensure_environment() -> Path:
    python = venv_python()
    if not python.is_file():
        say("Creating a private Python environment in .venv (once) ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    return python


def ensure_installed(python: Path) -> None:
    wanted = requirements_fingerprint()
    try:
        if STAMP.read_text(encoding="utf-8").strip() == wanted:
            return
    except OSError:
        pass
    say("Installing the investigator (about a minute, once) ...")
    result = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
                             "--editable", str(ROOT)])
    if result.returncode != 0:
        say()
        say("The installation did not finish. The usual cause is no internet connection, or a company")
        say("network that blocks pypi.org. Fix that and start again; nothing is half-installed.")
        raise SystemExit(1)
    STAMP.write_text(wanted + "\n", encoding="utf-8")


def tool_report() -> dict[str, str | None]:
    """Where the other tools are. Missing ones are not fatal: the page explains each with the exact command."""
    found = {name: shutil.which(name) for name in ("git", "sf", "claude")}
    labels = {"git": "git", "sf": "Salesforce CLI (sf)", "claude": "Claude Code"}
    missing = {
        "git": "needed: the page shows how to install it",
        "sf": "needed for your own orgs, not for the demo: npm install --global @salesforce/cli",
        "claude": "needed to investigate from the page: npm install --global @anthropic-ai/claude-code",
    }
    for name, label in labels.items():
        say(f"  {'ok     ' if found[name] else 'missing'}  {label}" + ("" if found[name] else f"   ({missing[name]})"))
    return found


def child_environment(python: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["SFTI_PYTHON"] = str(python)  # the investigator's tools run with the Python that has them installed
    env["PYTHONUTF8"] = "1"
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up and start the Salesforce Ticket Investigator.")
    parser.add_argument("--demo", action="store_true", help="use the simulated demo orgs")
    parser.add_argument("--claude", action="store_true", help="open Claude Code in this folder instead of the page")
    parser.add_argument("--check", action="store_true", help="set up, report and stop")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if sys.version_info < MINIMUM:
        say(f"Python {MINIMUM[0]}.{MINIMUM[1]} or newer is needed; this is {sys.version.split()[0]}.")
        say("Install a newer one from https://www.python.org/downloads/ and start again.")
        return 1

    say("Salesforce Ticket Investigator")
    say(f"Folder: {ROOT}")
    python = ensure_environment()
    ensure_installed(python)
    found = tool_report()
    env = child_environment(python)

    if args.check:
        # Load every part, then start the page once and ask it what a browser would ask.
        steps = [[str(python), "-c", "import core, mcp_server.server, simulator, webapp"],
                 [str(python), "-m", "webapp", "--self-test"] + (["--demo"] if args.demo else [])]
        ok = all(subprocess.run(step, cwd=str(ROOT), env=env).returncode == 0 for step in steps)
        say("Set-up is complete." if ok else "Set-up failed: see the message above.")
        return 0 if ok else 1

    if args.claude:
        if not found["claude"]:
            say()
            say("Claude Code is not installed. Install it, then start this again:")
            say("  npm install --global @anthropic-ai/claude-code")
            return 1
        say()
        say("Opening Claude Code in this folder. Type /get-started if this is your first time.")
        return subprocess.call([found["claude"]], cwd=str(ROOT), env=env)

    command = [str(python), "-m", "webapp"]
    if args.demo:
        command.append("--demo")
    if args.port:
        command += ["--port", str(args.port)]
    if args.no_browser:
        command.append("--no-browser")
    say()
    say("Starting the page. Keep this window open while you use it; close it to stop.")
    try:
        return subprocess.call(command, cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
