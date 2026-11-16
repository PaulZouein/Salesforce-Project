"""Shared fixtures.

Integration tests run the real subprocess path against the simulated org in ``simulator/``:
``SfCli`` starts the simulator's ``sf`` launcher exactly as it would start the Salesforce CLI.
No Salesforce org and no network are involved.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.app import Investigator  # noqa: E402
from core.sf_cli import ProcessResult, SfCli  # noqa: E402
from simulator.build import build_state  # noqa: E402
from simulator.demo import MIXED_BATCH_TEST, apply_planted_bug_fix  # noqa: E402,F401

#: The simulator hands this out as an access token; it must never reach a tool result.
SIMULATED_TOKEN = "00D-SIMULATED-TOKEN-THAT-MUST-NEVER-BE-SHOWN"

TEST_ORGS: list[dict[str, Any]] = [
    {"alias": "sandbox", "username": "admin@test.example.com.sbx", "orgId": "00DWs000000SbxAMAS",
     "name": "Test Co (Sandbox)", "instanceUrl": "https://test--sbx.sandbox.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": True, "organizationType": "Enterprise Edition"},
    {"alias": "prod", "username": "admin@test.example.com", "orgId": "00DWs000000PrdAMAS",
     "name": "Test Co", "instanceUrl": "https://test.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": False, "organizationType": "Enterprise Edition"},
    {"alias": "dev", "username": "admin@test-dev.example.com", "orgId": "00DWs000000DevAMAS",
     "name": "Test Developer Org", "instanceUrl": "https://test-dev-ed.develop.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": False, "organizationType": "Developer Edition"},
    {"alias": "scratch", "username": "test-scratch@example.com", "orgId": "00DWs000000ScrAMAS",
     "name": "Scratch", "instanceUrl": "https://scratch.scratch.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": True, "isScratch": True, "organizationType": "Developer Edition"},
    {"alias": "broken", "username": "admin@broken.example.com", "orgId": "00DWs000000BrkAMAS",
     "name": "Broken", "instanceUrl": "https://broken.my.salesforce.com",
     "apiVersion": "62.0", "isSandbox": True, "organizationType": "Enterprise Edition",
     "organizationRecordMissing": True},
]

SERVICE = "metadata/force-app/main/default/classes/CaseEscalationService.cls"
SERVICE_TEST = "metadata/force-app/main/default/classes/CaseEscalationServiceTest.cls"
FIX_COMPONENTS = ["ApexClass:CaseEscalationService", "ApexClass:CaseEscalationServiceTest"]

class Sim:
    """A simulator state folder and its ``sf`` launcher."""

    def __init__(self, state: Path, launcher: Path):
        self.state = state
        self.launcher = launcher

    def org_dir(self, alias: str) -> Path:
        return self.state / "orgs" / alias

    def source(self, alias: str, relative: str) -> Path:
        return self.org_dir(alias) / "source" / relative

    def records(self, alias: str) -> dict[str, list[dict[str, Any]]]:
        return json.loads((self.org_dir(alias) / "records.json").read_text(encoding="utf-8"))

    def fingerprint(self, alias: str) -> str:
        """Hash of every file of one simulated org: equal fingerprints mean nothing was written."""
        digest = hashlib.sha256()
        base = self.org_dir(alias)
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            digest.update(path.relative_to(base).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def cli(self, **kwargs: Any) -> SfCli:
        return SfCli(executable=str(self.launcher), **kwargs)


class Project:
    """A temporary project root (never the repository's own)."""

    def __init__(self, root: Path):
        self.root = root

    def write_config(self, **settings: Any) -> None:
        (self.root / "sfti.config.json").write_text(json.dumps(settings), encoding="utf-8")

    def ticket(self, key: str) -> Path:
        return self.root / "tickets" / key


def _private_folders_snapshot() -> dict[str, tuple[int, int]]:
    seen: dict[str, tuple[int, int]] = {}
    for name in ("tickets", "knowledge", "audit"):
        folder = ROOT / name
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file():
                    info = path.stat()
                    seen[path.relative_to(ROOT).as_posix()] = (info.st_mtime_ns, info.st_size)
    return seen


@pytest.fixture(scope="session", autouse=True)
def tests_never_touch_real_tickets_or_org_memory():
    """Every test works in a temporary folder. The tickets, org memory and audit data in this checkout belong
    to whoever runs the suite, and running it must leave them exactly as they were."""
    before = _private_folders_snapshot()
    yield
    after = _private_folders_snapshot()
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    assert not changed, ("The test run changed files in tickets/, knowledge/ or audit/ of this checkout "
                         f"(unless you were using the investigator at the same time): {changed[:10]}")


@pytest.fixture
def sim(tmp_path: Path) -> Sim:
    state = tmp_path / "sim-state"
    launcher = build_state(state, orgs=TEST_ORGS)
    return Sim(state, launcher)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "project"
    root.mkdir()
    return Project(root)


@pytest.fixture
def inv(sim: Sim, project: Project) -> Investigator:
    """Investigator in the default (advise) mode."""
    return Investigator(root=project.root, cli=sim.cli())


@pytest.fixture
def apply_inv(sim: Sim, project: Project) -> Investigator:
    """Investigator with mode ``apply`` set in a temporary config file."""
    project.write_config(mode="apply")
    return Investigator(root=project.root, cli=sim.cli())


class RecordingRunner:
    """Injected in place of the subprocess runner: records calls and replays canned output."""

    def __init__(self, responses: list[Any] | None = None):
        self.calls: list[list[str]] = []
        self.responses = list(responses or [])

    def __call__(self, argv: list[str], cwd: str | None, timeout: float, env: dict[str, str]) -> ProcessResult:
        self.calls.append(list(argv))
        if not self.responses:
            return ProcessResult(0, json.dumps({"status": 0, "result": {}}), "")
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, ProcessResult):
            return response
        return ProcessResult(int(response.get("status", 0)) if isinstance(response, dict) else 0,
                             json.dumps(response), "")


@pytest.fixture
def recording_runner() -> type[RecordingRunner]:
    return RecordingRunner
