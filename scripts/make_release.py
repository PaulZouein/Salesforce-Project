"""Build the zip to hand to someone: everything needed to run the investigator, and nothing else.

    python scripts/make_release.py            ->  dist/salesforce-ticket-investigator-<version>.zip

What goes in is an explicit list (``INCLUDE``), never "everything in the folder": ticket folders,
the org memory and the audit database describe customer orgs and must never travel in a release.
The archive is refused if the settings file it would ship is not in its safe default state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDER_NAME = "salesforce-ticket-investigator"

#: Top-level files and folders that make up a release.
INCLUDE = (
    "QUICKSTART.md", "README.md", "CLAUDE.md", "pyproject.toml", "sfti.config.json", ".mcp.json",
    "start.bat", "start-demo.bat", "claude-here.bat", "start.command", "start-demo.command", "claude-here.command",
    ".claude", "core", "mcp_server", "simulator", "webapp", "samples", "scripts", "docs",
)
#: Never shipped, wherever they are.
EXCLUDED_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "state", "tickets",
                                  "knowledge", "audit", "usage", "dist", "build", "node_modules", ".sf", ".sfdx"})
EXCLUDED_FILES = frozenset({".DS_Store", "settings.local.json", "Thumbs.db"})
#: Not shipped: leftovers, and the demo video (it is linked from the README; the zip stays small).
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".log.tmp", ".mp4", ".webm")
#: Files that must be present, or the release is useless.
REQUIRED = ("QUICKSTART.md", "pyproject.toml", "CLAUDE.md", ".mcp.json", "sfti.config.json", "start.bat",
            "start.command", "scripts/bootstrap.py", "webapp/static/index.html", "mcp_server/server.py",
            ".claude/settings.json", ".claude/skills/investigate-ticket/SKILL.md")
#: What the settings file must say in a release: the cautious defaults.
SAFE_SETTINGS = {"mode": "advise", "allow_developer_edition_writes": False, "allow_apex_tests_on_production": False}


def project_version(root: Path = ROOT) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "0"


def release_files(root: Path = ROOT) -> list[Path]:
    """Every file of a release, relative to the project root, sorted."""
    found: list[Path] = []
    for name in INCLUDE:
        entry = root / name
        if entry.is_file():
            found.append(Path(name))
        elif entry.is_dir():
            for path in entry.rglob("*"):
                relative = path.relative_to(root)
                if not path.is_file() or path.is_symlink():
                    continue
                if any(part in EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
                    continue
                if path.name in EXCLUDED_FILES or path.name.endswith(EXCLUDED_SUFFIXES):
                    continue
                found.append(relative)
    return sorted(found, key=lambda p: p.as_posix())


def check_settings(root: Path = ROOT) -> list[str]:
    """Reasons the settings file must not be shipped as it is. Empty when it is in its default state."""
    try:
        settings = json.loads((root / "sfti.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"sfti.config.json cannot be read: {exc}"]
    problems = [f"sfti.config.json has {key} = {settings.get(key)!r}; a release ships {value!r}"
                for key, value in SAFE_SETTINGS.items() if settings.get(key, value) != value]
    if settings.get("jira_base_url"):
        problems.append("sfti.config.json names a Jira server; a release ships none")
    return problems


def build(root: Path = ROOT, out_dir: Path | None = None) -> Path:
    problems = check_settings(root)
    files = release_files(root)
    names = {path.as_posix() for path in files}
    problems += [f"missing from the project: {name}" for name in REQUIRED if name not in names]
    if problems:
        raise SystemExit("Not building the release:\n  " + "\n  ".join(problems))
    out_dir = Path(out_dir) if out_dir else root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{FOLDER_NAME}-{project_version(root)}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            data = (root / relative).read_bytes()
            if relative.suffix == ".bat":  # Windows command files need Windows line endings
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            elif relative.suffix in (".command", ".sh"):
                data = data.replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(f"{FOLDER_NAME}/{relative.as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # Keep "may be run" on the macOS start scripts; plain files otherwise.
            info.external_attr = (0o755 if relative.suffix in (".command", ".sh") else 0o644) << 16
            archive.writestr(info, data)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the release zip.")
    parser.add_argument("--out", type=Path, default=None, help="folder for the zip (default: dist/)")
    args = parser.parse_args(argv)
    target = build(out_dir=args.out)
    with zipfile.ZipFile(target) as archive:
        count = len(archive.namelist())
    print(f"{target}  ({count} files, {target.stat().st_size / 1_000_000:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
