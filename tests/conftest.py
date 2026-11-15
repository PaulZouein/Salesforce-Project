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

MIXED_BATCH_TEST = '''
    @isTest
    static void mixedBatchEscalatesEveryPremiumCase() {
        Account standard = createAccount('Standard Test Account', 'Standard');
        Account premium = createAccount('Premium Test Account', 'Premium');

        Test.startTest();
        List<Case> batch = new List<Case>{
            new Case(Subject = 'Standard first', AccountId = standard.Id, Priority = 'Medium'),
            new Case(Subject = 'Premium second', AccountId = premium.Id, Priority = 'Medium')
        };
        insert batch;
        Test.stopTest();

        Map<Id, Case> saved = new Map<Id, Case>([SELECT Priority FROM Case WHERE Id IN :batch]);
        System.assertEquals('Medium', saved.get(batch[0].Id).Priority, 'A Standard case keeps its priority');
        System.assertEquals('High', saved.get(batch[1].Id).Priority,
            'Every Premium case in a mixed batch should be High');
    }
}
'''


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


def apply_planted_bug_fix(ticket_dir: Path) -> None:
    """Make the DEMO-101 fix in a ticket workspace: ``return;`` -> ``continue;`` plus the new test."""
    service = ticket_dir / SERVICE
    lines = service.read_text(encoding="utf-8").split("\n")
    assert lines[29].strip() == "return;", lines[29]
    lines[29] = lines[29].replace("return;", "continue;")
    service.write_text("\n".join(lines), encoding="utf-8")

    test_class = ticket_dir / SERVICE_TEST
    text = test_class.read_text(encoding="utf-8").rstrip()
    assert text.endswith("}")
    test_class.write_text(text[:-1].rstrip() + "\n" + MIXED_BATCH_TEST, encoding="utf-8")
