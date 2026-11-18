# Salesforce Ticket Investigator — Plan

Status: **built.** Phases 1 to 4 are implemented and tested against the simulated org; Phase 5
(Jira) exists as an optional, read-only module that is off by default.

What follows is the plan as it was agreed, kept for reference. Where the build departed from it,
[`DECISIONS.md`](DECISIONS.md) says how and why. The main differences:

- Two modes (`advise`, the default, and `apply`) instead of a single flow; see decision 2.
- Developer Edition orgs are read-only unless enabled (the plan's open question 2); decision 3.
- The simulated org lives in `simulator/`, not under `tests/`; decision 13.
- More tools than the plan lists; decisions 14 and 27.
- The "to verify" marks on CLI commands are resolved: see the tables in VERIFICATION.md for what was
  verified against the installed CLI and what has still not been verified against a live org.
- Sections 2 and 9 describe the situation before the build; the open questions were answered.
- Added after the plan: a local web page (decisions 47 to 50, 55 to 57), an org memory that is
  double-checked against the org (44 to 46, 64), one-click start scripts and a release zip
  (61, 62), and a recorded walk-through ([`WALKTHROUGH.md`](WALKTHROUGH.md)).

## 1. My understanding

You paste a Salesforce bug ticket into Claude Code. I save it in a structured form, summarize it
back with ambiguities, then investigate the connected org through a local MCP server that wraps the
`sf` CLI. I find the root cause with cited evidence, propose the smallest correct fix as a diff,
and — only after you approve each write — apply and verify it in a non-production org. I finish
with `report.md` and a draft comment that you post yourself.

The rules in section 2 of the brief are enforced at three independent layers:

| Layer | What it enforces | Can Claude bypass it? |
|---|---|---|
| `core/` guards (Python) | Production write block, row limits, redaction, path confinement, secret scrubbing | No. It is code, has no override flag, and fails closed. |
| `.claude/settings.json` | Read tools run unprompted; every write tool always asks; direct `sf`/`sfdx` in Bash is denied | Only by you changing the file or running Claude Code with permissions bypassed. |
| `CLAUDE.md` + skills | Procedure, evidence standard, data minimization in reports, nothing posted | Instructional only, which is why the two layers above exist. |

## 2. Facts verified so far, and what is not verified

Verified in this cloud container:

- Python 3.13.18, git 2.43, Node 22, npm and `uv` are present.
- The `sf` CLI is **not installed**, so no CLI flag in this plan has been checked against `--help`
  yet. Every command below is marked *to verify* and will be checked before code is written against it.
- The installed Python MCP SDK is `mcp` 2.3.0. In 2.x, `FastMCP` was renamed to `MCPServer`
  (`from mcp.server.mcpserver import MCPServer`). The server will be written against the 2.x API.
- `pytest` is not installed; it will be added as a dev dependency.
- No Salesforce org is authenticated here, so nothing can be run against a real org from this
  container until you connect one.

## 3. Final structure

```
Salesforce-Project/
├── CLAUDE.md                      purpose, rules, procedure, tool guide
├── README.md                      setup, how to try each phase
├── pyproject.toml                 package metadata, deps (mcp; pytest for dev)
├── .mcp.json                      registers the stdio server "sf-investigator"
├── .gitignore                     tickets/*, audit/, .venv, .sf, caches
├── .claude/
│   ├── settings.json              allow read tools, ask on write tools, deny direct sf/sfdx
│   ├── skills/
│   │   ├── intake-ticket/SKILL.md
│   │   ├── investigate-ticket/SKILL.md
│   │   ├── propose-fix/SKILL.md
│   │   └── write-ticket-comment/SKILL.md
│   └── agents/code-tracer.md      read-only subagent
├── core/                          stdlib-only; no Claude Code or MCP imports
│   ├── errors.py                  typed exceptions
│   ├── sf_cli.py                  subprocess wrapper, command classification, secret scrubbing
│   ├── org_guard.py               org info, classification, write permits
│   ├── workspace.py               tickets/<KEY>/ layout, path confinement
│   ├── metadata.py                retrieve, search, read
│   ├── query.py                   SOQL/Tooling with limits and redaction, describe
│   ├── dependencies.py            MetadataComponentDependency lookups
│   ├── logs.py                    list/fetch logs, trace flags
│   ├── apex_tests.py              run tests, results, coverage
│   ├── changes.py                 snapshot, deploy, verify, rollback, audit log
│   └── prereqs.py                 `python -m core.prereqs`
├── mcp_server/
│   ├── __main__.py                `python -m mcp_server`
│   └── server.py                  thin adapter: one MCP tool per core function
├── tests/                         pytest, mocked sf output, no org needed
│   ├── fake_sf/                   simulated-org shim used by tests and the offline demo
│   └── test_*.py
├── samples/
│   ├── planted-bug/               SFDX project with a deliberately buggy trigger handler
│   └── DEMO-101/ticket.txt        sample ticket text to paste
├── docs/PLAN.md
├── audit/audit.db                 SQLite audit log (gitignored)
└── tickets/<KEY>/                 (gitignored — may hold customer data)
    ├── ticket.md                  structured intake
    ├── attachments/               screenshots and files you drop in
    ├── metadata/                  SFDX project and its own git repository (snapshots)
    ├── queries/                   query outputs, always stored redacted
    ├── logs/                      debug logs
    ├── apex/                      anonymous Apex scripts and their output
    ├── report.md
    └── comment.md                 draft ticket comment
```

Design choices worth your attention:

- **`core/` has no third-party dependencies.** That makes it trivially shareable with the
  Agentforce project and a later web app. The MCP server is a thin adapter over it.
- **`tickets/` is gitignored in this repository.** It can contain customer metadata and data, and
  this repository is pushed to GitHub. Each `tickets/<KEY>/metadata/` is its own local git
  repository, which is what provides diff and rollback.
- **One metadata repository per ticket**, not per org, because the brief places retrieved
  metadata under `tickets/<KEY>/`.

## 4. Safety design

### 4.1 Org classification (`org_guard`)

Queried live from the org on every write: `SELECT Id, Name, IsSandbox, OrganizationType,
InstanceName, TrialExpirationDate FROM Organization LIMIT 1`.

| Classification | Rule | Writes |
|---|---|---|
| `sandbox` | `IsSandbox = true`, not a scratch org | Allowed, each with your approval |
| `scratch` | `IsSandbox = true` and the CLI reports a scratch org | Allowed, each with your approval |
| `developer` | `IsSandbox = false` and `OrganizationType = 'Developer Edition'` | Allowed, each with your approval (**open question 2**) |
| `production` | Everything else, including any failure to classify | **Hard-blocked** |

It fails closed: if the query errors, returns no row, or returns an unknown type, the org is
treated as production. There is no override parameter, environment variable or config switch.
The classification is not cached for write checks, because an alias can be re-pointed.

### 4.2 Write permits (`sf_cli` + `org_guard`)

`sf_cli.run()` classifies every command before running it:

- A fixed allowlist of read commands (`org list`, `org display`, `data query`,
  `sobject describe`, `project retrieve start`, `apex list log`, `apex get log`, …) runs freely.
- **Everything else is treated as a write**, including commands the wrapper has never seen. A
  write only runs when the caller passes a `WritePermit`, which only
  `org_guard.authorize_write(alias, operation)` can issue, and only for a non-production org.
  The permit is bound to the org Id and is single-use.

So a new write path cannot be added to `core/` without going through the guard.

### 4.3 Approval

Approval is Claude Code's permission prompt: every write tool is in the `ask` list, so you see the
tool name and its exact arguments before it runs. Core write functions additionally require an
`approved_by` value that goes into the audit log, so the future web app can plug in its own
approval step without changing `core/`.

Known limitation: if Claude Code is started with permission prompts bypassed, the prompt layer
disappears (the production block does not). If you want protection against that, I can add a
two-step confirm token in `core/` (first call previews, second call executes). It is not in the
plan because it doubles the prompts in normal use.

### 4.4 Other guards

- **No direct CLI use.** `.claude/settings.json` denies `Bash(sf:*)` and `Bash(sfdx:*)`, so all org
  access goes through the guarded server.
- **Secret scrubbing.** `sf org list` and `sf org display` can return access tokens. The wrapper
  strips `accessToken`, `refreshToken`, `sfdxAuthUrl`, `password` and `clientSecret` from every
  result, recursively, before anything is returned or logged.
- **Row limits.** A query without `LIMIT` gets one appended (default 50); a `LIMIT` above the cap
  (200) is rejected with an explanation. Only `SELECT` statements are accepted.
- **Redaction.** Values of fields whose names match personal-data patterns (email, phone, mobile,
  fax, birthdate, street/address, SSN/tax id, first/last name on Contact, Lead and User) are
  replaced with `[REDACTED]`. You can extend the list per call, or name specific fields to reveal
  in chat when the bug is about that value. Files written to `tickets/<KEY>/queries/` are always
  redacted.
- **Path confinement.** `read_file` and `search_metadata` only operate inside `tickets/<KEY>/`.
- **Telemetry and auto-update off** for the CLI subprocess (`SF_DISABLE_TELEMETRY`,
  `SF_AUTOUPDATE_DISABLE`), so tool runs send nothing but the API calls themselves.

### 4.5 Windows handling

On Windows `sf` is `sf.cmd`, which runs through `cmd.exe`, where characters such as `>`, `&`, `|`,
`%` and `"` are interpreted — and SOQL uses `>` and `<`. The wrapper therefore:

- resolves the executable with `shutil.which` and never uses `shell=True`;
- passes free text through files wherever the CLI allows it (SOQL via `--file`, Apex via `--file`,
  record values via a request body file);
- rejects any remaining argument that contains a `cmd.exe` metacharacter, with a clear error.

## 5. Tool definitions

Server name: `sf-investigator`. Tool names appear in Claude Code as
`mcp__sf-investigator__<tool>`. Every tool takes `org` (alias or username) unless noted, and
tools that write files take `ticket_key`.

### Read-only tools (allowed without prompting)

| Tool | Parameters | Returns | CLI used (*to verify*) |
|---|---|---|---|
| `list_orgs` | — | alias, username, org Id, type hints, connection status; secrets stripped | `sf org list --json` |
| `org_info` | `org` | org Id, name, instance, API version, `IsSandbox`, `OrganizationType`, classification, `writes_allowed` | `sf org display`, `sf data query` |
| `retrieve_components` | `org`, `ticket_key`, `components` (e.g. `["ApexTrigger:CaseTrigger", "Flow:Case_Routing"]`, `Type:*` for a whole type) | files retrieved, baseline commit hash | `sf project retrieve start --metadata` |
| `search_metadata` | `ticket_key`, `pattern` (regex), `glob`, `max_results` | `file:line: text` matches | none (local) |
| `read_file` | `ticket_key`, `path`, `start_line`, `end_line` | numbered lines | none (local) |
| `soql_query` | `org`, `query`, `ticket_key?`, `redact_fields?`, `reveal_fields?`, `save_as?` | rows (redacted), applied limit, total size | `sf data query --file` |
| `tooling_query` | same as `soql_query` | rows | `sf data query --use-tooling-api --file` |
| `find_dependencies` | `org`, `name`, `type`, `direction` (`uses`, `used_by`, `both`) | two lists of components with types and Ids, plus a coverage warning | Tooling `MetadataComponentDependency` |
| `describe_object` | `org`, `object`, `fields?`, `include` (`fields`, `record_types`, `child_relationships`, `validation_rules`) | condensed describe | `sf sobject describe`, Tooling `ValidationRule` |
| `list_debug_logs` | `org`, `user?`, `since?`, `limit` | log Id, user, operation, status, size, start time | `sf apex list log` |
| `get_debug_log` | `org`, `log_id`, `ticket_key`, `grep?`, `start_line?`, `max_lines` | saved path and the requested excerpt | `sf apex get log --log-id` |
| `run_apex_tests` | `org`, `class_names`, `ticket_key?`, `code_coverage` | per-method outcome, failures with stack traces, coverage | `sf apex run test` |

Notes:

- `find_dependencies` always states its limits: `MetadataComponentDependency` does not track every
  reference type (for example standard fields and dynamic Apex references), so an empty result is
  not proof that nothing depends on a component. The skills pair it with `search_metadata`.
- `get_debug_log` saves the full log to disk and returns only an excerpt, because logs can reach
  20 MB.
- `run_apex_tests` enqueues test jobs in the org. It commits no business data, but it is not a
  pure read. My default is to **block it on production** (**open question 3**).

### Write tools (always ask; hard-blocked on production)

| Tool | Parameters | What it does | CLI used (*to verify*) |
|---|---|---|---|
| `set_trace_flag` | `org`, `user`, `minutes` (default 30, max 1440), `debug_level?` | Creates a trace flag (and a debug level if needed), or extends an existing one | Tooling `TraceFlag`, `DebugLevel` |
| `execute_anonymous_apex` | `org`, `ticket_key`, `apex`, `reason` | Saves the script, runs it, saves and returns the log | `sf apex run --file` |
| `update_records` | `org`, `ticket_key`, `sobject`, `updates` (max 10 records per call), `reason` | Captures the current values of the fields being changed, updates, re-queries to verify | `sf data update record` or `sf api request rest` |
| `deploy_component` | `org`, `ticket_key`, `component`, `test_level`, `tests?`, `validate_only`, `reason` | See the sequence below | `sf project deploy start` |
| `rollback_change` | `change_id`, `reason` | Redeploys the pre-change snapshot (or removes a component the change created), verifies, logs | `sf project deploy start` |

`deploy_component` sequence, all inside `core/changes.py`:

1. Guard: classify the org live; refuse on production.
2. Drift check: re-retrieve the component into a temporary directory and compare it with the
   baseline commit. If the org changed since the baseline, stop and report the difference.
3. Snapshot: commit the pre-change state and the proposed change as two commits in
   `tickets/<KEY>/metadata/`.
4. Deploy the single component, with the requested test level.
5. Verify: deploy status is `Succeeded`, the component is in the success list, and a fresh retrieve
   matches what was deployed.
6. Audit: write a row with change Id, ticket, org Id, alias, classification, component, both commit
   hashes, deploy Id, result, timestamps and `approved_by`.

Every write attempt is audited, including blocked and failed ones.

`deploy_component` deploys one component per call, as the brief says. Some fixes need two
components to land together to compile (a changed method signature and its caller). I propose
allowing up to 5 components in one call for that case, documented as one logical change
(**open question 5**).

## 6. Skills and subagent

| Item | Purpose |
|---|---|
| `intake-ticket` | Writes `tickets/<KEY>/ticket.md` (summary, steps to reproduce, expected vs actual, environment/org, affected records, comment timeline, status), reads attachments, summarizes back, lists ambiguities, asks at most a few questions that change what gets checked. |
| `investigate-ticket` | Scope (org alias and classification) → ranked hypotheses with confirming and refuting evidence → targeted retrieval, `code-tracer`, record queries, logs, dependencies → root cause with an evidence chain, or an explicit statement of what is missing → `report.md`. |
| `propose-fix` | Smallest correct change as a diff, the layer chosen and why, impact analysis from `find_dependencies` and `search_metadata`, risks, test plan. With approval: snapshot, deploy, run tests, re-check records or logs, roll back if not resolved. |
| `write-ticket-comment` | Concise draft in `tickets/<KEY>/comment.md`, record Ids only, no personal data. Never posted. |
| `code-tracer` (subagent) | Read-only tools only. Given an object and event, returns the ordered call chain across triggers, handlers, classes and flows, each step with `file:line`, and marks anything it could not resolve. |

Evidence standard written into `CLAUDE.md` and the skills: every causal claim carries a citation
(`file:line`, a metadata setting, a saved query result, or a log line) and anything without one is
labelled **Hypothesis**.

## 7. Phase plan and acceptance criteria

### Phase 1 — Skeleton and guards

Deliverables: project skeleton, `core/sf_cli`, `core/org_guard`, `core/errors`, `core/prereqs`,
MCP server with `list_orgs` and `org_info`, `.mcp.json`, `.claude/settings.json`, initial
`CLAUDE.md`, README.

Acceptance:

- `python -m core.prereqs` reports Python, `sf`, git, the `mcp` package and authenticated orgs,
  with a non-zero exit code when something required is missing.
- `pytest` passes without an org. Tests cover: JSON parsing including noisy output, timeouts,
  missing CLI, CLI error mapping, secret scrubbing, Windows argument rejection, every
  classification row in 4.1 including fail-closed cases, and a write command refused without a permit.
- The MCP server starts over stdio and lists its tools (checked with an MCP client in a test).
- Every CLI command used has been checked against the installed `--help`, and the README lists
  what was verified.

### Phase 2 — Read-only investigation tools

Deliverables: `core/workspace`, `metadata`, `query`, `dependencies`, `logs` (read side), all
read-only tools except `run_apex_tests`, the `intake-ticket` skill.

Acceptance:

- Tests cover limit enforcement and rejection, non-`SELECT` rejection, redaction (default, extended,
  revealed, always-redacted on disk), path confinement, retrieve into a per-ticket git repository
  with a baseline commit, search results with correct line numbers, dependency direction, and
  log excerpting.
- Against the simulated org: retrieve → search → read → query → dependencies → logs all work
  through the MCP server.
- Pasting the sample ticket produces a structured `ticket.md`, a summary and an ambiguity list.

### Phase 3 — Investigation, tracing and reporting

Deliverables: `core/apex_tests`, `run_apex_tests`, the `investigate-ticket` skill, the
`code-tracer` subagent, the report template, `samples/planted-bug/` and the sample ticket.

Acceptance:

- The sample ticket is investigated end to end and `report.md` names the planted bug with a
  `file:line` citation, a query result and a failing test as evidence, with every uncited
  statement labelled as a hypothesis.
- The report contains no personal data (checked by a test against the redaction patterns).

The brief asks for this demonstration against a Developer Edition org. No org is reachable from
this container, so I can build and test this phase against the simulated org, and the real-org run
happens once an org is connected (**open question 4**).

### Phase 4 — Write tools and apply-and-verify

Deliverables: `core/changes`, trace flag support in `logs`, all five write tools, the SQLite audit
log, the `propose-fix` and `write-ticket-comment` skills.

Acceptance:

- Tests prove that each write tool raises `ProductionWriteBlocked` on a production-classified org
  and on an unclassifiable org, and that the CLI is never invoked in those cases.
- Tests cover: snapshot commits, drift detection, deploy verification, rollback of a modified
  component, rollback of a newly created component, the 10-record cap, and an audit row for
  successful, failed and blocked attempts.
- Against the simulated org: propose the fix for the planted bug, deploy, run tests, verify, then
  roll back and confirm the original state is restored.
- `.claude/settings.json` lists all five write tools under `ask` and none under `allow`.

### Phase 5 — Jira (not started until you say so)

Read-only. Jira Cloud through Atlassian's official MCP server, or Server/Data Center through a
small REST wrapper with a personal access token. Comments are drafted for you and never posted.

## 8. Dependencies

| Dependency | Use | Cost |
|---|---|---|
| Salesforce CLI (`@salesforce/cli`) | All org access | Free |
| `mcp` Python SDK 2.x | MCP server | Free, MIT |
| `pytest` | Tests | Free, MIT |
| git | Snapshots and rollback | Free |

No paid dependency is planned.

## 9. Open questions

1. **Stopping points.** Your message says to build end to end; the brief says to stop after this
   plan and after every phase. Which do you want?
2. **Developer Edition orgs.** They report `IsSandbox = false`. I propose classifying
   `OrganizationType = 'Developer Edition'` as non-production so the planted-bug demonstration and
   apply-and-verify can run there. The alternative is to treat them as production, which makes
   the tool read-only on them.
3. **Apex tests on production.** Block `run_apex_tests` on production (my default, the strict
   reading of "only read and query"), or allow it because test data is rolled back?
4. **CLI and org in this container.** The `sf` CLI is not installed here and no org is
   authenticated. I would install the CLI with npm to verify flags against `--help`. A real-org
   demonstration needs an org you connect, or needs to run on your machine.
5. **Multi-component deploys.** Allow up to 5 components in one `deploy_component` call when a fix
   cannot compile otherwise, or keep it strictly to one?
6. **Your local operating system.** `.mcp.json` will launch `${SFTI_PYTHON:-python}`. That default
   suits Windows; on macOS or Linux you would set `SFTI_PYTHON=python3`. Tell me if you want a
   different default.
7. **Package name.** The brief names the package `core/`. That is a generic import name and may
   collide when shared with the Agentforce project. I will keep `core/` unless you prefer a
   distinct name such as `sfcore/`.
