"""Applying and undoing changes in a non-production org.

Every function that writes goes through ``org_guard`` first, snapshots the org's current
state into the ticket's git repository before touching it, verifies the result afterwards,
and records what happened in the audit log.

Deploy sequence::

    proposed   commit the local edits
    snapshot   retrieve the same components from the org and commit them ("pre")
    drift      stop if the org no longer matches the version the edit was based on
    deploy     sf project deploy start --metadata ...
    verify     retrieve again and compare with what was deployed
    record     change row with both commits, so the change can be rolled back
"""

from __future__ import annotations

import difflib
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Sequence

from .audit import OUTCOME_FAILED, OUTCOME_IN_PROGRESS, OUTCOME_SUCCEEDED, AuditLog, new_change_id
from .errors import (
    ChangeError,
    DeployFailedError,
    DeployInProgressError,
    DriftError,
    InputValidationError,
    SfCliError,
    SfCliTimeoutError,
    SfInvestigatorError,
)
from .gitrepo import GitRepo
from .logs import api_version_of, rest_request
from .metadata import (
    _relative_to_repo,
    load_baselines,
    run_retrieve,
    save_baselines,
    set_baselines,
    validate_components,
)
from .org_guard import OrgGuard, OrgInfo
from .query import Redactor, run_raw_query, soql_literal, strip_attributes, validate_api_name
from .sf_cli import SfCli, validate_target_org
from .workspace import Workspace

MAX_DEPLOY_COMPONENTS = 5
MAX_RECORDS_PER_UPDATE = 10
MAX_DIFF_CHARS = 20000
MAX_APEX_CHARS = 20000
DEPLOY_TEST_LEVELS = ("NoTestRun", "RunSpecifiedTests", "RunLocalTests")

KIND_DEPLOY = "deploy"
KIND_RECORDS = "records"

STATUS_DEPLOYED = "deployed"
STATUS_VALIDATED = "validated"
STATUS_FAILED = "failed"
STATUS_APPLIED = "applied"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ROLLBACK_FAILED = "rollback_failed"
STATUS_IN_PROGRESS = "in_progress"  # still running in the org when the wait ran out: it may change the org

# Deploy statuses of the Metadata API that mean "not finished yet" (plugin-deploy-retrieve exits 69 on them).
_UNFINISHED_DEPLOY_STATUSES = ("InProgress", "Pending", "Canceling")

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?\Z")
_TEST_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}(\.[A-Za-z][A-Za-z0-9_]{0,79})?\Z")


def _truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… diff truncated ({len(text)} characters in total)"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _require_reason(reason: str) -> str:
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise InputValidationError("A reason is required (what this change is for).")
    return reason.strip()[:500]


def _spec(entry: dict[str, Any]) -> str:
    return f"{entry.get('type')}:{entry.get('name')}"


def _rollback_plan(change: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Components and files a rollback restores (changed) and deletes (created), from the change record."""
    detail = change.get("detail") or {}
    deployed = detail.get("deployed") or []
    changed_specs = list(dict.fromkeys(_spec(f) for f in deployed if f.get("state") == "Changed"))
    created_specs = list(dict.fromkeys(_spec(f) for f in deployed if f.get("state") == "Created"))
    changed_paths = list(detail.get("modified_paths") or [])
    created_paths = [f["path"] for f in deployed if f.get("state") == "Created" and f.get("path")]
    change_id = change.get("change_id")
    if not changed_specs and not created_specs:
        raise ChangeError(f"Change {change_id} recorded no modified or created components; nothing to undo.")
    if changed_specs and not changed_paths:
        # Restoring nothing and redeploying the working tree would deploy the fix again and call it a rollback.
        raise ChangeError(
            f"Change {change_id} changed {', '.join(changed_specs)} but recorded no file paths to restore, so it "
            "cannot be rolled back safely.",
            hint="Restore the earlier version by hand: the snapshot taken before the deploy is the commit "
            f"{change.get('pre_commit')} in the ticket's metadata repository.",
        )
    return changed_specs, created_specs, changed_paths, created_paths


def _deploy_still_running(exc: SfInvestigatorError) -> bool:
    """Did the command stop waiting while the deploy carries on in the org?

    ``project deploy start`` answers a wait that runs out with ``ClientTimeoutError``: "The command
    has timed out, although the deployment is still running." Our own time limit on the process
    (``SfCliTimeoutError``) leaves the org in the same unknown state.
    """
    if isinstance(exc, SfCliTimeoutError):
        return True
    text = f"{getattr(exc, 'name', '')} {exc.message}".lower()
    return getattr(exc, "exit_code", None) == 69 or "clienttimeout" in text or "still running" in text


class ChangeManager:
    def __init__(self, cli: SfCli, guard: OrgGuard, workspace: Workspace, audit: AuditLog):
        self.cli = cli
        self.guard = guard
        self.workspace = workspace
        self.audit = audit

    # ------------------------------------------------------------------------------------
    # Local, read-only helpers
    # ------------------------------------------------------------------------------------

    def workspace_diff(self, ticket_key: str, *, save: bool = True) -> dict[str, Any]:
        """Diff of the local edits against the version last retrieved from the org.

        This is how a proposed fix is shown as a diff. Nothing is sent to the org.
        """
        repo = self.workspace.metadata_repo(ticket_key)
        baselines = load_baselines(repo.path)
        present = set(repo.list_files())
        chunks: list[str] = []
        files: list[dict[str, str]] = []
        for path in sorted(present | set(baselines)):
            if not path.startswith("force-app/"):
                continue
            base = baselines.get(path)
            old = repo.show(base, path) if base and repo.exists_in(base, path) else ""
            target = repo.path / path
            new = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            if old == new:
                continue
            status = "added" if not base else ("deleted" if not target.is_file() else "modified")
            files.append({"status": status, "path": f"metadata/{path}"})
            chunks.append("".join(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"a/{path}" if base else "/dev/null",
                tofile=f"b/{path}" if target.is_file() else "/dev/null",
            )))
        diff = "\n".join(c if c.endswith("\n") else c + "\n" for c in chunks)
        out: dict[str, Any] = {"ticket_key": ticket_key, "files": files, "diff": _truncate(diff)}
        if not files:
            out["note"] = (
                "No local edits. To draft a fix, edit the retrieved files under "
                f"tickets/{ticket_key}/metadata/force-app/ and call this again."
            )
        elif save:
            (self.workspace.ticket_dir(ticket_key) / "fix.diff").write_text(diff, encoding="utf-8")
            out["saved_to"] = "fix.diff"
            out["note"] = "This diff is local only. Nothing has been deployed."
        return out

    def snapshot(self, ticket_key: str, message: str) -> dict[str, Any]:
        """Commit the current state of the ticket's metadata repository."""
        repo = self.workspace.metadata_repo(ticket_key)
        commit = repo.commit_all(f"snapshot: {message.strip()[:150] or 'manual'}")
        return {"commit": commit or repo.head(), "created": commit is not None}

    def list_changes(self, ticket_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        for change in self.audit.list_changes(ticket_key=ticket_key, limit=limit):
            detail = change.get("detail") if isinstance(change.get("detail"), dict) else {}
            entry = {k: change.get(k) for k in (
                "change_id", "ts", "kind", "ticket_key", "org_target", "org_id", "status",
                "pre_commit", "post_commit", "deploy_id", "reason",
            )}
            if change.get("kind") == KIND_DEPLOY:
                entry["components"] = detail.get("components")
            else:
                # Never echo stored field values: they may be personal data.
                entry["sobject"] = detail.get("sobject")
                entry["record_ids"] = [u.get("id") for u in detail.get("updates", [])]
                entry["fields"] = sorted({f for u in detail.get("updates", []) for f in (u.get("fields") or {})})
            out.append(entry)
        return out

    # ------------------------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------------------------

    def _fail(self, operation: str, info: OrgInfo, org: str, ticket_key: str, approved_by: str | None, detail: Any) -> None:
        self.audit.record(operation, OUTCOME_FAILED, org_target=org, org_id=info.org_id,
                          classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                          detail=detail)

    def _run_deploy(self, org: str, repo: GitRepo, specs: Sequence[str], permit: Any, *, test_level: str,
                    tests: Sequence[str], validate_only: bool, wait_minutes: int) -> dict[str, Any]:
        args = ["project", "deploy", "start"]
        for spec in specs:
            args += ["--metadata", spec]
        args += ["--target-org", org, "--test-level", test_level, "--wait", str(wait_minutes), "--ignore-conflicts"]
        for test in tests:
            args += ["--tests", test]
        if validate_only:
            args.append("--dry-run")
        # Exit codes of project deploy start: 1 failed, 68 partly succeeded, 69 not finished. Each carries a result.
        result = self.cli.run(args, permit=permit, cwd=repo.path, timeout=wait_minutes * 60 + 120,
                              accept_statuses=(0, 1, 68, 69)) or {}
        details = result.get("details") or {}
        failures = [
            {
                "component": f"{f.get('componentType')}:{f.get('fullName')}",
                "file": f.get("fileName"),
                "line": f.get("lineNumber"),
                "column": f.get("columnNumber"),
                "problem": f.get("problem"),
                "problem_type": f.get("problemType"),
            }
            for f in _as_list(details.get("componentFailures"))
            if isinstance(f, dict)
        ]
        test_result = details.get("runTestResult") or {}
        test_failures = [
            {
                "name": f"{f.get('name')}.{f.get('methodName')}",
                "message": str(f.get("message"))[:1500],
                "stack_trace": str(f.get("stackTrace"))[:1500],
            }
            for f in _as_list(test_result.get("failures"))
            if isinstance(f, dict)
        ]
        files = []
        for f in result.get("files") or []:
            if not isinstance(f, dict):
                continue
            rel = None
            if f.get("filePath"):
                rel = _relative_to_repo(repo.path, f["filePath"])
            files.append({"type": f.get("type"), "name": f.get("fullName"), "state": f.get("state"), "path": rel,
                          **({"error": f.get("error")} if f.get("error") else {})})
        return {
            "deploy_id": result.get("id"),
            "status": result.get("status"),
            "success": bool(result.get("success")) and result.get("status") in ("Succeeded", "SucceededPartial"),
            "in_progress": result.get("status") in _UNFINISHED_DEPLOY_STATUSES,
            "files": files,
            "component_failures": failures,
            "test_failures": test_failures,
            "tests_run": test_result.get("numTestsRun"),
        }

    def deploy_component(
        self,
        org: str,
        ticket_key: str,
        components: Sequence[str],
        *,
        reason: str,
        test_level: str = "NoTestRun",
        tests: Sequence[str] = (),
        validate_only: bool = False,
        wait_minutes: int = 20,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        """Deploy the local version of ``components`` after snapshotting the org's version."""
        validate_target_org(org)
        specs = validate_components(components, maximum=MAX_DEPLOY_COMPONENTS)
        for spec in specs:
            if ":" not in spec or "*" in spec:
                raise InputValidationError(
                    f"{spec!r} is not a single component.",
                    hint="Deploys name each component exactly, for example ApexClass:CaseService.",
                )
        reason = _require_reason(reason)
        if test_level not in DEPLOY_TEST_LEVELS:
            raise InputValidationError(f"test_level must be one of {DEPLOY_TEST_LEVELS}.")
        tests = list(tests or [])
        for test in tests:
            if not isinstance(test, str) or not _TEST_NAME_RE.match(test):
                raise InputValidationError(f"Invalid test name: {test!r}.")
        if test_level == "RunSpecifiedTests" and not tests:
            raise InputValidationError("RunSpecifiedTests needs at least one test class in 'tests'.")
        if tests and test_level != "RunSpecifiedTests":
            raise InputValidationError("'tests' is only used with test_level RunSpecifiedTests.")
        if not 1 <= int(wait_minutes) <= 120:
            raise InputValidationError("wait_minutes must be between 1 and 120.")
        self.workspace.ticket_dir(ticket_key)

        operation = "validate_component" if validate_only else "deploy_component"
        permit, info = self.guard.authorize_write(
            org, operation, commands=1, ticket_key=ticket_key, approved_by=approved_by
        )
        repo = self.workspace.metadata_repo(ticket_key, api_version=info.api_version)
        change_id = new_change_id()

        try:
            # 1. Commit the proposed local state.
            proposed = repo.commit_all(f"proposed: {change_id} {reason[:100]}") or repo.head()
            assert proposed

            # 2. Snapshot the org's current version of the same components. Without it there is nothing
            #    to roll back to and no drift check, so a failed snapshot stops the deploy.
            try:
                before = run_retrieve(self.cli, org, repo.path, specs, 10)
            except (SfCliError, SfCliTimeoutError) as exc:
                self._fail(operation, info, org, ticket_key, approved_by,
                           {"change_id": change_id, "error": f"snapshot failed: {exc.message}"})
                raise DeployFailedError(
                    f"Nothing was deployed: the org's current version could not be retrieved first ({exc.message}).",
                    hint="Without that snapshot the change could not be rolled back, so the deploy stopped. Your "
                    "edit is still in the working tree. " + (exc.hint or ""),
                    details={"change_id": change_id},
                ) from exc
            org_paths = [f["path"] for f in before["files"] if f.get("path") and f.get("state") != "Failed"]
            pre = repo.commit_all(f"snapshot: org state before {change_id}") or repo.head()
            assert pre
            in_org = {_spec(f) for f in before["files"] if f.get("state") != "Failed"}
            new_specs = [s for s in specs if s not in in_org]
            modified = [p for status, p in repo.changed_files(pre, proposed) if status == "M" and p in org_paths]

            # 3. Drift: has the org changed since the version the edit was based on?
            baselines = load_baselines(repo.path)
            drifted = [p for p in org_paths if baselines.get(p) and repo.differs(baselines[p], pre, [p])]
            unbased = [p for p in modified if not baselines.get(p)]
            if drifted or unbased:
                drift_diff = "".join(repo.diff(baselines[p], pre, [p]) for p in drifted)
                repo.checkout_paths(proposed, modified)  # keep the user's edits in the working tree
                set_baselines(repo.path, org_paths, pre)
                detail = {"change_id": change_id, "drifted": drifted, "never_retrieved": unbased}
                self._fail(operation, info, org, ticket_key, approved_by, detail)
                raise DriftError(
                    "Nothing was deployed: the org's version of "
                    f"{', '.join(drifted + unbased)} is not the version this edit was based on.",
                    hint="The org's current version is now committed in the ticket repository and your edit is "
                    "still in the working tree. Review the difference, re-apply the fix on top of the current "
                    "version if needed, then deploy again.",
                    details={**detail, "org_changes": _truncate(drift_diff, 8000)},
                )
            set_baselines(repo.path, org_paths, pre)

            if not modified and not new_specs:
                self._fail(operation, info, org, ticket_key, approved_by,
                           {"change_id": change_id, "reason": "nothing to deploy"})
                raise ChangeError(
                    "Nothing to deploy: the org already matches the local source for these components.",
                    hint="Edit the files under tickets/<KEY>/metadata/ first, or check the component names.",
                )

            # 4. Deploy the proposed content.
            repo.checkout_paths(proposed, modified)
            unfinished = {"components": specs, "modified_paths": modified, "new_components": new_specs,
                          "changed_components": [s for s in specs if s not in new_specs]}
            try:
                deployed = self._run_deploy(org, repo, specs, permit, test_level=test_level, tests=tests,
                                            validate_only=validate_only, wait_minutes=int(wait_minutes))
            except (SfCliError, SfCliTimeoutError) as exc:
                if not validate_only and _deploy_still_running(exc):
                    raise self._deploy_in_progress(change_id, ticket_key, org, info, operation, pre, proposed, None,
                                                   reason, approved_by, {**unfinished, "error": exc.message},
                                                   int(wait_minutes)) from exc
                self._save_deploy_change(change_id, ticket_key, org, info, STATUS_FAILED, pre, proposed, None, None,
                                         reason, approved_by, {"components": specs, "error": exc.message})
                self._fail(operation, info, org, ticket_key, approved_by, {"change_id": change_id, "error": exc.message})
                raise DeployFailedError(
                    f"The {'validation' if validate_only else 'deploy'} did not complete: {exc.message}",
                    hint="Your edit is still in the working tree. " + (exc.hint or ""),
                    details={"change_id": change_id},
                ) from exc

            if deployed["in_progress"] and not validate_only:
                raise self._deploy_in_progress(change_id, ticket_key, org, info, operation, pre, proposed,
                                               deployed["deploy_id"], reason, approved_by,
                                               {**unfinished, "deploy_status": deployed["status"]}, int(wait_minutes))
            proposed_diff = _truncate(repo.diff(pre, proposed, modified)) if modified else ""
            if not deployed["success"]:
                detail = {
                    "components": specs,
                    "component_failures": deployed["component_failures"],
                    "test_failures": deployed["test_failures"],
                    "deploy_status": deployed["status"],
                }
                self._save_deploy_change(change_id, ticket_key, org, info, STATUS_FAILED, pre, proposed, None,
                                         deployed["deploy_id"], reason, approved_by, detail)
                self._fail(operation, info, org, ticket_key, approved_by, {"change_id": change_id, **detail})
                raise DeployFailedError(
                    f"The {'validation' if validate_only else 'deploy'} failed with status "
                    f"{deployed['status']!r}. The org was not changed.",
                    hint="Fix the reported problems in the local files and try again. Your edit is still in the "
                    "working tree.",
                    details={"change_id": change_id, **detail},
                )

            if validate_only:
                detail = {"components": specs, "tests_run": deployed["tests_run"]}
                self._save_deploy_change(change_id, ticket_key, org, info, STATUS_VALIDATED, pre, proposed, None,
                                         deployed["deploy_id"], reason, approved_by, detail)
                self.audit.record(operation, OUTCOME_SUCCEEDED, org_target=org, org_id=info.org_id,
                                  classification=info.classification, ticket_key=ticket_key,
                                  approved_by=approved_by, detail={"change_id": change_id, **detail})
                return {
                    "change_id": change_id,
                    "status": STATUS_VALIDATED,
                    "org": org,
                    "classification": info.classification,
                    "components": specs,
                    "deploy_id": deployed["deploy_id"],
                    "tests_run": deployed["tests_run"],
                    "diff": proposed_diff,
                    "note": "Validation only: the org was not changed.",
                }

            # 5. Verify: what the org holds now should be what was deployed.
            after = run_retrieve(self.cli, org, repo.path, specs, 10)
            after_paths = [f["path"] for f in after["files"] if f.get("path") and f.get("state") != "Failed"]
            deployed_paths = [f["path"] for f in deployed["files"] if f.get("path")]
            compare = sorted(set(modified) | {p for p in deployed_paths if repo.exists_in(proposed, p)})
            mismatched = [p for p in compare if repo.differs(proposed, None, [p])]
            post = repo.commit_all(f"deployed: {change_id} org state after deploy") or repo.head()
            assert post
            set_baselines(repo.path, sorted(set(after_paths) | set(compare)), post)

            detail = {
                "components": specs,
                "deployed": deployed["files"],
                "modified_paths": modified,
                "created": [f for f in deployed["files"] if f.get("state") == "Created"],
                "verified": not mismatched,
                "mismatched_paths": mismatched,
                "test_level": test_level,
                "tests_run": deployed["tests_run"],
            }
            self._save_deploy_change(change_id, ticket_key, org, info, STATUS_DEPLOYED, pre, proposed, post,
                                     deployed["deploy_id"], reason, approved_by, detail)
            self.audit.record(operation, OUTCOME_SUCCEEDED, org_target=org, org_id=info.org_id,
                              classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                              detail={"change_id": change_id, "components": specs, "verified": not mismatched})
            out: dict[str, Any] = {
                "change_id": change_id,
                "status": STATUS_DEPLOYED,
                "org": org,
                "classification": info.classification,
                "components": specs,
                "deploy_id": deployed["deploy_id"],
                "deployed_files": deployed["files"],
                "pre_commit": pre,
                "post_commit": post,
                "verified": not mismatched,
                "tests_run": deployed["tests_run"],
                "diff": proposed_diff,
                "rollback": f"rollback_change(change_id='{change_id}') restores the snapshot taken before this deploy.",
            }
            if mismatched:
                out["verify_note"] = (
                    "The deploy succeeded, but the org's stored form of these files differs from the local "
                    "source. Salesforce normalizes some XML on save, so this is not necessarily a problem; "
                    "review the diff."
                )
                out["verify_diff"] = _truncate(repo.diff(proposed, post, mismatched), 8000)
            return out
        except SfInvestigatorError:
            raise
        except Exception as exc:  # noqa: BLE001 - make sure unexpected failures are audited
            self._fail(operation, info, org, ticket_key, approved_by, {"change_id": change_id, "error": repr(exc)})
            raise ChangeError(f"Unexpected error during {operation}: {exc}", details={"change_id": change_id}) from exc

    def _deploy_in_progress(self, change_id: str, ticket_key: str, org: str, info: OrgInfo, operation: str,
                            pre: str, proposed: str, deploy_id: str | None, reason: str, approved_by: str | None,
                            detail: dict[str, Any], wait_minutes: int) -> DeployInProgressError:
        """Record a deploy that may still change the org; it is never recorded as failed."""
        self._save_deploy_change(change_id, ticket_key, org, info, STATUS_IN_PROGRESS, pre, proposed, None,
                                 deploy_id, reason, approved_by, detail)
        self.audit.record(operation, OUTCOME_IN_PROGRESS, org_target=org, org_id=info.org_id,
                          classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                          detail={"change_id": change_id, **detail})
        return DeployInProgressError(
            f"The deploy was still running in the org when the wait of {wait_minutes} minute(s) ran out, so it may "
            f"still change the org. It is recorded as change {change_id} with the status in_progress.",
            hint="Do not deploy again yet: check Setup > Deployment Status in the org. Once the deploy has finished, "
            f"rollback_change(change_id='{change_id}') can undo it; it first retrieves the components to see what "
            "the deploy did. Your edit is still in the working tree.",
            details={"change_id": change_id, "status": STATUS_IN_PROGRESS,
                     **({"deploy_id": deploy_id} if deploy_id else {})},
        )

    def _save_deploy_change(self, change_id: str, ticket_key: str, org: str, info: OrgInfo, status: str,
                            pre: str | None, proposed: str | None, post: str | None, deploy_id: str | None,
                            reason: str, approved_by: str | None, detail: Any) -> None:
        self.audit.save_change(
            change_id=change_id, kind=KIND_DEPLOY, ticket_key=ticket_key, org_target=org, org_id=info.org_id or "",
            status=status, pre_commit=pre, proposed_commit=proposed, post_commit=post, deploy_id=deploy_id,
            reason=reason, approved_by=approved_by, detail=detail,
        )

    # ------------------------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------------------------

    def rollback_change(self, change_id: str, *, reason: str, force: bool = False,
                        approved_by: str | None = None) -> dict[str, Any]:
        """Undo a deployed change or a record update using the snapshot taken before it."""
        if not isinstance(change_id, str) or not re.match(r"^CHG-[0-9A-Za-z\-]{6,40}\Z", change_id):
            raise InputValidationError(f"Invalid change Id: {change_id!r}.")
        reason = _require_reason(reason)
        change = self.audit.get_change(change_id)
        if change is None:
            raise ChangeError(f"No change with Id {change_id}.", hint="Use list_changes to see recorded changes.")
        if change["kind"] == KIND_RECORDS:
            return self._rollback_records(change, reason, approved_by)
        if change["status"] not in (STATUS_DEPLOYED, STATUS_ROLLBACK_FAILED, STATUS_IN_PROGRESS):
            raise ChangeError(
                f"Change {change_id} has status {change['status']!r}; only deployed changes (and deploys still "
                "recorded as in progress, or whose rollback failed) can be rolled back.",
                hint="Failed and validation-only changes never modified the org.",
            )

        org, ticket_key = change["org_target"], change["ticket_key"]
        unfinished = change["status"] == STATUS_IN_PROGRESS
        if unfinished:
            # What the deploy did is not known yet: it is read from the org once the org is confirmed.
            planned = change.get("detail") or {}
            commands = (1 if planned.get("modified_paths") else 0) + (1 if planned.get("new_components") else 0)
            if not commands:
                raise ChangeError(f"Change {change_id} recorded no modified or new components; nothing to undo.")
        else:
            changed_specs, created_specs, changed_paths, created_paths = _rollback_plan(change)
            commands = (1 if changed_specs else 0) + (1 if created_specs else 0)
        permit, info = self.guard.authorize_write(
            org, "rollback_change", commands=commands, ticket_key=ticket_key, approved_by=approved_by
        )
        if (info.org_id or "") != change["org_id"]:
            self._fail("rollback_change", info, org, ticket_key, approved_by,
                       {"change_id": change_id, "error": "org id mismatch"})
            raise ChangeError(
                f"{org!r} now points at org {info.org_id}, but the change was made in org {change['org_id']}.",
                hint="Re-authenticate the original org under this alias before rolling back.",
            )
        repo = self.workspace.metadata_repo(ticket_key)
        settled = not unfinished
        try:
            repo.commit_all(f"local: edits saved before rollback of {change_id}")
            if unfinished:
                change = self._settle_unfinished_deploy(change, repo, info, force, approved_by)
                settled = True
                changed_specs, created_specs, changed_paths, created_paths = _rollback_plan(change)
            pre, post = change["pre_commit"], change["post_commit"]
            all_specs = list(dict.fromkeys(changed_specs + created_specs))
            run_retrieve(self.cli, org, repo.path, all_specs, 10)
            current = repo.commit_all(f"snapshot: org state before rollback of {change_id}") or repo.head()
            assert current and pre and post
            tracked = changed_paths + created_paths
            drifted = [p for p in tracked if repo.differs(post, current, [p])]
            if drifted and not force:
                self._fail("rollback_change", info, org, ticket_key, approved_by,
                           {"change_id": change_id, "drifted": drifted})
                raise DriftError(
                    f"Nothing was rolled back: {', '.join(drifted)} changed in the org after this deploy, "
                    "so restoring the snapshot would also discard that later change.",
                    hint="Review the difference. To restore the snapshot anyway, call rollback_change with force=true.",
                    details={"change_id": change_id, "drifted": drifted,
                             "org_changes": _truncate(repo.diff(post, current, drifted), 8000)},
                )

            results: dict[str, Any] = {}
            if changed_specs:
                repo.checkout_paths(pre, changed_paths)
                restored = self._run_deploy(org, repo, changed_specs, permit, test_level="NoTestRun", tests=(),
                                            validate_only=False, wait_minutes=20)
                results["restore"] = {k: restored[k] for k in ("deploy_id", "status", "component_failures")}
                if not restored["success"]:
                    self.audit.set_change_status(change_id, STATUS_ROLLBACK_FAILED)
                    self._fail("rollback_change", info, org, ticket_key, approved_by,
                               {"change_id": change_id, **results})
                    if restored["in_progress"]:
                        raise DeployInProgressError(
                            "The rollback deploy was still running in the org when the wait ran out.",
                            hint="Check Setup > Deployment Status in the org. If it fails, call rollback_change again.",
                            details={"change_id": change_id, **results},
                        )
                    raise DeployFailedError(
                        f"The rollback deploy failed with status {restored['status']!r}.",
                        hint="The org may still hold the changed version. Review the failures and call "
                        "rollback_change again.",
                        details={"change_id": change_id, **results},
                    )
            if created_specs:
                args = ["project", "delete", "source"]
                for spec in created_specs:
                    args += ["--metadata", spec]
                args += ["--target-org", org, "--no-prompt"]
                self.cli.run(args, permit=permit, cwd=repo.path, timeout=20 * 60)
                for path in created_paths:
                    target = repo.path / path
                    if target.exists():
                        target.unlink()
                results["deleted"] = created_specs

            verified = True
            if changed_specs:
                run_retrieve(self.cli, org, repo.path, changed_specs, 10)
                verified = not any(repo.differs(pre, None, [p]) for p in changed_paths)
            rollback_commit = repo.commit_all(f"rollback: {change_id} {reason[:100]}") or repo.head()
            baselines = load_baselines(repo.path)
            for path in changed_paths:
                baselines[path] = rollback_commit or ""
            for path in created_paths:
                baselines.pop(path, None)
            save_baselines(repo.path, baselines)

            self.audit.set_change_status(change_id, STATUS_ROLLED_BACK)
            self.audit.record("rollback_change", OUTCOME_SUCCEEDED, org_target=org, org_id=info.org_id,
                              classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                              detail={"change_id": change_id, "reason": reason, "verified": verified,
                                      "forced": bool(force and drifted), **results})
            return {
                "change_id": change_id,
                "status": STATUS_ROLLED_BACK,
                "org": org,
                "restored_components": changed_specs,
                "deleted_components": created_specs,
                "verified": verified,
                "rollback_commit": rollback_commit,
                **({"forced_over_drift": drifted} if drifted else {}),
            }
        except (SfCliError, SfCliTimeoutError) as exc:
            if settled:  # an unfinished deploy that could not be read stays in progress
                self.audit.set_change_status(change_id, STATUS_ROLLBACK_FAILED)
            self._fail("rollback_change", info, org, ticket_key, approved_by,
                       {"change_id": change_id, "error": exc.message})
            raise ChangeError(f"The rollback did not complete: {exc.message}",
                              hint=(exc.hint or "") + " rollback_change can be called again for this change.",
                              details={"change_id": change_id}) from exc

    def _settle_unfinished_deploy(self, change: dict[str, Any], repo: GitRepo, info: OrgInfo, force: bool,
                                  approved_by: str | None) -> dict[str, Any]:
        """Find out from the org what a deploy recorded as in progress did, and record it as deployed.

        Deploys are all or nothing: every modified file now holds either the version from before
        (not finished, failed or canceled: nothing to undo) or the deployed one. Anything else means
        the org changed for another reason as well, which only ``force`` restores over.
        """
        change_id, org, ticket_key = change["change_id"], change["org_target"], change["ticket_key"]
        detail = change.get("detail") or {}
        pre, proposed = change["pre_commit"], change["proposed_commit"]
        modified = list(detail.get("modified_paths") or [])
        existing = list(detail.get("changed_components") or [])
        new = list(detail.get("new_components") or [])
        retrieved = run_retrieve(self.cli, org, repo.path, list(dict.fromkeys(existing + new)), 10)
        now = repo.commit_all(f"snapshot: org state after the unfinished deploy {change_id}") or repo.head()
        assert now and pre and proposed
        present = [f for f in retrieved["files"] if f.get("state") != "Failed" and f.get("path")]
        created = [f for f in present if _spec(f) in new]
        untouched = [p for p in modified if not repo.differs(pre, now, [p])]
        if len(untouched) == len(modified) and not created:
            self._fail("rollback_change", info, org, ticket_key, approved_by,
                       {"change_id": change_id, "reason": "the deploy has not changed the org"})
            raise ChangeError(
                f"Nothing was rolled back: the org still holds the version from before change {change_id}, so the "
                "deploy is still running, failed or was canceled.",
                hint="Check Setup > Deployment Status in the org. If the deploy is still running, call rollback_change "
                "again once it has finished; if it failed or was canceled, there is nothing to undo.",
                details={"change_id": change_id, "status": STATUS_IN_PROGRESS},
            )
        unexpected = [p for p in modified if repo.differs(proposed, now, [p])]
        if unexpected and not force:
            self._fail("rollback_change", info, org, ticket_key, approved_by,
                       {"change_id": change_id, "unexpected": unexpected})
            raise DriftError(
                f"Nothing was rolled back: the org's version of {', '.join(unexpected)} is not the version change "
                f"{change_id} deployed, so restoring the snapshot could also discard another change.",
                hint="Review the difference. To restore the snapshot anyway, call rollback_change with force=true.",
                details={"change_id": change_id, "unexpected": unexpected,
                         "org_changes": _truncate(repo.diff(proposed, now, unexpected), 8000)},
            )
        deployed = [{"type": f.get("type"), "name": f.get("name"), "path": f["path"],
                     "state": "Created" if _spec(f) in new else "Changed"}
                    for f in present if _spec(f) in new or (_spec(f) in existing and f["path"] in modified)]
        self._save_deploy_change(change_id, ticket_key, org, info, STATUS_DEPLOYED, pre, proposed, now,
                                 change.get("deploy_id"), change.get("reason") or "", change.get("approved_by"),
                                 {**detail, "deployed": deployed, "read_back_after_the_wait": True})
        settled = self.audit.get_change(change_id)
        assert settled
        return settled

    # ------------------------------------------------------------------------------------
    # Record updates
    # ------------------------------------------------------------------------------------

    def update_records(
        self,
        org: str,
        ticket_key: str,
        sobject: str,
        updates: Sequence[dict[str, Any]],
        *,
        reason: str,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        """Update a few records, capturing the previous values first so they can be restored.

        ``updates`` is a list of ``{"id": "<record id>", "fields": {"Field__c": value}}``.
        """
        validate_target_org(org)
        validate_api_name(sobject, "object API name")
        reason = _require_reason(reason)
        if not isinstance(updates, (list, tuple)) or not updates:
            raise InputValidationError("At least one update is required.")
        if len(updates) > MAX_RECORDS_PER_UPDATE:
            raise InputValidationError(f"At most {MAX_RECORDS_PER_UPDATE} records per call; got {len(updates)}.")
        clean: list[dict[str, Any]] = []
        field_names: list[str] = []
        for update in updates:
            if not isinstance(update, dict):
                raise InputValidationError("Each update must be an object with 'id' and 'fields'.")
            record_id, fields = update.get("id"), update.get("fields")
            if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
                raise InputValidationError(f"Invalid record Id: {record_id!r}.")
            if not isinstance(fields, dict) or not fields:
                raise InputValidationError(f"No fields to update for {record_id}.")
            for name, value in fields.items():
                validate_api_name(name, "field API name")
                if "." in name or name.lower() == "id":
                    raise InputValidationError(f"{name!r} cannot be updated.")
                if value is not None and not isinstance(value, (str, int, float, bool)):
                    raise InputValidationError(f"Value for {name} must be text, a number, true/false or null.")
                if name not in field_names:
                    field_names.append(name)
            clean.append({"id": record_id, "fields": dict(fields)})
        if len({u["id"] for u in clean}) != len(clean):
            raise InputValidationError("Each record may appear only once per call.")
        self.workspace.ticket_dir(ticket_key)

        permit, info = self.guard.authorize_write(
            org, "update_records", commands=len(clean), ticket_key=ticket_key, approved_by=approved_by
        )
        change_id = new_change_id()
        ids = ", ".join(soql_literal(u["id"]) for u in clean)
        select = f"SELECT Id, {', '.join(field_names)} FROM {sobject} WHERE Id IN ({ids}) LIMIT {len(clean)}"
        try:
            before_rows = strip_attributes(run_raw_query(self.cli, org, select))
        except SfCliError as exc:
            self._fail("update_records", info, org, ticket_key, approved_by, {"error": exc.message})
            raise
        before = {str(r.get("Id"))[:15]: r for r in before_rows}
        missing = [u["id"] for u in clean if u["id"][:15] not in before]
        if missing:
            self._fail("update_records", info, org, ticket_key, approved_by, {"missing": missing})
            raise ChangeError(f"Record(s) not found in {sobject}: {', '.join(missing)}. Nothing was updated.")

        stored_updates = [
            {"id": u["id"], "fields": u["fields"],
             "before": {f: before[u["id"][:15]].get(f) for f in u["fields"]}}
            for u in clean
        ]
        base = f"/services/data/v{api_version_of(info.api_version)}/sobjects/{sobject}"
        done: list[str] = []
        failure: SfCliError | None = None
        for update in clean:
            try:
                rest_request(self.cli, org, "PATCH", f"{base}/{update['id']}", update["fields"], permit)
                done.append(update["id"])
            except SfCliError as exc:
                failure = exc
                break

        applied = [u for u in stored_updates if u["id"] in done]
        if applied:
            self.audit.save_change(
                change_id=change_id, kind=KIND_RECORDS, ticket_key=ticket_key, org_target=org,
                org_id=info.org_id or "", status=STATUS_APPLIED, reason=reason, approved_by=approved_by,
                detail={"sobject": sobject, "updates": applied},
            )
        if failure is not None:
            self._fail("update_records", info, org, ticket_key, approved_by,
                       {"change_id": change_id if applied else None, "updated": done, "error": failure.message})
            raise ChangeError(
                f"Update stopped: {failure.message}",
                hint=(f"{len(done)} record(s) were already updated and can be restored with "
                      f"rollback_change(change_id='{change_id}')." if done else "No record was updated."),
                details={"updated": done, "change_id": change_id if applied else None},
            ) from failure

        after_rows = strip_attributes(run_raw_query(self.cli, org, select))
        after = {str(r.get("Id"))[:15]: r for r in after_rows}
        shown = Redactor()
        report = []
        verified = True
        for update in clean:
            key = update["id"][:15]
            entry_fields = {}
            for name, wanted in update["fields"].items():
                now_value = after.get(key, {}).get(name)
                matches = _same_value(now_value, wanted)
                verified = verified and matches
                redact = shown._should_redact(name, name, sobject)
                entry_fields[name] = {
                    "before": "[REDACTED]" if redact and before[key].get(name) is not None else before[key].get(name),
                    "after": "[REDACTED]" if redact and now_value is not None else now_value,
                    "matches_requested_value": matches,
                }
            report.append({"id": update["id"], "fields": entry_fields})
        self.audit.record("update_records", OUTCOME_SUCCEEDED, org_target=org, org_id=info.org_id,
                          classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                          detail={"change_id": change_id, "sobject": sobject, "record_ids": done,
                                  "fields": field_names, "verified": verified})
        out: dict[str, Any] = {
            "change_id": change_id,
            "status": STATUS_APPLIED,
            "org": org,
            "classification": info.classification,
            "sobject": sobject,
            "records": report,
            "verified": verified,
            "rollback": f"rollback_change(change_id='{change_id}') restores the previous values.",
        }
        if not verified:
            out["verify_note"] = (
                "At least one field does not hold the requested value after the update. Automation in the "
                "org (triggers, flows, workflow) may have changed it; that is evidence worth recording."
            )
        return out

    def _rollback_records(self, change: dict[str, Any], reason: str, approved_by: str | None) -> dict[str, Any]:
        change_id = change["change_id"]
        if change["status"] != STATUS_APPLIED:
            raise ChangeError(f"Change {change_id} has status {change['status']!r}; nothing to roll back.")
        org, ticket_key = change["org_target"], change["ticket_key"]
        detail = change.get("detail") or {}
        sobject, updates = detail.get("sobject"), detail.get("updates") or []
        validate_api_name(sobject, "object API name")
        permit, info = self.guard.authorize_write(
            org, "rollback_change", commands=max(1, len(updates)), ticket_key=ticket_key, approved_by=approved_by
        )
        if (info.org_id or "") != change["org_id"]:
            raise ChangeError(
                f"{org!r} now points at org {info.org_id}, but the change was made in org {change['org_id']}."
            )
        base = f"/services/data/v{api_version_of(info.api_version)}/sobjects/{sobject}"
        restored: list[str] = []
        try:
            for update in updates:
                rest_request(self.cli, org, "PATCH", f"{base}/{update['id']}", update["before"], permit)
                restored.append(update["id"])
        except SfCliError as exc:
            self.audit.set_change_status(change_id, STATUS_ROLLBACK_FAILED)
            self._fail("rollback_change", info, org, ticket_key, approved_by,
                       {"change_id": change_id, "restored": restored, "error": exc.message})
            raise ChangeError(f"The rollback stopped: {exc.message}", details={"restored": restored}) from exc
        self.audit.set_change_status(change_id, STATUS_ROLLED_BACK)
        self.audit.record("rollback_change", OUTCOME_SUCCEEDED, org_target=org, org_id=info.org_id,
                          classification=info.classification, ticket_key=ticket_key, approved_by=approved_by,
                          detail={"change_id": change_id, "reason": reason, "record_ids": restored})
        return {"change_id": change_id, "status": STATUS_ROLLED_BACK, "org": org, "sobject": sobject,
                "restored_record_ids": restored,
                "fields": sorted({f for u in updates for f in (u.get("before") or {})})}

    # ------------------------------------------------------------------------------------
    # Anonymous Apex
    # ------------------------------------------------------------------------------------

    def execute_anonymous_apex(
        self,
        org: str,
        ticket_key: str,
        apex: str,
        *,
        reason: str,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        """Run anonymous Apex in a non-production org. The script and its log are kept."""
        validate_target_org(org)
        reason = _require_reason(reason)
        if not isinstance(apex, str) or not apex.strip():
            raise InputValidationError("The Apex script is empty.")
        if len(apex) > MAX_APEX_CHARS:
            raise InputValidationError(f"The Apex script is too long ({MAX_APEX_CHARS} characters at most).")
        self.workspace.ticket_dir(ticket_key)

        permit, info = self.guard.authorize_write(
            org, "execute_anonymous_apex", commands=1, ticket_key=ticket_key, approved_by=approved_by
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        apex_dir = self.workspace.subdir(ticket_key, "apex")
        script = apex_dir / f"{stamp}.apex"
        script.write_text(apex, encoding="utf-8")
        digest = hashlib.sha256(apex.encode("utf-8")).hexdigest()
        ticket_dir = self.workspace.ticket_dir(ticket_key)

        result: dict[str, Any]
        error: str | None = None
        try:
            result = self.cli.run(
                ["apex", "run", "--file", f"apex/{script.name}", "--target-org", org],
                permit=permit, cwd=ticket_dir, timeout=600,
            ) or {}
        except SfCliError as exc:
            result = exc.data if isinstance(exc.data, dict) else {}
            error = exc.message

        log_text = str(result.get("logs") or "")
        log_path = apex_dir / f"{stamp}.log"
        if log_text:
            log_path.write_text(log_text, encoding="utf-8")
        lines = log_text.splitlines()
        interesting = [
            f"{n + 1}: {line[:500]}"
            for n, line in enumerate(lines)
            if any(marker in line for marker in ("|USER_DEBUG|", "|FATAL_ERROR|", "|EXCEPTION_THROWN|",
                                                 "|VALIDATION_FAIL|", "|DML_BEGIN|"))
        ][:200]
        succeeded = error is None and bool(result.get("success", True)) and bool(result.get("compiled", True))
        out: dict[str, Any] = {
            "org": org,
            "classification": info.classification,
            "success": succeeded,
            "compiled": result.get("compiled"),
            "script": f"apex/{script.name}",
            "script_sha256": digest,
            "log": f"apex/{log_path.name}" if log_text else None,
            "log_lines": len(lines),
            "log_excerpt": "\n".join(interesting),
            "note": "Anonymous Apex is not transactional from this tool's point of view: any DML it committed "
                    "cannot be rolled back automatically.",
        }
        if error:
            out["error"] = error
        for key in ("compileProblem", "exceptionMessage", "exceptionStackTrace", "line", "column"):
            if result.get(key) not in (None, "", -1):
                out[key] = result.get(key)
        self.audit.record(
            "execute_anonymous_apex", OUTCOME_SUCCEEDED if succeeded else OUTCOME_FAILED,
            org_target=org, org_id=info.org_id, classification=info.classification, ticket_key=ticket_key,
            approved_by=approved_by,
            detail={"script": out["script"], "sha256": digest, "reason": reason, "error": error},
        )
        return out


_DATETIME_RE = re.compile(r"\A(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+\-]\d{2}:?\d{2})?\Z")


def _as_utc(value: str) -> datetime | None:
    """A Salesforce datetime in any of its spellings (``…Z``, ``….000+0000``, ``+01:00``), in UTC."""
    match = _DATETIME_RE.match(value)
    if not match:
        return None
    day, hours, minutes, seconds, zone = match.groups()
    zone = "+00:00" if zone in (None, "Z") else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
    try:
        return datetime.fromisoformat(f"{day}T{hours}:{minutes}:{seconds or '00'}{zone}").astimezone(timezone.utc)
    except ValueError:
        return None


def _same_value(actual: Any, wanted: Any) -> bool:
    """Does the value read back equal the one written, in the form Salesforce stores it?"""
    if actual == wanted:
        return True
    if actual is None or wanted is None:
        return (actual in (None, "")) and (wanted in (None, ""))
    if isinstance(actual, bool) or isinstance(wanted, bool):
        return str(actual).lower() == str(wanted).lower()
    if isinstance(actual, str) and isinstance(wanted, str):
        if _RECORD_ID_RE.match(actual) and _RECORD_ID_RE.match(wanted) and len(actual) != len(wanted):
            return actual[:15] == wanted[:15]  # a 15-character Id is read back with 18 characters
        when, asked = _as_utc(actual), _as_utc(wanted)
        if when is not None and asked is not None:
            return when == asked  # 2026-03-01T08:00:00Z is read back as 2026-03-01T08:00:00.000+0000
    try:
        return float(actual) == float(wanted)
    except (TypeError, ValueError):
        return str(actual) == str(wanted)
