"""Ticket workspace layout and path confinement.

::

    tickets/<KEY>/
        ticket.md        structured intake
        attachments/     screenshots and files from the user
        metadata/        SFDX project and its own git repository
        queries/         query outputs (always redacted)
        logs/            debug logs
        apex/            anonymous Apex scripts and their logs
        report.md, comment.md

Every path handed in by a caller is resolved and checked to stay inside the ticket folder.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import InputValidationError, WorkspaceError
from .gitrepo import GitRepo

TICKETS_DIRNAME = "tickets"
SUBDIRS = ("attachments", "metadata", "queries", "logs", "apex")
PACKAGE_DIR = "force-app"

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-]+")

METADATA_GITIGNORE = ".sf/\n.sfdx/\n*.log\n"


def validate_ticket_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key) or set(key) <= {"-", "_"}:
        raise InputValidationError(
            f"Invalid ticket key: {key!r}.",
            hint="Use the ticket key, for example ABC-123. Letters, digits, - and _ only.",
        )
    return key


def slugify(text: str, fallback: str = "item", max_length: int = 60) -> str:
    slug = _SLUG_RE.sub("-", text.strip()).strip("-")[:max_length].strip("-")
    return slug or fallback


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.tickets_root = self.root / TICKETS_DIRNAME

    # -- folders -------------------------------------------------------------------------

    def ticket_dir(self, key: str, *, create: bool = False) -> Path:
        validate_ticket_key(key)
        path = self.tickets_root / key
        if create:
            path.mkdir(parents=True, exist_ok=True)
            for sub in SUBDIRS:
                (path / sub).mkdir(exist_ok=True)
        elif not path.is_dir():
            raise WorkspaceError(
                f"No workspace for ticket {key!r}.",
                hint="Run the intake step first; it creates tickets/<KEY>/.",
            )
        return path

    def subdir(self, key: str, name: str) -> Path:
        if name not in SUBDIRS:
            raise WorkspaceError(f"Unknown workspace folder {name!r}.")
        path = self.ticket_dir(key, create=True) / name
        path.mkdir(exist_ok=True)
        return path

    def list_tickets(self) -> list[str]:
        if not self.tickets_root.is_dir():
            return []
        return sorted(p.name for p in self.tickets_root.iterdir() if p.is_dir() and _KEY_RE.match(p.name))

    # -- confinement ---------------------------------------------------------------------

    def resolve(self, key: str, relative_path: str, *, must_exist: bool = True) -> Path:
        """Resolve ``relative_path`` inside the ticket folder, refusing anything outside it."""
        base = self.ticket_dir(key).resolve()
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise InputValidationError("A path inside the ticket folder is required.")
        if "\x00" in relative_path:
            raise WorkspaceError("Invalid path.")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            # Accept an absolute path only when it already points inside the ticket folder.
            resolved = candidate.resolve()
        else:
            resolved = (base / candidate).resolve()
        if resolved != base and base not in resolved.parents:
            raise WorkspaceError(
                f"Path {relative_path!r} is outside tickets/{key}/.",
                hint="Paths are relative to the ticket folder, for example "
                "metadata/force-app/main/default/classes/MyClass.cls.",
            )
        if ".git" in resolved.relative_to(base).parts:
            raise WorkspaceError("The .git folder is not readable through this tool.")
        if must_exist and not resolved.exists():
            raise WorkspaceError(f"{relative_path!r} does not exist in tickets/{key}/.")
        return resolved

    def relative(self, key: str, path: Path) -> str:
        base = self.ticket_dir(key).resolve()
        try:
            return Path(path).resolve().relative_to(base).as_posix()
        except ValueError:
            return str(path)

    # -- metadata project ----------------------------------------------------------------

    def metadata_dir(self, key: str) -> Path:
        return self.subdir(key, "metadata")

    def metadata_repo(self, key: str, *, api_version: str | None = None) -> GitRepo:
        """Return the ticket's metadata repository, creating the SFDX project if needed."""
        path = self.metadata_dir(key)
        project_file = path / "sfdx-project.json"
        if not project_file.exists():
            project: dict = {
                "packageDirectories": [{"path": PACKAGE_DIR, "default": True}],
                "namespace": "",
                "sfdcLoginUrl": "https://login.salesforce.com",
            }
            if api_version and re.match(r"^\d{2,3}\.0$", api_version):
                project["sourceApiVersion"] = api_version
            project_file.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
        (path / PACKAGE_DIR / "main" / "default").mkdir(parents=True, exist_ok=True)
        ignore = path / ".gitignore"
        if not ignore.exists():
            ignore.write_text(METADATA_GITIGNORE, encoding="utf-8")
        repo = GitRepo(path)
        if not repo.is_repo():
            repo.init()
            repo.commit_all("workspace: create SFDX project", allow_empty=True)
        return repo
