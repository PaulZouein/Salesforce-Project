"""Build a simulated-org state folder from the planted-bug sample.

``python -m simulator setup`` creates ``simulator/state/`` with three orgs that share the
same starting content and differ only in how they classify:

* ``demo-sandbox`` — a sandbox (writes can be authorized)
* ``demo-prod``    — production (always read-only)
* ``demo-dev``     — a Developer Edition org (read-only unless enabled in the settings)
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = PROJECT_ROOT / "samples" / "planted-bug"
DEFAULT_STATE = Path(__file__).resolve().parent / "state"

DEFAULT_ORGS: list[dict[str, Any]] = [
    {"alias": "demo-sandbox", "username": "admin@demo.example.com.sbx", "orgId": "00DWs000000SbxAMAS",
     "name": "Demo Co (Sandbox)", "instanceUrl": "https://demo--sbx.sandbox.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": True, "organizationType": "Enterprise Edition", "instanceName": "CS101"},
    {"alias": "demo-prod", "username": "admin@demo.example.com", "orgId": "00DWs000000PrdAMAS",
     "name": "Demo Co", "instanceUrl": "https://demo.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": False, "organizationType": "Enterprise Edition", "instanceName": "EU45"},
    {"alias": "demo-dev", "username": "admin@demo-dev.example.com", "orgId": "00DWs000000DevAMAS",
     "name": "Demo Developer Org", "instanceUrl": "https://demo-dev-ed.develop.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": False, "organizationType": "Developer Edition", "instanceName": "NA224"},
]


def build_state(state: Path = DEFAULT_STATE, orgs: list[dict[str, Any]] | None = None, sample: Path = SAMPLE) -> Path:
    """Create (or recreate) a simulator state folder and return the path of its ``sf`` launcher."""
    state = Path(state).resolve()
    if state.exists():
        shutil.rmtree(state)
    for org in orgs or DEFAULT_ORGS:
        folder = state / "orgs" / org["alias"]
        folder.mkdir(parents=True)
        shutil.copytree(sample / "force-app" / "main" / "default", folder / "source")
        for item in (sample / "org-data").iterdir():
            target = folder / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copyfile(item, target)
        (folder / "org.json").write_text(json.dumps(org, indent=2), encoding="utf-8")
    return write_launcher(state)


def write_launcher(state: Path) -> Path:
    bin_dir = state / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parent / "fake_sf.py"
    if os.name == "nt":
        launcher = bin_dir / "sf.cmd"
        launcher.write_text(
            f'@echo off\r\nset "SFTI_SIM_STATE={state}"\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = bin_dir / "sf"
        launcher.write_text(
            f'#!/bin/sh\nSFTI_SIM_STATE="{state}" exec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher
