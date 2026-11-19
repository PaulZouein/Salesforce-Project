"""A write reaches the org that was checked, even if its alias is pointed at another org in the meantime.

Found by two independent reviews. The permit used to be bound to the alias text: an alias re-pointed at
production between the guard's check and the write (in another terminal, or from the page's Connect)
sent an approved sandbox write to production. Permits now carry the org's username, which cannot be
re-pointed, and every permitted command runs against it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.app import Investigator
from core.errors import InputValidationError
from core.sf_cli import ProcessResult, SfCli, default_runner

from .conftest import FIX_COMPONENTS, apply_planted_bug_fix

KEY = "T-1"
CASE = "500Ws00000C0003IAA"
SERVICE = "classes/CaseEscalationService.cls"


class RepointAfterGuardCheck:
    """Runs the simulator; right after the guard's Organization query it points 'sandbox' at production."""

    def __init__(self, sim):
        self.sim, self.armed, self.fired = sim, False, False

    def __call__(self, argv, cwd, timeout, env, **kw):
        result = default_runner(argv, cwd, timeout, env, **kw)
        query = Path(cwd or ".") / "query.soql"
        if self.armed and query.is_file() and "FROM Organization" in query.read_text(encoding="utf-8"):
            self.armed, self.fired = False, True
            for folder, alias in ((self.sim.org_dir("sandbox"), "old-sandbox"), (self.sim.org_dir("prod"), "sandbox")):
                org = json.loads((folder / "org.json").read_text(encoding="utf-8"))
                org["alias"] = alias
                (folder / "org.json").write_text(json.dumps(org), encoding="utf-8")
        return result


def make(sim, project):
    project.write_config(mode="apply")
    runner = RepointAfterGuardCheck(sim)
    return Investigator(root=project.root, cli=SfCli(executable=str(sim.launcher), runner=runner)), runner


def test_a_record_update_approved_for_the_sandbox_stays_in_the_sandbox(sim, project):
    inv, runner = make(sim, project)
    inv.init_ticket(KEY)
    prod_before = sim.records("prod")      # records, not the folder: re-pointing the alias edits prod's org.json
    runner.armed = True
    out = inv.update_records("sandbox", KEY, "Case", [{"id": CASE, "fields": {"Priority": "Low"}}],
                             reason="approved by the user for the sandbox", approved_by="tester")
    assert runner.fired and out["classification"] == "sandbox"
    assert sim.records("prod") == prod_before                         # production untouched
    assert next(c for c in sim.records("sandbox")["Case"] if c["Id"] == CASE)["Priority"] == "Low"


def test_a_deploy_approved_for_the_sandbox_stays_in_the_sandbox(sim, project):
    inv, runner = make(sim, project)
    inv.retrieve_components("sandbox", KEY, FIX_COMPONENTS + ["ApexClass:CaseTriggerHandler"])
    apply_planted_bug_fix(project.ticket(KEY))
    prod_source = sim.source("prod", SERVICE).read_bytes()
    runner.armed = True
    out = inv.deploy_component("sandbox", KEY, ["ApexClass:CaseEscalationService"], reason="T-1 fix, sandbox only")
    assert runner.fired and out["status"] == "deployed"
    assert sim.source("prod", SERVICE).read_bytes() == prod_source    # production code untouched
    assert "continue;" in sim.source("sandbox", SERVICE).read_text(encoding="utf-8")


def test_the_page_does_not_silently_repoint_a_name_in_use(sim, project):
    from webapp.server import App

    calls = []

    def login_runner(argv, cwd, timeout, env, *, input_text=None):
        calls.append(argv)
        return ProcessResult(0, json.dumps({"status": 0, "result": {"orgId": "00D5g000000AbCdEAG"}}), "")

    app = App(root=project.root)
    real = sim.cli()
    cli = SfCli(executable=str(sim.launcher),
                runner=lambda argv, cwd, timeout, env, **kw: (login_runner(argv, cwd, timeout, env, **kw)
                                                            if "login" in argv else real._runner(argv, cwd, timeout, env)))
    app._inv = Investigator(root=project.root, cli=cli)
    url = "force://PlatformCLI::5Aep861TSESvWeug_xvFHRBTTbf_YrTWgEyjBJAbCdEf@prod.my.salesforce.com"
    with pytest.raises(InputValidationError) as refused:
        app.connect_org("sandbox", url)
    assert refused.value.details == {"alias_in_use": "sandbox"} and calls == []
    app.connect_org("sandbox", url, replace=True)                     # the user confirmed on the page
    assert calls and calls[0][calls[0].index("--alias") + 1] == "sandbox"
    app.connect_org("brand-new-name", url)                            # a free name needs no confirmation
