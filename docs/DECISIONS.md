# Decisions

Why the Salesforce Ticket Investigator is built the way it is. Decisions 1 to 16 were taken in
the first build session, 17 to 42 in the session that finished the build while the owner was
away, and 43 by the first session after the finishing session reported. The rules in `CLAUDE.md`
were treated as fixed throughout: no decision here relaxes one.

For setup and use, read the [README](../README.md). The original plan is [PLAN.md](PLAN.md).

## What the owner asked for

The brief is quoted in `PLAN.md` section 1 and encoded in `CLAUDE.md`. Decisions the owner made
after reading the plan:

1. Build phases 1 to 4 straight through.
2. **Two modes.** Default `advise`: find the root cause and say what to change; the owner
   implements. Second mode `apply`: with approval, deploy and test repeatedly until it works.
   "The most important thing is to find the root cause."
3. `run_apex_tests` is blocked on production by default; an opt-in to allow it is a feature.
4. Install the Salesforce CLI in the build container to verify flags.
5. Later: work alone, decide alone, add enhancements without asking, and report the decisions.

That autonomy did not extend to the eight rules at the top of `CLAUDE.md`. Phase 5 (Jira) was
"only when I say so"; it was built as an opt-in, read-only module that is inactive by default.

## Decisions from the first build session

| # | Decision | Reason |
|---|---|---|
| 1 | `core/` uses only the standard library; the MCP server is a thin adapter over `core.app.Investigator`. | Reuse by the Agentforce project and a future web app. |
| 2 | Three independent gates on writes: org classification (hard), mode (`advise`/`apply`), Claude Code approval prompt. | Rules 5 and 6. |
| 3 | Developer Edition orgs are classified `developer` and are **read-only unless** the owner sets `allow_developer_edition_writes`. | The owner did not answer that question directly, so the safe default was chosen and an opt-in added. |
| 4 | `set_trace_flag` is a diagnostic write: allowed in advise mode (non-production, with approval). Deploys, record updates, anonymous Apex and rollback need `apply`. | Root-cause finding is the priority; trace flags serve it. |
| 5 | Apex tests on production: refused by `run_apex_tests`; a separate always-ask tool `run_apex_tests_on_production` works only with `allow_apex_tests_on_production: true`. | The owner's answer 3. |
| 6 | Every `sf` command is classified before it runs; anything not on a read allowlist needs a single-org, limited-use permit that only `org_guard` can issue. Unknown commands count as writes. | Makes the production block structural, not a convention. |
| 7 | Record updates and trace flags go through `sf api request rest` with the body in a file. | Arbitrary values are unsafe on a `cmd.exe` command line (Windows). |
| 8 | Queries are passed with `--file`, never `--query`. | Same Windows reason; SOQL contains `<` and `>`. |
| 9 | `tickets/` is gitignored here; each `tickets/<KEY>/metadata/` is its own git repository. Baselines (the commit at which each file last matched the org) live in `.git/sfti-baseline.json`. | Customer data must not reach GitHub; drift detection needs per-file baselines. |
| 10 | Deploys take up to 5 components per call (one logical change). | A fix and its test class must sometimes land together. |
| 11 | Debug logs are listed by querying `ApexLog` through the Tooling API, returning user Ids rather than names. | Data minimization. |
| 12 | `mcp` SDK 2.x is supported (`MCPServer`), with a fallback import for 1.x (`FastMCP`). Tools return JSON text. | The build container had 2.3.0; users may have either. Only 2.3.0 has been run. |
| 13 | A simulated org (`simulator/`) backs the tests and the offline demo. It does not execute Apex; test outcomes come from rules in `tests.json`. | No org is reachable from the build container. |
| 14 | Extra tools beyond the brief: `get_config`, `init_ticket`, `list_tickets`, `search_tickets`, `list_files`, `summarize_flow`, `scan_apex`, `workspace_diff`, `recent_org_changes`, `record_history`, `analyze_debug_log`, `list_changes`, `audit_log`, `write_template`, `validate_report`, `validate_comment`, `export_report_html`. | Each shortens root-cause work or enforces a rule mechanically. |
| 15 | `validate_report` checks that every citation resolves to a real file and line in the ticket folder, that a confirmed root cause cites evidence, and that the report holds no email addresses. | Rule 1 and rule 7 enforced by a tool, not only by instructions. |
| 16 | A PreToolUse hook blocks direct `sf`/`sfdx`, direct calls to core write functions from Bash, and edits to the safety files. It is heuristic defense in depth; the hard guarantee is in `core/org_guard.py`. The hook command starts with `[ -f …/guard.py ] || exit 0` and the script exits 0 on any internal error. | Rules 5 and 6 when permission prompts are bypassed. The first build session locked itself out by committing the hook setting before the script existed; the two exit-0 properties make that impossible, and both are tested. |

## Decisions from the finishing session

### Tests

| # | Decision | Reason |
|---|---|---|
| 17 | Integration tests run the real subprocess path against the simulator; `SfCli` unit tests inject a runner. "Nothing was written" is asserted by hashing every file of a simulated org before and after. | A guard test that only checks for an exception does not prove the org was untouched. The simulator deliberately does not enforce org type, so only `core/org_guard` stands between a test and a "production" write. |
| 18 | The hook script `.claude/hooks/guard.py` was **not modified**, and the test suite describes it as it is. | I intended one correction (see "Open points", item 1). The session's permission layer refused edits to the hook script as a change to an oversight mechanism, and I did not look for another way to make the edit. The known false positive is covered by a strict expected-failure test, so the suite will flag it when the script is fixed. |
| 19 | `.claude/settings.json` was **not regenerated** by the finishing session. It left the unguarded file from the first session in place, with one test failing to report it. **Superseded by decision 43.** | Generating the settings was the planned last step. The session's permission layer refused it, in the unguarded and in the guarded form, for the same reason as 18, and I did not look for another way to write the file. The test `test_committed_settings_match_the_server` was left failing rather than relaxed, because what it reported was true. A second test checks the invariant that held throughout: no write tool is allowed without asking. |
| 20 | A test compares the tool names in `CLAUDE.md` with the server's tool list, both ways. | A tool the assistant is told about must exist, and a tool that exists must be documented. |

### Bugs found by running the code, and their fixes

| # | Decision | Reason |
|---|---|---|
| 21 | `find_dependencies` no longer uses `ORDER BY`; results are sorted in Python. | `MetadataComponentDependency` has restricted query support (carried over from the handoff notes). |
| 22 | `scan_apex` reports an indexed query (`[SELECT …][0]`) once, as `query_indexed_without_check`, not also as `single_row_query`. | It was reported twice under two rules. |
| 23 | `validate_report`: the template's example citation is no longer skipped. A report that still contains it fails. | The skip was dead code (it tested the path's prefix, which is always `metadata/`), and an unreplaced example citation is a broken citation. |
| 24 | `validate_report`: a backticked bare file name without a folder and without a line (`CaseService.cls`) is a mention, not a citation. With a folder or a line number it must resolve. | Writing a class's file name in prose failed the DEMO-101 report three times. A check that cries wolf on normal prose gets ignored. Detection of real broken citations is unchanged, and tested. |
| 25 | `validate_report` warns when a cited source file carries local edits. | After a fix is drafted, `file:line` in the working tree may no longer show what the org holds. In DEMO-101 line 30 is the defect in the org and the fix in the working tree. The report must say which version it cites. |
| 26 | Changes are listed in recording order (`rowid`), not by change Id. | Two changes recorded in the same second were listed in random order. |
| 27 | HTML export: consecutive `>` lines form one block quote; citation columns keep a readable width. | Found by rendering the DEMO-101 report. |
| 28 | Simulator: numbers sort numerically; `OR`, `NOT` and subqueries in `WHERE` are rejected with an error; the simulated stack trace points at the real line of the new test. | It sorted `10` before `7`, and evaluated `a = 'x' OR b = 'y'` silently and wrongly. A simulator that returns a wrong answer quietly would make every test built on it suspect. |

`core/changes.py` (deploy, drift, rollback, record updates, anonymous Apex), `set_trace_flag`,
`get_debug_log`, `run_apex_tests`, `recent_org_changes`, `record_history` and the settings
generator had never been executed before this session. They ran correctly against the simulator
on first execution; the bugs above are the ones the tests exposed.

### Additions

| # | Decision | Reason |
|---|---|---|
| 29 | `analyze_debug_log` also returns the Apex source lines that executed, per method, and how each flow decision evaluated. | "Statement 30 ran and statement 32 never did" and "the flow's condition was false" are the two log facts that settle DEMO-101; they should not need a hand-written grep. It says when the log level was too low for the absence of a line to mean anything. |
| 30 | New read-only tool `evidence_index`: every saved artifact of a ticket with a one-line description and the string to cite. | Makes the Evidence table follow from what is on disk. Descriptions are built from the already-redacted files. |
| 31 | `scripts/call_tool.py` calls read-only tools from the command line and refuses every write tool, with no flag to allow them. | Debugging without Claude Code must not become a way around the approval prompt. |
| 32 | GitHub Actions workflow: the suite on Linux, Windows and macOS, Python 3.11 and 3.13. | The owner works on Windows; the build container is Linux. All six jobs pass since decision 43; before it, the one test described in 19 kept the run red. |
| 33 | Phase 5 Jira reader (`core/jira.py`, tool `jira_get_ticket`): off unless `jira_base_url` is set and `SFTI_JIRA_TOKEN` is exported; https only; one `GET`; redirects not followed; people returned as roles and emails masked; no write function exists, and a test asserts that. Jira Cloud is documentation only (use Atlassian's MCP server). | Allowed by the owner's later message as opt-in, read-only and inactive by default. Roles instead of names because the text ends up in `ticket.md` (rule 7). Not following redirects keeps the token with the configured host. |
| 34 | `jira_base_url` is a new optional key in `sfti.config.json`. The repository's file was not edited; absence means off. | That file is the owner's. |

### The demonstration

| # | Decision | Reason |
|---|---|---|
| 35 | DEMO-101 was investigated in **advise** mode: records, source and the debug log from `demo-prod`; the existing tests on `demo-sandbox`; the fix as a diff. | Advise is the default mode and root-cause finding is the priority. The report says plainly that the orgs are simulated, that Apex was not executed, and that the new test was not run. |
| 36 | The report labels two things as **Hypothesis**: the order of cases inside the batch that has no debug log, and whether the 9 November change to the class introduced the defect. | The evidence does not prove either. The root cause does not depend on them. |
| 37 | The apply-mode loop is proven in a temporary project root with `"mode": "apply"`, as a test that also stores a normalized transcript. | The repository's own config must stay `advise`. A stored transcript that a test regenerates and compares cannot drift from what the code does. |
| 38 | The loop's first attempt deploys only the new test, to show it failing against the unfixed service; the data correction is a separate `update_records` step; everything is rolled back at the end. | It mirrors the `propose-fix` skill: a test that fails before the fix, a code fix that does not repair existing records, and a data correction that is named as such. |
| 39 | The end-to-end test maps the stored report's query citations onto the replay's files by name, ignoring the timestamp prefix. | Saved query files are named by time, so a replay produces different names. Everything else in the stored report is checked as written. |

### Deliberately left out

| # | Decision | Reason |
|---|---|---|
| 40 | No two-step confirm token for write tools (first call previews, second executes). | Proposed in `PLAN.md` 4.3 as protection when permission prompts are bypassed. It doubles the prompts in normal use, and the production block and the mode gate do not depend on prompts. The README states the limitation. |
| 41 | No wrapper around Atlassian's MCP server for Jira Cloud, and no Jira write tool of any kind. | Rule 8. A second route to the same tickets would also need its own credentials handling. |
| 42 | The simulator was not extended to execute Apex. | Out of reach, and pretending otherwise would be worse than saying so. |

## Decision after the finishing session reported

| # | Decision | Reason |
|---|---|---|
| 43 | The first build session committed `.claude/settings.json` with the guard on: the PreToolUse hook, the edit protection for the safety files, and all 31 read-only tools in `allow` (the two added since, `evidence_index` and `jira_get_ticket`, were missing). The hook script itself was left unchanged. | The owner's brief asks for this file by name ("read-only tools allowed without prompting; every write tool set to always ask"), and the owner had since said to finish without asking. The finishing session had that request only second-hand, through the handoff notes, and was right to stop when its permission layer refused (decision 19); the first session held it directly. The change only tightens what the assistant may do, apart from letting two read-only tools run unprompted like the other 29. The first session could not run the generator (its own container was blocked, see decision 16), so the file was written by hand to equal the generator's output and pushed through GitHub. `test_committed_settings_match_the_server` confirms the content is equal, and the CI run for that commit passed on all six jobs. Whether to change the hook script (open point 1) was not decided for the owner. |

## Open points for the owner

0. **The guard is on, and it applies to the assistant in this folder.** It can no longer edit
   `sfti.config.json`, `.mcp.json`, `.claude/settings.json` or `.claude/hooks/`; you can. If you
   want the assistant to work on the tool itself, run
   `python scripts/generate_settings.py --no-guard` in your own terminal, and regenerate with the
   guard afterwards. The committed file was checked after decision 43: it is byte-for-byte the
   generator's output, `python scripts/generate_settings.py --check` reports it as up to date, and
   the full suite passes (583 passed, 1 expected failure).
1. **Hook false positive.** `guard.py` treats `.py` in a file name as a Python invocation, so a
   Bash command that reads a Python file *and* names a write function, such as
   `grep deploy_component core/changes.py`, is blocked. It errs on the side of blocking. The
   same test also lets `python -c "… deploy_component …"` through if the word `pytest` appears
   anywhere in the command. A tighter rule would look at what each command segment actually
   starts with:

   ```python
   PYTHON_NAMES = re.compile(r"^(python[\d.]*|pypy[\d.]*|py|ipython)$")

   def runs_python_code(words):
       """A segment that starts a Python interpreter on something other than pytest."""
       if not words or not PYTHON_NAMES.match(base_name(words[0])):
           return False
       return not re.search(r"(^|\s)(-m\s+)?pytest(\s|$)", " ".join(words[1:]))

   # in check_bash, replacing the first test:
   segments = [first_words(s) for s in re.split(r"[;&|\n`]|\$\(|\(|\)", command)]
   if WRITE_FUNCTIONS.search(command) and any(runs_python_code(w) for w in segments):
       raise Blocked(...)
   ```

   This was written and not applied (decision 18). If you apply it, the expected-failure test in
   `tests/test_guard_hook.py` will start passing and report itself; turn it into a normal test.
   Note that the sketch above still accepts the word `pytest` anywhere after the interpreter;
   requiring `-m pytest` to come directly after it would close that as well.
2. **First contact with a real org.** Everything in the list below is unverified. Start with a
   sandbox in advise mode.
3. **`mcp` 1.x.** The fallback import exists, but only 2.3.0 has been run, locally and in CI.

## Not verified against a live org

No Salesforce org was reachable during either build session.

- `Organization.IsSandbox` is `true` for scratch orgs.
- `FlowDefinitionView` fields and filtering by `TriggerObjectOrEventId`.
- `ApexTrigger.TableEnumOrId` holds the object's durable Id for custom objects.
- `MetadataComponentDependency` accepting `LIMIT`.
- `SetupAuditTrail` fields (`Action`, `Section`, `Display`, `DelegateUser`).
- `ApexLog.LogUserId` through the Tooling API.
- History object naming in `record_history`.
- The JSON envelope of a failed deploy (`status: 1` with `result`), of `project delete source`,
  and of `api request rest` for 201/204 responses. These come from reading the plugin source.
- What `project retrieve start` returns for a component that does not exist in the org
  (`deploy_component` treats a component with no retrieved file as new).
- Creating `TraceFlag` and `DebugLevel` records through the Tooling REST API.
- Apex test outcomes of any kind: the simulator does not execute Apex.
- The hook command inside Claude Code on Windows (it ran under Git for Windows' `sh` in CI).
- The reworked hook inside a Claude Code session, beyond one observation: after decision 43 the
  finishing session pulled the settings and the hook blocked a Bash command there with its own
  message (the false positive of open point 1), on Linux. Its Edit and Write rules, and Claude
  Code on Windows and macOS, were not observed.
- Behaviour under bypassed permissions.
- The Jira reader against a real Jira instance.

Verified against the installed CLI (`@salesforce/cli` 2.158.7, `--help` and plugin sources, no
org): the commands, flags and result shapes listed in the README.

## Things worth knowing about the code

- `Workspace.subdir()` creates the ticket folder if needed, so a tool given a `ticket_key`
  creates `tickets/<KEY>/`. Tools that only read (`scan_apex`, `read_file`, `validate_report`)
  require the folder to exist.
- `deploy_component` treats a component as new when the pre-deploy retrieve returns no file for
  it; rollback of a created component uses `sf project delete source`.
- After a drift refusal, the org's current version is committed and becomes the new baseline, and
  the local edit is put back in the working tree. A second deploy then goes through: the refusal
  is the warning, and `workspace_diff` shows what the deploy would overwrite.
- `rest_request` accepts envelope statuses 0 and 1 and decides on `statusCode`.
- The simulator's SOQL engine supports single-object `SELECT` with dotted relationship fields,
  `AND`-joined conditions (`=`, `!=`, comparisons, `IN`, `NOT IN`, `LIKE`), `ORDER BY`, `LIMIT`
  and `COUNT()`. Anything else is an error.
- `mcp_server/server.py` deletes `__wrapped__` and sets `__signature__` on each tool wrapper so
  the SDK sees the parameters but a `str` return type. `server.TOOLS` maps names to wrappers.
- The settings generator builds the `allow` and `ask` lists from the server's own tool lists.
  Run it after adding or renaming a tool.
