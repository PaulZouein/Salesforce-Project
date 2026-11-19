# What was verified, and what was not

The project was built without a live Salesforce org. This page shows the demonstration on the
simulated orgs, then lists what was verified, how, and what still needs a real org or a real
Windows or macOS desktop.

- [The demonstration: DEMO-101](#the-demonstration-demo-101)
- [What was verified, and what was not](#what-was-verified-and-what-was-not)

## The demonstration: DEMO-101

`samples/DEMO-101/ticket.txt` reports that some cases for Premium accounts are created with
Medium priority. The ticket's comments point at a flow. The investigation, run in advise mode
against the simulated orgs, is stored in `samples/DEMO-101/expected/`:

| File | What it is |
|---|---|
| `ticket.md` | The structured intake, with six ambiguities |
| `report.md`, `report.html` | The report: 19 evidence rows, 23 verified citations, root cause confirmed |
| `comment.md` | The draft ticket comment |
| `fix.diff` | One changed statement and one new test method |
| `apply-loop-transcript.txt` | The apply-mode loop on the simulated sandbox |

The root cause it reaches: `CaseEscalationService.cls` line 30 is `return;` inside the loop over
the trigger batch, so the first non-Premium case of a batch stops the rule for every later case.
The chain is: the affected records all follow a Standard-account case in their import batch
(query); the line (source); the debug log of a failing import shows statement 30 executing and
statement 32 never executing; the flow only reads the priority, so the missing notification is a
consequence. What the evidence does not prove is labelled **Hypothesis** in the report.

`tests/test_demo_e2e.py` replays the same tool sequence and checks the stored report against the
replayed evidence.

### Apply mode, proven on the simulated sandbox

The loop runs in a temporary project folder with `"mode": "apply"`; the repository's own config
stays `advise`. From `samples/DEMO-101/expected/apply-loop-transcript.txt` (Ids shortened):

```
## attempt 1: add the new test only, validate
REFUSED [deploy_failed] The validation failed with status 'Failed'. The org was not changed.
  test failure: CaseEscalationServiceTest.mixedBatchEscalatesEveryPremiumCase: System.AssertException:
  Assertion Failed: Every Premium case in a mixed batch should be High: Expected: High, Actual: Medium
-> the new test fails against the current service: it reproduces the defect. The org is unchanged.

## attempt 2: service fix and test together, validate
change CHG-<id>: status = validated, tests_run = 3

## deploy_component
change CHG-<id>: status = deployed, verified = True, tests_run = 3

## run_apex_tests
outcome = Passed, passing = 3, failing = 0

## re-check the symptom in data
Premium cases that are not High: 3 (500Ws00000C0003IAA, 500Ws00000C0004IAA, 500Ws00000C0006IAA)
-> as expected: the code fix does not repair records that were saved before it.

## update_records (data correction, a separate approved step)
change CHG-<id>: status = applied, verified = True
Premium cases that are not High: 0

## the same write on production
REFUSED [production_write_blocked] 'deploy_component' was refused: 'demo-prod' is classified as
production (IsSandbox = False, OrganizationType = 'Enterprise Edition'). Production is read-only.

## rollback_change (records, then the deploy)
change CHG-<id>: status = rolled_back, restored = 3 records
change CHG-<id>: status = rolled_back, verified = True
Premium cases that are not High: 3 (back to the starting state)
```

The test asserts that the sandbox's files are byte-for-byte identical before and after the loop,
and that the production org was never touched.

**Read this with its limit in mind:** the simulator does not execute Apex. "The new test fails,
then passes" comes from a rule in `samples/planted-bug/org-data/tests.json` that looks at the
source text, not from a Salesforce runtime. What the loop proves is the tool's behaviour: guard,
snapshot, drift check, deploy, verification, test reporting, audit and rollback.

## What was verified, and what was not

### Verified against the real Salesforce CLI (`@salesforce/cli` 2.158.7)

Checked with `--help` and by reading the plugin sources, during the first build session, with no
org connected:

| Command | Flags used | Notes |
|---|---|---|
| `org list` | `--json` | result groups: `other`, `sandboxes`, `nonScratchOrgs`, `devHubs`, `scratchOrgs` |
| `org display` | `--target-org` | result has `accessToken` (stripped), `id`, `apiVersion`, `instanceUrl`, `username`, `alias` |
| `data query` | `--file`, `--target-org`, `--use-tooling-api` | |
| `sobject describe` | `--sobject`, `--target-org` | |
| `project retrieve start` | `--metadata` (repeatable), `--target-org`, `--wait`, `--ignore-conflicts` | result `files[]` with `fullName`, `type`, `state`, `filePath` |
| `project deploy start` | `--metadata`, `--target-org`, `--test-level`, `--tests`, `--wait`, `--dry-run`, `--ignore-conflicts` | exit code set from the deploy status |
| `project delete source` | `--metadata`, `--target-org`, `--no-prompt` | |
| `apex run test` | `--class-names` (repeatable), `--result-format json`, `--code-coverage`, `--wait`, `--target-org` | exit code 100 on test failures |
| `apex get log` | `--log-id`, `--output-dir`, `--target-org` | with `--output-dir` the log is written to `<dir>/<id>.log` and the JSON result is the list of log texts (plugin-apex `apex/get/log.js`); a log that expired or was deleted is a 404 (`NOT_FOUND`) |
| `apex run` | `--file`, `--target-org` | a compile or runtime failure throws, with the result in `data` |
| `api request rest` | URL, `--method`, `--body @file`, `--target-org` | result `{statusCode, headers, body}`; exit code 1 when HTTP >= 400 |
| `org login sfdx-url` | `--sfdx-url-stdin -`, `--alias`, `--json` | read from `@salesforce/plugin-auth` 6.0.5 and `@salesforce/core`: the value is read from standard input only; the URL's host is used for the token refresh and the user lookup, so only the org's own address must be reachable. `org login device` no longer exists in 2.158.7 |

The error envelope (`name`, `message`, `exitCode`, `status`, `stack`) was captured live.

### Verified by the test suite

The suite runs in GitHub Actions on every push on Windows (Python 3.11) and macOS (Python 3.13),
on all four combinations when the workflow is run by hand, and on Linux in the build container. One test is an expected failure: the hook's documented false positive. It covers the CLI wrapper, the guard,
queries and redaction, metadata, logs, tests, the whole write path, the report checks, the MCP
server over stdio and the hook. It runs against the **simulator**, through the real subprocess
path.

### The web interface and the org memory

- The page was driven in a real browser (Chromium, desktop and phone width) through the whole
  demo flow: set-up checks, sample ticket, replay, result tabs, org memory and its check. No page
  errors. Its server is covered by tests, including the refusals (foreign host, missing token).
- **A complete AI investigation started from the page was run**, four times, in the build
  container, on the simulated orgs with the sample ticket and Sonnet: a first run (2 minutes,
  root cause confirmed, 14 citations verified, fix as a diff with a new test, draft comment,
  three facts saved to the org memory), a follow-up after answering its open questions through
  **Add information** (1 minute, 18 citations), and two recordings for the demo video (the
  published one took a minute and a half; the first was discarded after it showed the two
  defects fixed in decisions 67 and 68). All four confirmed the same root cause. The folder had
  never been opened in Claude Code. The unedited output of the first two is in
  `samples/DEMO-101/live-run/`. In the tests Claude Code is still replaced by a stand-in that
  speaks the same protocol.
- **Again after the switch to `dontAsk`** (decision 85), on 19 November: one run of the sample
  ticket from the page with a $2 spending limit, on Claude Code's default model (Opus 5.5).
  Finished in under 3 minutes, root cause confirmed, evidence check passed with 18 citations,
  usage reported $1.16. Outside `tickets/` only the tool's own data changed (audit log, org memory,
  usage ledger): the run's file writes stayed in the ticket's folder.
- **The start scripts and the release zip:** on every push the workflow builds the zip on Windows
  and macOS machines, unpacks it into an empty folder and runs its own start script up to the
  page answering on the simulated orgs. **Not verified:** the double-click itself, and the
  warning Windows and macOS show for a downloaded script (the two notes in `QUICKSTART.md` come
  from the systems' documentation); a company proxy that blocks `pypi.org`.
- **Not verified:** an investigation started from the page on a real org, and on Windows or
  macOS (the runs were on Linux). Time and usage will differ there.
- **The usage box and Use fewer tokens:** checked with two real runs of the sample ticket with
  *Use fewer tokens* on, before and after trimming Claude Code's tools and adding the 5-minute
  cache: Claude Code accepted the options, the box followed each run, and the figures are above.
  The trimmed tool list was also checked with a short session that loads and calls an
  investigator tool. The PDF upload was checked in a browser with a stand-in for Claude Code;
  a real investigator opening a ticket's PDF was not run.
- **The kit for a real org** (`docs/LIVE-ORG.md`, `scripts/live_check.py`, `samples/live-org`)
  was checked against the simulated orgs only.
- The org memory's change check was exercised against the simulator only. On a real org it
  depends on `LastModifiedDate` of `ApexClass`, `ApexTrigger` and `Flow` through the Tooling API.

### Not verified against a live org

No Salesforce org was reachable while this was built. `docs/LIVE-ORG.md` is how to check all of
this on your own Developer Edition org in about half an hour. The following comes from documentation and
from the CLI's source, and may need adjusting on first contact with a real org:

- `Organization.IsSandbox` being `true` for scratch orgs.
- `FlowDefinitionView` field names and filtering by `TriggerObjectOrEventId`.
- `ApexTrigger.TableEnumOrId` holding the object's durable Id for custom objects.
- `MetadataComponentDependency` accepting `LIMIT` (the query no longer uses `ORDER BY`).
- `SetupAuditTrail` field names (`Action`, `Section`, `Display`, `DelegateUser`).
- `ApexLog.LogUserId` through the Tooling API.
- History object naming in `record_history` (`<Object>History`, `<Object>__History`,
  `OpportunityFieldHistory`).
- The JSON envelope of a failed deploy (`status: 1` with a `result`), of `project delete source`,
  and of `api request rest` for 201 and 204 responses.
- What `project retrieve start` returns for a component that does not exist in the org. The
  deploy tool relies on it to recognise a new component.
- Creating a `TraceFlag` and a `DebugLevel` through the Tooling REST API with the fields used.
- Every Apex test outcome: **the simulator does not execute Apex**.
- The hook command inside Claude Code on Windows. (The command itself ran under Git for Windows'
  `sh` in CI; Claude Code invoking it on Windows was not observed.)
- The hook and the permission rules under bypassed permissions.
- The hook inside Claude Code beyond one observation: with the committed settings pulled into a
  Claude Code cloud session on Linux, the hook blocked a Bash command with its own message (the
  documented false positive). Its Edit and Write rules, and Claude Code on Windows and macOS, were
  not observed.
- The optional Jira reader against a real Jira instance.

Each section of `describe_object` (automation) and `recent_org_changes` is queried separately, so
one query that a real org rejects reports an error in place instead of failing the call.

**Recommended first contact with a real org:** a sandbox, in advise mode, starting with
`org_info`, `describe_object` with automation, `recent_org_changes` and `find_dependencies`. If one
of them reports an error for a section, that is one of the items above.
