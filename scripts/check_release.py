"""Prove that the release zip sets itself up from nothing on this system.

    python scripts/check_release.py

Builds the zip, unpacks it into an empty temporary folder, and runs that copy's own start
script for this system with ``--check``: create the private environment, install, load the
project, start the page's server once and ask it for its status. Nothing is left behind.
The automated checks run this on Windows and macOS.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_release  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sfti-release-", ignore_cleanup_errors=True) as tmp:
        archive = make_release.build(out_dir=Path(tmp) / "dist")
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(Path(tmp) / "unpacked")
        folder = Path(tmp) / "unpacked" / make_release.FOLDER_NAME
        print(f"Unpacked {archive.name} ({archive.stat().st_size / 1_000_000:.1f} MB) into an empty folder.", flush=True)
        if os.name == "nt":
            command = ["cmd", "/c", "start-demo.bat", "--check"]
        else:
            command = ["sh", "./start-demo.command", "--check"]
        # No keyboard: a start script that stops to ask something fails here instead of waiting.
        result = subprocess.run(command, cwd=str(folder), stdin=subprocess.DEVNULL)
        print(f"Start script finished with code {result.returncode}.", flush=True)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
