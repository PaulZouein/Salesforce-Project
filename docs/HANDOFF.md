# Build handoff

Written by the session that built the first half of this project, for the session that
finishes it. Read this file, `docs/PLAN.md` and `CLAUDE.md` before touching anything.
When the build is complete, turn this file into `docs/DECISIONS.md` (see the last section).

## Why the build moved to a new session

The first session committed `.claude/settings.json` with a PreToolUse hook pointing at
`.claude/hooks/guard.py` one step before creating that script. Claude Code applied the new
settings immediately. Python exits with code 2 when a script file is missing, exit code 2 from
a PreToolUse hook blocks the tool call, and the same settings denied writes to `.claude/hooks/`.
Bash, Edit and Write were therefore all blocked in that container. Rather than route around its
own deny rule, the first session pushed the fix through the GitHub connector and handed over.

Consequences you must respect:

- `.claude/settings.json` on the branch currently has **no hook and no edit-protection rules**
  so that you can build freely. The hardened settings are produced by
  `python scripts/generate_settings.py` and committed as the **last step** of the build.
- The hook command now starts with `[ -f …/guard.py ] || exit 0`, and `guard.py` exits 0 on any
  internal error. Keep both properties, and keep a test for them.
- Once the hardened settings are on disk, the hook and deny rules apply to you too: you will not
  be able to edit `sfti.config.json`, `.mcp.json`, `.claude/settings.json` or `.claude/hooks/`
  with Edit, Write or Bash. Finish everything else first.

## What the user asked for

The full brief is quoted in `docs/PLAN.md` section 1 and encoded in `CLAUDE.md`. Decisions the
user made after reading the plan:

1. Build phases 1 to 4 straight through, no stops between phases.
2. **Two modes.** Default `advise`: find the root cause and tell the user what to change ("add
   this method in Apex class X"); the user implements. Second mode `apply`: with approval, deploy
   and test repeatedly until it works. Build `apply` after everything else, as an upgrade.
   "The most important thing is to find the root cause."
3. `run_apex_tests` is blocked on production by default; an opt-in to allow it is a feature to
   build at the end.
4. Install the Salesforce CLI in the container to verify flags (done, see below).
5. Later message: the user is away until tomorrow. Work alone, decide alone, add features and
   enhancements without asking, "make it wow", and **report the decisions taken** when done.

That autonomy does not extend to weakening the rules in section 2 of the brief (the eight rules
at the top of `CLAUDE.md`). Phase 5 (Jira) was "only when I say so"; under the later message it
may be built as an **opt-in, read-only, inactive-by-default** module, clearly labelled.

Never open a pull request. Push only to `claude/salesforce-ticket-investigator-zeepyr`.

## Decisions already taken (carry these into DECISIONS.md)

| # | Decision | Reason |
|---|---|---|
| 1 | `core/` uses only the standard library; the MCP server is a thin adapter over `core.app.Investigator`. | Reuse by the Agentforce project and a future web app. |
| 2 | Three independent gates on writes: org classification (hard), mode (`advise`/`apply`), Claude Code approval prompt. | Rules 5 and 6. |
| 3 | Developer Edition orgs are classified `developer` and are **read-only unless** the user sets `allow_developer_edition_writes`. | The user did not answer that question directly, so the safe default was chosen and an opt-in added. |
| 4 | `set_trace_flag` is a diagnostic write: allowed in advise mode (non-production, with approval). Deploys, record updates, anonymous Apex and rollback need `apply`. | Root-cause finding is the priority; trace flags serve it. |
| 5 | Apex tests on production: refused by `run_apex_tests`; a separate always-ask tool `run_apex_tests_on_production` works only with `allow_apex_tests_on_production: true`. | User's answer 3. |
| 6 | Every `sf` command is classified before it runs; anything not on a read allowlist needs a single-org, limited-use permit that only `org_guard` can issue. Unknown commands count as writes. | Makes the production block structural, not a convention. |
| 7 | Record updates and trace flags go through `sf api request rest` with the body in a file. | Arbitrary values are unsafe on a `cmd.exe` command line (Windows). |
| 8 | Queries are passed with `--file`, never `--query`. | Same Windows reason; SOQL contains `<` and `>`. |
| 9 | `tickets/` is gitignored here; each `tickets/<KEY>/metadata/` is its own git repository. Baselines (the commit at which each file last matched the org) live in `.git/sfti-baseline.json`. | Customer data must not reach GitHub; drift detection needs per-file baselines. |
| 10 | Deploys take up to 5 components per call (one logical change). | A fix and its test class must sometimes land together. |
| 11 | Debug logs are listed by querying `ApexLog` through the Tooling API, returning user Ids rather than names. | Data minimization. |
| 12 | `mcp` SDK 2.x is supported (`MCPServer`), with a fallback import for 1.x (`FastMCP`). Tools return JSON text. | The container had 2.3.0; users may have either. |
| 13 | A simulated org (`simulator/`) backs the tests and the offline demo. It does not execute Apex; test outcomes come from rules in `tests.json`. | No org is reachable from the build container. |
| 14 | Extra tools beyond the brief: `get_config`, `init_ticket`, `list_tickets`, `search_tickets`, `list_files`, `summarize_flow`, `scan_apex`, `workspace_diff`, `recent_org_changes`, `record_history`, `analyze_debug_log`, `list_changes`, `audit_log`, `write_template`, `validate_report`, `validate_comment`, `export_report_html`. | Each shortens root-cause work or enforces a rule mechanically. |
| 15 | `validate_report` checks that every citation resolves to a real file and line in the ticket folder, that a confirmed root cause cites evidence, and that the report holds no email addresses. | Rule 1 and rule 7 enforced by a tool, not only by instructions. |
| 16 | A PreToolUse hook blocks direct `sf`/`sfdx`, direct calls to core write functions from Bash, and edits to the safety files. It is heuristic defense in depth; the hard guarantee is in `core/org_guard.py`. | Rules 5 and 6 when permission prompts are bypassed. |

## What exists

```
core/errors.py         typed errors with code + hint
core/config.py         sfti.config.json loader; fails closed to safe defaults
core/sf_cli.py         subprocess wrapper, read/test/write classification, permits, secret scrubbing, Windows rules
core/audit.py          SQLite audit_log + changes tables
core/org_guard.py      OrgInfo, classify_org, authorize_write, authorize_test_run
core/gitrepo.py        git helper (fixed identity, no global config needed)
core/workspace.py      tickets/<KEY>/ layout, path confinement, metadata repo creation
core/query.py          enforce_limit, Redactor, run_raw_query, run_query, describe_object, object_automation
core/metadata.py       retrieve_components, baselines, search_metadata, read_file, list_files
core/dependencies.py   find_dependencies
core/logs.py           list_debug_logs, get_debug_log, rest_request, set_trace_flag
core/apex_tests.py     run_apex_tests
core/changes.py        ChangeManager: workspace_diff, deploy_component, rollback_change, update_records, execute_anonymous_apex
core/log_analysis.py   analyze_log
core/flow_summary.py   summarize_flow_xml
core/apex_scan.py      scan_apex_source, scan_workspace
core/timeline.py       recent_org_changes, record_history
core/report.py         templates, init_ticket, validate_report, validate_comment, export_report_html, search_tickets
core/app.py            Investigator facade
core/prereqs.py        python -m core.prereqs
mcp_server/server.py   35 tools; READ_ONLY_TOOLS and WRITE_TOOLS lists drive the settings generator
simulator/fake_sf.py   stand-in for the sf executable; simulator/build.py builds simulator/state/
samples/planted-bug/   SFDX project with the planted bug + org-data (records, tests.json, a debug log, describes)
samples/DEMO-101/ticket.txt   the sample ticket to paste
.claude/               settings.json, hooks/guard.py, skills (4), agents/code-tracer.md
scripts/generate_settings.py
CLAUDE.md, sfti.config.json, .mcp.json, pyproject.toml, docs/PLAN.md
```

There are **no tests yet** and the README is a stub.

### Smoke-tested by hand in the first session

- `python -m core.prereqs` against the real CLI (no org) and against the simulator.
- The MCP server over stdio: lists 35 tools, `get_config` and `org_info` work, CLI errors come back as `{"ok": false, "error": …}`.
- `org_info` on `demo-prod` classifies production; a relationship query through the simulator returns redacted rows.
- `summarize_flow_xml` and `scan_apex_source` on the sample (the scan flags `return_inside_loop` at line 30 of `CaseEscalationService.cls`, which is the planted bug).

### Never executed (expect bugs here)

`core/changes.py` end to end, `set_trace_flag`, `get_debug_log`, `analyze_log`, `recent_org_changes`,
`record_history`, `find_dependencies`, `describe_object` with automation, `run_apex_tests`,
`workspace_diff`, everything in `core/report.py`, the simulator's deploy, delete, REST and test
commands, `scripts/generate_settings.py`, the reworked `guard.py`.

## Verified against the installed CLI (@salesforce/cli 2.158.7)

Checked with `--help` and by reading the plugin sources under
`node_modules/@salesforce/cli/node_modules/@salesforce/`:

| Command | Flags used | Notes verified |
|---|---|---|
| `org list` | `--json` | result groups: `other`, `sandboxes`, `nonScratchOrgs`, `devHubs`, `scratchOrgs` |
| `org display` | `--target-org` | result has `accessToken` (scrubbed), `id`, `apiVersion`, `instanceUrl`, `username`, `alias` |
| `data query` | `--file`, `--target-org`, `--use-tooling-api` | |
| `sobject describe` | `--sobject`, `--target-org` | |
| `project retrieve start` | `--metadata` (repeatable), `--target-org`, `--wait`, `--ignore-conflicts` | result `files[]` with `fullName`, `type`, `state`, `filePath` |
| `project deploy start` | `--metadata`, `--target-org`, `--test-level`, `--tests`, `--wait`, `--dry-run`, `--ignore-conflicts` | process exit code set from deploy status |
| `project delete source` | `--metadata`, `--target-org`, `--no-prompt` | |
| `apex run test` | `--class-names` (repeatable), `--result-format json`, `--code-coverage`, `--wait`, `--target-org` | exit code 100 on test failures |
| `apex get log` | `--log-id`, `--output-dir`, `--target-org` | with `--output-dir` the JSON result is a list of file paths |
| `apex run` | `--file`, `--target-org` | compile/runtime failure throws with the result in `data` |
| `api request rest` | `<url>`, `--method`, `--body @file`, `--target-org` | result `{statusCode, headers, body}`; exit code 1 when HTTP >= 400 |

Error envelope (captured live): `{"name", "message", "exitCode", "status", "stack", …}` on stdout.

### Not verified against a live org (list these in the README)

- `Organization.IsSandbox` is `true` for scratch orgs.
- `FlowDefinitionView` fields and filtering by `TriggerObjectOrEventId`.
- `ApexTrigger.TableEnumOrId` holds the object's durable Id for custom objects.
- `MetadataComponentDependency` accepting `ORDER BY` and `LIMIT`. **Remove the `ORDER BY` from
  `core/dependencies.py` and sort in Python**: this object has restricted query support and the
  sort is not worth the risk.
- `SetupAuditTrail` fields (`Action`, `Section`, `Display`, `DelegateUser`).
- `ApexLog.LogUserId` through the Tooling API.
- History object naming in `record_history`.
- The JSON envelope of a failed deploy (`status: 1` with `result`), of `project delete source`,
  and of `api request rest` for 201/204 responses. These come from reading the plugin source.
- The hook command under Windows shells.

Every section of `object_automation` and `recent_org_changes` is wrapped so that one failing
query reports an error in place instead of failing the call.

## Environment setup for the new container

```bash
pip install pytest            # mcp 2.x is already in the image
python -m simulator setup     # builds simulator/state (gitignored), prints the launcher path
export SFTI_SF_EXECUTABLE="$PWD/simulator/state/bin/sf"
python -m core.prereqs
```

The real CLI is not needed to finish the build. Do not run `sf` from Bash: it is denied by
the settings, and all verification against it is already recorded above.

## Remaining work, in order

### 1. Test suite (`tests/`), all passing without an org

Use the simulator through the real subprocess path for integration tests, and an injected
`runner` for `SfCli` unit tests. Suggested fixtures in `tests/conftest.py`: a simulator state in
`tmp_path` with orgs `sandbox`, `prod`, `dev`, `scratch` (isScratch + isSandbox true) and
`broken` (`organizationRecordMissing: true`); a temporary project root with a helper to write
`sfti.config.json`; an `Investigator(root=tmp, cli=SfCli(executable=launcher))`.

Must-have coverage:

- **sf_cli**: JSON parsing with warning lines before the envelope; timeout (injected and real,
  using `SFTI_SIM_DELAY`); missing executable; error mapping and hints; secret scrubbing (the
  simulator returns an `accessToken`: assert it never appears in any tool output); Windows
  metacharacter rejection with `windows=True`; `classify_command` for read, test, write and
  unknown commands; a flag outside the allowlist (`org list --clean`) is refused; a write without
  a permit raises and **the runner is never called**; permits cannot be constructed directly,
  cannot be issued for production, are bound to the target org and run out.
- **org_guard**: every row of the classification table, including fail-closed cases (`None`,
  non-boolean `IsSandbox`, missing Organization row, org query error). `authorize_write`:
  production refused in every combination of settings; Developer Edition refused unless enabled;
  sandbox refused in advise unless `diagnostic=True`; allowed in apply; an audit row for allowed
  and for blocked. `authorize_test_run` matrix.
- **config**: defaults, bad JSON, wrong types, unknown keys, limits out of range.
- **query**: limit added, kept, lowered, rejected above max, subquery limit rejected, `OFFSET`
  placement, non-SELECT and multi-statement rejected, `FOR UPDATE` rejected; redaction default,
  extra, reveal, nested relationship (`Owner.Name` on User), and **saved files stay redacted even
  with `reveal_fields`**; describe and automation.
- **metadata/workspace**: retrieve creates the repo, a commit and baselines; unknown component
  reported; local edits committed before a retrieve; search line numbers; `read_file` ranges;
  path confinement (`..`, absolute path outside, `.git`).
- **logs**: list, get (file saved, excerpt, grep), `analyze_log` on the sample log (trigger
  order, flow interviews, no `STATEMENT_EXECUTE|[32]`); `set_trace_flag` refused on production
  with no write command sent, allowed on sandbox in advise mode, creates the debug level once,
  second call extends.
- **apex_tests**: pass on sandbox; refused on production; allowed there only with the setting
  and `production_confirmed=True`.
- **changes** (apply mode on the sandbox org): the whole planted-bug fix: retrieve, edit
  `return;` to `continue;` at line 30, add the test method `mixedBatchEscalatesEveryPremiumCase`,
  `workspace_diff`, validate-only deploy, real deploy with `RunSpecifiedTests`, verified, tests
  pass, `rollback_change` restores the org source byte for byte. Also: drift detected when the
  org's source changes after the retrieve and the local edit survives; compile failure leaves the
  org unchanged; a new component is created and rollback deletes it; `update_records` cap, before
  values, verification, rollback; `execute_anonymous_apex` allowed in apply, refused in advise;
  every write tool refused on `prod` **with the simulator's state unchanged afterwards**; audit
  rows for succeeded, failed and blocked attempts.
- **analysis**: flow summary, Apex scan (lead at line 30; no false positive on the SOQL
  for-loop idiom), `recent_org_changes`, `record_history`.
- **report**: `init_ticket`; `validate_report` passes a good report and fails on a broken
  citation, a line out of range, a confirmed cause without evidence, an email address; HTML
  export; `search_tickets`.
- **mcp_server**: tool list over stdio; every write tool is in `ask` and none in `allow`; every
  read-only tool is in `allow`; `python scripts/generate_settings.py --check` logic; one tool
  call end to end against the simulator.
- **guard hook**: blocks `sf …`, `cd x && sf …`, `npx @salesforce/cli …`, writes to protected
  files, python one-liners calling `deploy_component`; allows `grep sf README.md`,
  `python -m pytest`, `cat sfti.config.json`, `git add .claude/settings.json`; exits 0 on
  malformed input; the hook command is a no-op when the script is missing.
- **report privacy**: the sample report in `samples/DEMO-101/expected/` passes `validate_report`
  and contains no value from the simulator's `SuppliedEmail` or `User.Name` fields.

Fix whatever the tests expose, in `core/` or in the simulator. If a behaviour was wrong by
design, fix the design and record it as a decision.

### 2. Demonstration on DEMO-101 (Phase 3 acceptance)

Act as the investigator and follow `CLAUDE.md` and the skills against the simulated orgs:
intake from `samples/DEMO-101/ticket.txt`, read from `demo-prod`, reproduce on `demo-sandbox`.
The expected chain:

- query: imported cases for Premium accounts with Priority Medium all come after a
  Standard-account case in the same import batch (same `CreatedDate`, same creator);
- code: `CaseEscalationService.cls:30` returns from the method instead of continuing the loop;
- log `07LWs00000L0001MAA`: statement 30 executes, statement 32 never does, the flow evaluates
  `High_Priority` to false for both cases, so the flow is a consequence, not the cause;
- timeline: the class changed on 9 November (setup audit trail), the first failing import ran
  on 10 November;
- the existing test class passes and has no mixed-batch test;
- fix: `return;` becomes `continue;`, plus the mixed-batch test; existing wrong cases need a
  data correction, proposed separately.

The MCP server registered in `.mcp.json` will not point at the simulator in your session, so
drive the same code through a small stdio MCP client or `core.app.Investigator` directly.
Copy the resulting `ticket.md`, `report.md`, `report.html`, `comment.md` and `fix.diff` to
`samples/DEMO-101/expected/`. State in the report that the org is simulated and Apex was not
executed. Add an end-to-end test that replays the tool sequence.

### 3. Apply-mode upgrade (the user's "second option")

The write tools exist. Prove the loop on `demo-sandbox` with mode `apply` in a temporary project
root (never change the repository's `sfti.config.json` default): validate, deploy, test,
re-query, roll back. Record the transcript for the README.

### 4. README and docs

README: what it is; setup on Windows, macOS and Linux (`SFTI_PYTHON`, `pip install -e .[dev]`,
`python -m core.prereqs`, logging in to an org); first run in Claude Code; "how to try it" for
each phase, with the simulated org as the zero-setup path; the two modes and how to switch; the
safety model (three gates, what the hook does and does not guarantee, bypass-permissions
limitation); tool reference; what was verified against the CLI and what was not verified
against a live org; troubleshooting; reuse of `core/` from other projects.

Update `docs/PLAN.md` with a short status section pointing to the decisions.

### 5. Enhancements, most valuable first

Build what you can do well; skip rather than ship something half-working.

1. Opt-in Apex tests on production (exists: verify and document).
2. GitHub Actions workflow running pytest on Linux and Windows.
3. `scripts/call_tool.py`: call a **read-only** tool from the command line for debugging.
   It must refuse write tools.
4. Evidence index: a tool or function that lists every saved artifact of a ticket (queries,
   logs, test results, commits) with a one-line description, to make the Evidence table easy.
5. Jira, read-only, off by default: a small REST wrapper for Jira Server/Data Center using a
   personal access token from an environment variable, exposing `jira_get_ticket` only;
   documentation for using Atlassian's official MCP server with Jira Cloud. No tool that writes
   to Jira may exist. Label it "Phase 5, optional" everywhere.
6. Anything else that makes root-cause finding faster and is honest about its limits.

### 6. Last step

1. `python scripts/generate_settings.py` (guard on), run the full test suite once more.
2. Rename this file to `docs/DECISIONS.md`: keep the decisions table, add yours, keep the
   "not verified against a live org" list, drop the task list.
3. Commit and push. Do not open a pull request.
4. Send a message to session `session_01XQErRz1JnknhisRyEksK6m` (the first session, which will
   report to the user) with: what was built, test count and result, decisions taken, anything
   left undone and why.

## Things worth knowing about the code

- `Workspace.subdir()` creates the ticket folder if needed, so any tool given a `ticket_key`
  creates `tickets/<KEY>/`.
- `deploy_component` treats a component as new when the pre-deploy retrieve returns no file for
  it; rollback of a created component uses `sf project delete source`.
- `rest_request` accepts envelope statuses 0 and 1 and decides on `statusCode`.
- The simulator's SOQL engine supports single-object SELECT with dotted relationship fields,
  `AND`-joined conditions (`=`, `!=`, comparisons, `IN`, `LIKE`), `ORDER BY`, `LIMIT` and
  `COUNT()`. Extend it when a test needs more; keep it honest about what it cannot parse.
- `mcp_server/server.py` deletes `__wrapped__` and sets `__signature__` on each tool wrapper so
  the SDK sees the parameters but a `str` return type.
- The model identifier must not appear in commits, code or docs.
