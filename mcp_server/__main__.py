"""``python -m mcp_server``: the investigator's tools for Claude Code, over standard input and output.

Claude Code starts it with ``${SFTI_PYTHON:-python}`` (see ``.mcp.json``). ``start.bat`` and
``start.command`` set ``SFTI_PYTHON`` to the project's own ``.venv``; when Claude Code is opened
some other way (from an editor, or a plain terminal), the plain ``python`` it finds may not have
the packages. Then this hands over to the ``.venv`` that the start file created, instead of failing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _project_python() -> Path | None:
    """The project's ``.venv`` Python, when this one lacks the packages and that one exists."""
    try:
        import mcp  # noqa: F401
        return None
    except ImportError:
        pass
    venv = Path(__file__).resolve().parent.parent / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or Path(sys.prefix).resolve() == venv.resolve():
        return None  # nothing better to hand over to: the import below says what is missing
    return python


def _run() -> None:
    python = _project_python()
    if python is not None:
        args = [str(python), "-m", "mcp_server", *sys.argv[1:]]
        if os.name == "nt":
            # Passed on explicitly: Windows gives a child the parent's pipes only when told to, and the MCP
            # channel is those pipes.
            raise SystemExit(subprocess.call(args, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr))
        os.execv(str(python), args)
    from .server import main

    main()


if __name__ == "__main__":
    _run()
