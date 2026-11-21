"""Minimal git helper for the per-ticket metadata repositories.

Commits are made with a fixed identity passed on the command line, so no global git
configuration is needed and the user's own identity is not written into ticket workspaces.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import GitError

_IDENTITY = [
    "-c", "user.name=sf-investigator",
    "-c", "user.email=sf-investigator@localhost",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "core.quotepath=false",
    "-c", "core.longpaths=true",  # Windows: deep metadata paths under a long project folder
    # Nothing in a ticket's repository may make git run a program: these override its own settings.
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.pager=cat",
]

# Settings that make git run a program and that "-c" cannot switch off by name (filter and diff drivers
# are named by the repository). The tool never writes them; finding one means someone else did.
_RUNS_A_PROGRAM = re.compile(
    r"^\s*\[\s*(filter|diff|merge|include|includeif|alias|credential|uploadpack)\b"
    r"|^\s*(fsmonitor|hookspath|sshcommand|editor|pager|askpass|gitproxy|textconv|command|clean|smudge|process)\s*=",
    re.IGNORECASE | re.MULTILINE,
)


class GitRepo:
    def __init__(self, path: Path):
        self.path = Path(path)

    # -- plumbing ------------------------------------------------------------------------

    def _git(self, args: Sequence[str], *, check: bool = True, timeout: float = 120.0) -> subprocess.CompletedProcess:
        exe = shutil.which("git")
        if not exe:
            raise GitError("git was not found on PATH.", hint="Install git and reopen your terminal.")
        self._check_settings()
        try:
            proc = subprocess.run(
                [exe, *_IDENTITY, *args],
                cwd=str(self.path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args[:2])} timed out.") from None
        except OSError as exc:
            raise GitError(f"Could not run git: {exc}") from None
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:500]}",
            )
        return proc

    def _check_settings(self) -> None:
        """Refuse to run git in a repository whose own settings would make it run a program."""
        config = self.path / ".git" / "config"
        try:
            text = config.read_text(encoding="utf-8", errors="replace") if config.is_file() else ""
        except OSError:
            return
        found = _RUNS_A_PROGRAM.search(text)
        if found:
            raise GitError(
                f"The git settings of {self.path.name} were changed outside the tool ({found.group(0).strip()!r} in "
                ".git/config): git would run a program from them. Nothing was run.",
                hint="Remove that setting from the file yourself, or delete the folder and retrieve again.",
            )

    # -- repository ----------------------------------------------------------------------

    def is_repo(self) -> bool:
        return (self.path / ".git").exists()

    def init(self) -> None:
        if self.is_repo():
            return
        self.path.mkdir(parents=True, exist_ok=True)
        self._git(["init", "--quiet"])
        # Keep the branch name stable regardless of the user's init.defaultBranch.
        self._git(["symbolic-ref", "HEAD", "refs/heads/main"])

    def head(self) -> str | None:
        proc = self._git(["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def is_clean(self) -> bool:
        return not self._git(["status", "--porcelain"]).stdout.strip()

    def status(self) -> list[str]:
        return [line for line in self._git(["status", "--porcelain"]).stdout.splitlines() if line.strip()]

    def commit_all(self, message: str, *, allow_empty: bool = False) -> str | None:
        """Stage everything and commit. Returns the new hash, or ``None`` if nothing changed."""
        self._git(["add", "-A"])
        staged = self._git(["diff", "--cached", "--quiet"], check=False).returncode != 0
        if not staged and not allow_empty and self.head() is not None:
            return None
        args = ["commit", "--quiet", "-m", message]
        if not staged:
            args.append("--allow-empty")
        self._git(args)
        return self.head()

    # -- inspection ----------------------------------------------------------------------

    def diff(self, a: str, b: str | None = None, paths: Sequence[str] | None = None) -> str:
        """Unified diff between two commits, or between a commit and the working tree."""
        args = ["diff", "--no-color", a]
        if b:
            args.append(b)
        if paths:
            args += ["--", *paths]
        return self._git(args).stdout

    def differs(self, a: str, b: str | None = None, paths: Sequence[str] | None = None) -> bool:
        args = ["diff", "--quiet", a]
        if b:
            args.append(b)
        if paths:
            args += ["--", *paths]
        return self._git(args, check=False).returncode != 0

    def changed_files(self, a: str, b: str | None = None) -> list[tuple[str, str]]:
        """``(status, path)`` pairs between two commits or a commit and the working tree."""
        args = ["diff", "--name-status", "--no-renames", a]
        if b:
            args.append(b)
        out: list[tuple[str, str]] = []
        for line in self._git(args).stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                out.append((parts[0].strip(), parts[1].strip()))
        return out

    def exists_in(self, commit: str, path: str) -> bool:
        return self._git(["cat-file", "-e", f"{commit}:{path}"], check=False).returncode == 0

    def show(self, commit: str, path: str) -> str:
        return self._git(["show", f"{commit}:{path}"]).stdout

    def list_files(self) -> list[str]:
        """Tracked and untracked (not ignored) files in the working tree."""
        out = self._git(["ls-files", "--cached", "--others", "--exclude-standard"]).stdout
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def log(self, limit: int = 20) -> list[dict[str, str]]:
        if self.head() is None:
            return []
        proc = self._git(["log", f"-{int(limit)}", "--pretty=format:%H%x09%cI%x09%s"])
        entries = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                entries.append({"commit": parts[0], "date": parts[1], "subject": parts[2]})
        return entries

    # -- working tree --------------------------------------------------------------------

    def checkout_paths(self, commit: str, paths: Sequence[str]) -> None:
        """Make ``paths`` in the working tree match ``commit`` (files absent there are removed)."""
        for path in paths:
            if self.exists_in(commit, path):
                self._git(["checkout", commit, "--", path])
            else:
                target = self.path / path
                if target.exists():
                    target.unlink()
                self._git(["rm", "--quiet", "--cached", "--ignore-unmatch", "--", path], check=False)

    def restore_worktree(self, commit: str) -> None:
        """Make the whole working tree match ``commit`` without moving HEAD."""
        self._git(["checkout", commit, "--", "."])
        # Remove files that exist in the tree but not in ``commit``.
        for status, path in self.changed_files(commit):
            if status == "A":
                target = self.path / path
                if target.exists():
                    target.unlink()
        for line in self.status():
            if line.startswith("??"):
                target = self.path / line[3:].strip().strip('"')
                if target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
