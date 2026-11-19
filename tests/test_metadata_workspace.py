from __future__ import annotations

import json
import os
import subprocess

import pytest

from core.errors import InputValidationError, WorkspaceError
from core.gitrepo import GitRepo
from core.metadata import load_baselines

from .conftest import SERVICE

COMPONENTS = ["ApexTrigger:CaseTrigger", "ApexClass:CaseTriggerHandler", "ApexClass:CaseEscalationService"]


def test_retrieve_creates_repo_commit_and_baselines(inv, project):
    out = inv.retrieve_components("prod", "T-1", COMPONENTS)
    assert out["retrieved_count"] == 6 and out["changed_since_last_retrieve"] is True
    paths = {f["path"] for f in out["files"]}
    assert SERVICE in paths
    assert all(p.startswith("metadata/force-app/main/default/") for p in paths)

    repo = GitRepo(project.ticket("T-1") / "metadata")
    assert repo.is_repo() and repo.is_clean()
    log = repo.log()
    assert log[0]["commit"] == out["commit"]
    assert log[0]["subject"].startswith("retrieve: ApexTrigger:CaseTrigger")
    assert log[-1]["subject"] == "workspace: create SFDX project"
    baselines = load_baselines(repo.path)
    assert set(baselines) == {p[len("metadata/"):] for p in paths}
    assert set(baselines.values()) == {out["commit"]}
    assert (repo.path / "sfdx-project.json").is_file()
    # The fixed identity is used; the user's git identity is not written into the workspace.
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=repo.path, capture_output=True, text=True)
    assert author.stdout.strip() == "sf-investigator <sf-investigator@localhost>"


def test_second_identical_retrieve_makes_no_commit(inv):
    first = inv.retrieve_components("prod", "T-1", COMPONENTS)
    second = inv.retrieve_components("prod", "T-1", COMPONENTS)
    assert second["commit"] == first["commit"] and second["changed_since_last_retrieve"] is False


def test_unknown_component_is_reported(inv):
    out = inv.retrieve_components("prod", "T-1", ["ApexClass:DoesNotExist", "ApexClass:CaseTriggerHandler"])
    assert out["retrieved_count"] == 2
    assert any("DoesNotExist" in p for p in out["problems"])
    only_missing = inv.retrieve_components("prod", "T-2", ["ApexClass:DoesNotExist"])
    assert only_missing["retrieved_count"] == 0 and "Nothing was retrieved" in only_missing["note"]


def test_wildcard_and_child_types(inv):
    out = inv.retrieve_components("prod", "T-1", ["ApexClass:*", "Flow:Case_Premium_Notification",
                                                  "CustomField:Account.Support_Tier__c"])
    names = {(f["type"], f["name"]) for f in out["files"]}
    assert ("ApexClass", "CaseEscalationServiceTest") in names
    assert ("Flow", "Case_Premium_Notification") in names
    assert ("CustomField", "Account.Support_Tier__c") in names


def test_local_edits_are_committed_before_a_retrieve(inv, project):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    service = project.ticket("T-1") / SERVICE
    original = service.read_text()
    service.write_text(original.replace("return;", "continue;"))
    out = inv.retrieve_components("prod", "T-1", ["ApexClass:CaseEscalationService"])
    saved = out["local_edits_saved_in_commit"]
    repo = GitRepo(project.ticket("T-1") / "metadata")
    assert "continue;" in repo.show(saved, SERVICE[len("metadata/"):])
    assert service.read_text() == original  # the working tree shows the org's version again
    assert "local_edits_note" in out


@pytest.mark.parametrize("components", [
    [], None, "ApexClass:A", ["ApexClass:A; rm -rf /"], ["../x:y"], ["ApexClass:" + "a" * 300], [3],
    ["ApexClass:A"] * 0, [f"ApexClass:C{i}" for i in range(51)],
])
def test_component_validation(inv, components):
    with pytest.raises(InputValidationError):
        inv.retrieve_components("prod", "T-1", components)


def test_invalid_ticket_keys(inv):
    for key in ("", "../x", "a/b", "-", "__", "a" * 80, "x y", None):
        with pytest.raises(InputValidationError):
            inv.init_ticket(key)


def test_search_line_numbers(inv):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    out = inv.search_metadata("T-1", r"^\s*return;", glob="*.cls")
    assert [(m["path"], m["line"]) for m in out["matches"]] == [(SERVICE, 19), (SERVICE, 30)]
    assert out["matches"][1]["text"] == "return;"
    context = inv.search_metadata("T-1", "PREMIUM_TIER\\)", context_lines=1)
    assert context["matches"][0]["line"] == 28
    assert context["matches"][0]["context"][0].startswith("27: ")
    assert context["matches"][0]["context"][-1].startswith("29: ")


def test_search_options_and_limits(inv):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    assert inv.search_metadata("T-1", "premium_tier", ignore_case=False)["match_count"] == 0
    assert inv.search_metadata("T-1", "premium_tier")["match_count"] == 2
    assert inv.search_metadata("T-1", "Case", glob="*.trigger")["files_searched"] == 1
    truncated = inv.search_metadata("T-1", "e", max_results=3)
    assert truncated["match_count"] == 3 and truncated["truncated"] is True
    empty = inv.search_metadata("T-1", "NothingLikeThis")
    assert "only covers retrieved files" in empty["note"]
    for bad in ("", "(", "x" * 501):
        with pytest.raises(InputValidationError):
            inv.search_metadata("T-1", bad)
    with pytest.raises(InputValidationError):
        inv.search_metadata("T-1", "x", scope="everything")


def test_search_never_looks_inside_git(inv):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    out = inv.search_metadata("T-1", "retrieve", scope="all", max_results=500)
    assert not any(".git" in m["path"].split("/") for m in out["matches"])
    assert not any(".git" in f.split("/") for f in inv.list_files("T-1", scope="all")["files"])


def test_read_file_ranges(inv):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    out = inv.read_file("T-1", SERVICE, start_line=28, end_line=30)
    assert out["content"].splitlines() == [
        "28:             if (account == null || account.Support_Tier__c != PREMIUM_TIER) {",
        "29:                 // Not a premium customer: nothing to do.",
        "30:                 return;",
    ]
    assert (out["start_line"], out["end_line"], out["total_lines"]) == (28, 30, 35)
    whole = inv.read_file("T-1", SERVICE)
    assert whole["end_line"] == 35 and "note" not in whole
    capped = inv.read_file("T-1", SERVICE, max_lines=10)
    assert capped["end_line"] == 10 and "start_line" in capped["note"]
    beyond = inv.read_file("T-1", SERVICE, start_line=100)
    assert beyond["content"] == ""
    past_end = inv.read_file("T-1", SERVICE, start_line=34, end_line=999)
    assert past_end["end_line"] == 35


def test_path_confinement(inv, project, tmp_path):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    inv.init_ticket("T-2")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (project.root / "sfti.config.json").write_text("{}")
    for path in ("../../sfti.config.json", "../T-2/ticket.md", str(secret), "metadata/../../../secret.txt",
                 "/etc/passwd", "metadata/.git/config", ".git", "metadata/.git/sfti-baseline.json", "nul\x00byte"):
        with pytest.raises(WorkspaceError):
            inv.read_file("T-1", path)
    with pytest.raises(InputValidationError):
        inv.read_file("T-1", "")
    with pytest.raises(WorkspaceError):
        inv.read_file("T-1", "metadata/force-app/missing.cls")
    with pytest.raises(WorkspaceError):
        inv.read_file("T-1", "metadata")
    with pytest.raises(WorkspaceError):
        inv.read_file("NOPE-1", "ticket.md")
    # An absolute path that does point inside the ticket folder is accepted.
    inside = project.ticket("T-1") / SERVICE
    assert inv.read_file("T-1", str(inside))["path"] == SERVICE


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_out_of_the_workspace_is_refused(inv, project, tmp_path):
    inv.init_ticket("T-1")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (project.ticket("T-1") / "attachments" / "link.txt").symlink_to(secret)
    with pytest.raises(WorkspaceError):
        inv.read_file("T-1", "attachments/link.txt")


def test_binary_file_is_refused(inv, project):
    inv.init_ticket("T-1")
    (project.ticket("T-1") / "attachments" / "shot.png").write_bytes(b"\x89PNG\x00\x00")
    with pytest.raises(WorkspaceError):
        inv.read_file("T-1", "attachments/shot.png")


def test_list_files(inv):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    out = inv.list_files("T-1", glob="*.cls")
    assert out["count"] == 2 and SERVICE in out["files"]
    assert inv.list_files("T-1", scope="logs")["count"] == 0


def test_workspace_diff(inv, project):
    inv.retrieve_components("prod", "T-1", COMPONENTS)
    assert "No local edits" in inv.workspace_diff("T-1")["note"]
    assert not (project.ticket("T-1") / "fix.diff").exists()
    service = project.ticket("T-1") / SERVICE
    service.write_text(service.read_text().replace("                return;", "                continue;"))
    new_class = service.with_name("Brand_New.cls")
    new_class.write_text("public class Brand_New {}\n")
    out = inv.workspace_diff("T-1")
    assert {(f["status"], f["path"]) for f in out["files"]} == {
        ("modified", SERVICE), ("added", "metadata/force-app/main/default/classes/Brand_New.cls")}
    diff = (project.ticket("T-1") / "fix.diff").read_text()
    assert out["saved_to"] == "fix.diff" and out["diff"] == diff
    assert "-                return;\n+                continue;\n" in diff
    assert "--- /dev/null\n+++ b/force-app/main/default/classes/Brand_New.cls" in diff
    assert "@@ -27,7 +27,7 @@" in diff
    # The diff applies cleanly to the retrieved version.
    repo = project.ticket("T-1") / "metadata"
    subprocess.run(["git", "stash", "-u", "-q"], cwd=repo, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"})
    check = subprocess.run(["git", "apply", "--check", str(project.ticket("T-1") / "fix.diff")], cwd=repo,
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("check, value", [
    ("ticket", "ABC-123\n"), ("org", "sandbox\n"), ("component", "ApexClass:CaseService\n"),
    ("record", "500Ws00000C0003IAA\n"), ("log", "07LWs00000L0001MAA\n"),
])
def test_a_trailing_newline_is_refused_by_the_input_checks(check, value):
    """`$` also matches before a final newline: "ABC-123\\n" made a folder whose name ends in a line break."""
    from core.errors import InputValidationError
    from core.logs import _LOG_ID_RE
    from core.metadata import validate_components
    from core.sf_cli import validate_target_org
    from core.timeline import _RECORD_ID_RE
    from core.workspace import validate_ticket_key

    if check in ("record", "log"):
        assert not (_RECORD_ID_RE if check == "record" else _LOG_ID_RE).match(value)
        assert (_RECORD_ID_RE if check == "record" else _LOG_ID_RE).match(value.strip())
        return
    validate = {"ticket": validate_ticket_key, "org": validate_target_org,
                "component": lambda v: validate_components([v])}[check]
    with pytest.raises(InputValidationError):
        validate(value)
    validate(value.strip())
