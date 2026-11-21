# Decisions

Why the Salesforce Ticket Investigator is built the way it is. Decisions 1 to 16 were taken in
the first build session, 17 to 42 in the session that finished the build while the owner was
away, 43 by the first session after the finishing session reported, and 44 onward when the
owner asked for the web interface and the org memory. The rules in `CLAUDE.md`
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
| 40 | No two-step confirm token for write tools (first call previews, second executes). | Proposed in `PLAN.md` 4.3 as protection when permission prompts are bypassed. It doubles the prompts in normal use, and the production block and the mode gate do not depend on prompts. `SAFETY.md` states the limitation. |
| 41 | No wrapper around Atlassian's MCP server for Jira Cloud, and no Jira write tool of any kind. | Rule 8. A second route to the same tickets would also need its own credentials handling. |
| 42 | The simulator was not extended to execute Apex. | Out of reach, and pretending otherwise would be worse than saying so. |

## Decision after the finishing session reported

| # | Decision | Reason |
|---|---|---|
| 43 | The first build session committed `.claude/settings.json` with the guard on: the PreToolUse hook, the edit protection for the safety files, and all 31 read-only tools in `allow` (the two added since, `evidence_index` and `jira_get_ticket`, were missing). The hook script itself was left unchanged. | The owner's brief asks for this file by name ("read-only tools allowed without prompting; every write tool set to always ask"), and the owner had since said to finish without asking. The finishing session had that request only second-hand, through the handoff notes, and was right to stop when its permission layer refused (decision 19); the first session held it directly. The change only tightens what the assistant may do, apart from letting two read-only tools run unprompted like the other 29. The first session could not run the generator (its own container was blocked, see decision 16), so the file was written by hand to equal the generator's output and pushed through GitHub. `test_committed_settings_match_the_server` confirms the content is equal, and the CI run for that commit passed on all six jobs. Whether to change the hook script (open point 1) was not decided for the owner. |

## Decisions for the web interface and the org memory

Taken after the owner asked for an easy interface to show the tool, and for a memory so that
each investigation makes the next one on the same org smarter, "with a double check to see if
anything changed".

| # | Decision | Reason |
|---|---|---|
| 44 | **Org memory fills by itself.** Retrieving source, listing an object's automation and looking up dependencies each add to a per-org map (components with a static outline, objects, links). The investigator adds short notes with `remember_org_fact`. A failure of the memory never fails the tool that triggered it. | A memory that depends on someone remembering to write it stays empty. The outline (methods, objects queried, fields set, calls; a flow's trigger and the fields it tests or writes) answers "what touches Case.Priority?" without any note. |
| 45 | **Memory is a lead, never evidence, and it is double-checked.** On recall, the org is asked for the last-modified date of each remembered component involved; on retrieve, the fresh source is compared with the remembered fingerprint. What changed is flagged, with the notes about it. Stale notes are flagged, not deleted; a new note can supersede one. | Rule 1 says every claim cites something read from the org. A remembered fact is neither current nor an artifact. The check makes it safe to use as a starting point, which is what the owner asked for. |
| 46 | The memory is local (`knowledge/`, gitignored), keyed by org Id rather than alias, holds metadata facts only, and refuses a note that contains an email address. Queries, logs and test results are not remembered. | It describes customer orgs and must not reach GitHub. An alias can be re-pointed at another org. Rule 7. |
| 47 | **The web interface is a local page on 127.0.0.1**, standard library only, with no route that changes an org. Requests for another host name are refused, and anything that changes state needs a token embedded in the page. There is no option to listen on another address. | It has to be safe to leave running on a laptop during a demonstration. The host check stops another website from reaching it through a domain that points at 127.0.0.1. A test scans the interface's code for any reference to the write functions. |
| 48 | **From the page the investigator only advises.** The page starts Claude Code with `claude -p`, the prompt on standard input, and (since decision 80) `--permission-mode dontAsk` with writes allowed only under `tickets/`. Org write tools stay in `ask`, and with nobody to answer they are refused. No flag that pre-approves tools or bypasses permissions is passed. | Rule 6: every write needs the owner's approval. An unattended run has no approver, so it must not be able to write. Standard input, not the command line, because on Windows `claude` runs through `cmd.exe` (same reason as decisions 7 and 8). |
| 49 | **A ticket's status is read from its folder**, not from what the page remembers. | The result shows up whether the investigation was started from the page, pasted into Claude Code by hand, or is still running, and survives a restart of the page. |
| 50 | The page offers a **recorded sample** for the sample ticket on the demo orgs: the real tools gather the evidence from the simulated org, the written analysis is the stored sample, and the page says so on the result. | A live AI run takes minutes and usage; a demonstration needs something that always works in seconds. Labelling it keeps it honest. The sequence lives in `simulator/demo.py`, shared with the end-to-end tests. |
| 51 | The page detects a folder that Claude Code does not trust yet and stops with the one command that fixes it. | Found by the one real unattended call made during the build: until the folder is trusted interactively, Claude Code ignores the project's `allow` list, so every investigator tool would be refused and the run would produce nothing. |
| 52 | `.claude/settings.json` was regenerated by `scripts/generate_settings.py` (guard on) to add the five memory tools to `allow`. | The owner was asked in the session and answered yes. Without it the memory tools would prompt in Claude Code and be refused in unattended runs. The diff is five added lines. |
| 53 | New skill `get-started`: set-up, connecting an org with the exact command, then the ticket, one step at a time. | The owner asked for guidance of the kind "connect your Salesforce CLI, say exactly how, then paste the ticket". The page does the same in a browser. |

| 54 | Supported systems are Windows and macOS. CI runs on those two; Linux was removed from the matrix. | The owner's decision. The code paths for Linux stay, because the build sessions run on it, but nothing is promised there. |

## Decisions for the live path, easy set-up and follow-ups

Taken after the owner asked to adjust "memory, interface user friendliness, easiness to set up
(make a zip containing what is needed, with a clear easy readme), efficiency, review, tests",
and later for "a video demo or screenshots of every step".

| # | Decision | Reason |
|---|---|---|
| 55 | **The page states on Claude Code's command line what an unattended investigation may use**: only the project's own MCP server (`--strict-mcp-config`), the read-only tools by name from the server's registry, and the org write tools, the shell and web access removed. Replaces decision 51 and narrows decision 48. | Found by experiment: a tool named on the command line works in a folder Claude Code was never opened in, so the one-time trust step is not needed and the run no longer depends on the user's own Claude Code set-up. Connectors the user has for other things are out of reach of a run nobody watches (rules 7 and 8). Removing the write tools is stronger than leaving them to be refused (rule 6). Nothing is pre-approved that the committed settings do not already allow. |
| 56 | A run that ends without a report is "incomplete", not "finished". A run whose tool server did not start is stopped at once. Nothing is reported after a run was stopped. | Each was a way for the page to say something untrue about a run. |
| 57 | **Follow-ups.** What the user adds after a run (text, or a file of a listed type, 5 MB at most, 20 per ticket, the name reduced to a plain file name) is saved in `attachments/`. The next run is told to read it and to continue from the ticket folder. | An unattended run cannot ask (rule 2), so its questions come back as open questions; this is how the user answers them. Continuing costs half of starting again in the recorded follow-up. |
| 58 | A model can be chosen per ticket, or with `SFTI_CLAUDE_MODEL`. The name must be a plain word, so it cannot carry a command-line option. | The owner hits usage limits; Sonnet did the sample ticket in two minutes. |
| 59 | `describe_object`, `find_dependencies`, `recent_org_changes` and `record_history` can save their answer under `queries/` (`ticket_key`, `save_as`). | The first live run had to write "no saved artifact" for two claims, which is rule 1 not being satisfiable. The follow-up run then cited one of them. No tool was added, so the permission lists are unchanged. |
| 60 | The page remembers for a short time what takes seconds to ask (org list, org kind, set-up checks, Claude Code version). For display only; the guard never reads it. | With the real CLI each of these is a process start of several seconds, and the page asked on every view. |
| 61 | **Start scripts** for Windows and macOS find Python, create a private `.venv`, install the project into it once, report missing tools and start the page. `scripts/bootstrap.py` is the one step behind them. They install nothing system-wide. | The owner asked for an easy set-up. A private environment leaves the user's Python alone; `SFTI_PYTHON` is passed on so the tool server runs with the Python that has the project. |
| 62 | **The release zip is built from an explicit list** and refused when `sfti.config.json` is not in its default state. The workflow unpacks it into an empty folder on Windows and macOS and runs its own start script up to the page answering. | Tickets, org memory and audit data describe customer orgs and must never travel in a zip (rule 7). A zip is only "what is needed" if it has been seen to work from nothing. |
| 63 | `python -m webapp` takes the next free port, and opens a copy that is already running instead of starting a second one. | Two pages on the same tickets folder would start two investigations of one ticket. |
| 64 | **Org memory: one writer at a time** (a per-org lock file), and the double check asks the org before taking the lock. Recall also lists earlier tickets on the same components with what each concluded. | The tool server and the page are separate programs writing one file: whichever saved last undid the other. Earlier conclusions are a lead, and labelled as one. |
| 65 | **The tests may not touch `tickets/`, `knowledge/` or `audit/` of the checkout.** A check over the whole run fails it otherwise. | During this work the stand-in for Claude Code wrote its report into the current folder when the page asked for its version, and overwrote the report of the live run. The report was restored from the session's record and shown identical by re-exporting it. The stand-in was fixed and the check added, so that a user's real tickets cannot be damaged by running the suite. |
| 66 | The demo video and the step-by-step screenshots are recorded by a script from the running page, on the simulated orgs, with a real investigation. The video is not part of the release zip. | The owner asked for them. Recording from the real page keeps them true to what a user sees; the zip stays small. |
| 67 | In an answer of the page's server, `ok` only says whether the request worked; the set-up check reports `ready`. | Found while recording the video on a computer without the Salesforce CLI: the page showed "Something went wrong" where the checks and the install command belong. The check's own "not everything is installed" flag had the same name as the envelope's, so it only failed when a required tool was missing, which is exactly when a newcomer needs that page. |
| 68 | Progress lines show paths inside the project without their prefix, and reading one of the project's own step descriptions is called that. | The prefix is long and contains the name of the user's home folder, which does not belong on a screen that is being shown to others. |


## Decisions for the usage box, fewer tokens, PDF tickets and the dev-org kit

Taken after the owner asked for "a token usage meter as a little box that always shows ... with
the estimated price ... that updates as the agent is working", "an option to put the ticket as a
PDF file (export of the ticket in Jira)", "an option to optimize token usage", and offered a
Developer Edition org for testing.

| # | Decision | Reason |
|---|---|---|
| 69 | The usage box adds up the tokens of each model response in Claude Code's progress stream (a response repeats its usage on every content block, so each is counted once) and prices them at Anthropic's API list prices; Claude Code's own total replaces the estimate when the run ends. The plan figures (5-hour and weekly limits) are shown as Claude Code reports them, reduced to numbers and states. Finished runs go to `usage/ledger.jsonl`, local and never committed. | A figure that moves while the run works is what was asked for, and the stream carries it. The estimate was within 1 to 4 percent of the reported total on two recorded runs. The owner keeps reaching usage limits, so the plan figures are the most useful part for them; for a subscription the dollar figure is what the run would cost at API prices, and the page says so. |
| 70 | **Use fewer tokens** = Sonnet unless a model is chosen, `--effort medium`, no subagents (`Task`, `Agent` refused), `--disable-slash-commands`, and an instruction to economise. The evidence rules are unchanged. | Each is a lever with a known effect: price per token, deliberation and number of steps, a second context re-reading files, a skill list sent with every step. Medium is Anthropic's suggested starting effort for multistep tool use on Sonnet 5.5. The prompt names the step files, so skills are not needed as commands. The first version cost $0.51 on the sample (13 steps), no saving: most of the price was context written to the 1-hour cache. Hence decision 75 and the 5-minute cache (`CLAUDE_CODE_PROMPT_CACHE_TTL=5m`, which Claude Code documents): $0.26 in 15 steps, against $0.44 to $0.77 for runs without the option. |
| 71 | A spending limit per run is passed to Claude Code (`--max-budget-usd`). A run stopped there is *incomplete*, with a message that says so and keeps the work. | Claude Code enforces it, so it cannot be overrun by the page. Treating it as an error would hide that the user asked for it. |
| 72 | A ticket can be a PDF: its text is read with `pypdf` (a new requirement, pure Python) to fill the form, the PDF is kept as `attachments/ticket-original.pdf`, and the investigator is told to open it for what the text loses. A PDF without text needs only the key. 10 MB, 80 pages of text. | Jira's export or printout is how many people share a ticket. Claude Code reads PDFs itself, images included, so the PDF stays the source and the text is a convenience. |
| 73 | **The dev-org kit runs on the owner's computer**: a guide, a read-only live check that writes a scrubbed report, and a sample for a real org (permission set, data that triggers the bug in one batch, a ticket). The sample's flow is not deployed. | The build container's network refuses Salesforce, and the guard stops the assistant from running the CLI, rightly: it is what keeps production read-only. The flow's notification action lacks inputs a real org requires, and the bug is in Apex. A field deployed through the API is visible to nobody until access is granted, hence the permission set. |
| 74 | Titles lose the key Jira puts in front of them (`[ABC-12] ...`). | It showed twice on every page. |
| 75 | **Every run from the page gets only the built-in tools an investigation uses** (`--tools Read,Write,Edit,Glob,Grep,ToolSearch,Skill,Task,Agent,TodoWrite`; without Skill, Task and Agent when using fewer tokens). | Measured: the starting context went from 41,254 tokens to 11,644, because each tool's description is sent with every step; the investigator's own tools still load through ToolSearch. It also removes capabilities an unattended run never needs (scheduling, messages, notifications, worktrees). |
| 76 | Views that write into the page after waiting for an answer first check that they are still shown. | Found in the first real run of the box: leaving New ticket before the org's kind arrived raised an error in the page. |

Done since: a complete AI investigation started from the page was run (see [VERIFICATION.md](VERIFICATION.md),
"The web interface and the org memory").

## Decisions for connecting an org where there is no browser

Taken after the owner offered to log in to their Developer Edition org for the cloud session:
"create a claude web page or something and ill login for you".

| # | Decision | Reason |
|---|---|---|
| 77 | **The page can connect an org from an authorization URL** (`force://…`, from `sf org auth show-sfdx-auth-url` on a computer with a browser). The URL goes to `sf org login sfdx-url --sfdx-url-stdin -` on standard input, through a dedicated `SfCli.connect_with_auth_url`; it is never in a command line, a log, an answer or an error, and the page stores nothing. Refused in demo mode. | The installed CLI (2.158.7) has no device login any more, and a web login needs a browser on the same machine. The URL's host is the only address the CLI then contacts, so a cloud session needs exactly one host allowed. `org login` stays a write for the generic runner, so nothing else can reach it; Claude Code is not given it: the user pastes, the user presses Connect. It changes nothing in any org, and every write still goes through the guard, which asks the org what it is every time. |
| 78 | **`--allow-host NAME`** lets the page answer one exact extra name, for a proxy the user trusts in front of it (a cloud session's preview). Refused names are printed once in the window. No wildcard; loopback names are already allowed. The page now asks for `api/…` relative to itself, so it also works when a proxy serves it under a path. | The host check stops a website from reaching the page through a name that points at 127.0.0.1. A preview proxy is reached by its own name; allowing that one name, on purpose, keeps the check for everything else. |
| 79 | **Each push runs two CI jobs**, Windows on Python 3.11 and macOS on Python 3.13; *Run workflow* by hand runs all four combinations; a newer push cancels a run still going on the same branch. | The owner's GitHub Actions minutes for the month ran out after 23 runs: on a private repository macOS minutes count ten times and Windows twice, about 126 minutes a push with four jobs, half that with two. Both systems and both Python versions stay covered on every push. |

## Decisions after the independent review

Taken after three independent reviewers (safety, correctness, Salesforce behaviour) read the code
and each reproduced its findings against the code with a fake CLI runner; none of them ran `sf` or
reached an org. Every fix below has a test; the simulator was changed to answer like the real CLI on the failure paths (decision 96).

| # | Decision | Reason |
|---|---|---|
| 80 | **A permit is bound to the org's username**, read when the org is checked, and every permitted command runs against that username. A permitted command must name its org exactly once, as `--target-org`; `-o`, `--targetusername`, `-u` and a second target are refused. | An alias is a pointer the user (or another terminal, or the page's connect form) can move at any moment. A permit bound to the alias let a write checked against a sandbox run against whatever the alias pointed to a second later, production included. A username names one org for good. |
| 81 | **`FOR VIEW`, `FOR REFERENCE`, `UPDATE TRACKING` and `UPDATE VIEWSTAT` are refused** like `FOR UPDATE`; `COUNT()` and other single-row aggregates are left without `LIMIT`. | Each of those clauses makes a `SELECT` change the org (last-viewed dates, Knowledge statistics), and query tools run without approval. A `LIMIT` on `COUNT()` caps the count, and Salesforce refuses one on an aggregate without `GROUP BY`. |
| 82 | **Redaction follows what Salesforce really returns**: the related record behind a polymorphic lookup is typed `Name`, not `User`; aggregate columns are named by alias, `exprN` or the field's last segment; person accounts are accounts; a lookup's field history is stored twice, once with Ids and once with names; the setup audit trail describes user changes with the user's name; debug logs quote e-mail addresses. All of these are now redacted or masked. | The simulator labels related records with their real object type, so these shapes never came up in its tests. Each let a person's name or address reach a saved file. |
| 83 | **The report check accepts only reserved example domains** (`example.com`, `.org`, `.net`, `.example`, `.localhost`) and system mailboxes; `.invalid` and `.test` are flagged. Phone numbers are matched in the usual international and national formats, from nine digits. | A sandbox refresh turns `jane@acme.com` into `jane@acme.com.invalid`: still a real person. Nine digits keeps case numbers, dates and line ranges out. |
| 84 | **The page runs no inline script** (`script-src 'self'`, the code moved to `static/app.js`, the token in a `<meta>`), escapes report text before turning links into anchors, and serves exported reports under `sandbox; default-src 'none'`. | A report contains text from the org and the ticket. Escaping first and allowing no inline script means a crafted link or field value cannot run in the page that holds the session token. |
| 85 | **Runs from the page use Claude Code's `dontAsk` mode with `Edit(tickets/**)` as the only write rule** (it covers `Write` too). | `acceptEdits` let the unattended run edit any file in the folder, the guard's own files aside. Checked by probe runs: `Write(tickets/**)` matches nothing, `Edit(tickets/**)` matches both tools. |
| 86 | **Debug logs**: a log cut off at Salesforce's size limit is flagged as truncated, with the warning that absence proves nothing; limits are kept per namespace, the org's own (`(default)`) as the headline; the log text that CLI 2.158 returns is saved as the log; a deleted log says so; dates are sent in the form SOQL accepts. | Each was a way to conclude from a log something the log does not show. |
| 87 | **Connecting an org under an alias already in use asks first** on the page, and replaces the alias only when the user confirms. | Re-pointing an alias silently changed which org an investigation reached. |
| 88 | **The report check takes the root-cause status from the header row**, and a report marked Confirmed must cite at least one artifact that exists. The section text decides only when the header row is missing. | "The flow hypothesis was not confirmed" in the text of a confirmed report turned the evidence rule off: the page showed "Confirmed" with "Evidence check passed · 0 citations". |
| 89 | **A retrieve that failed in the org is an error**, with the org's error code and message. A deploy whose snapshot retrieve fails stops before deploying. A rollback refuses a change that recorded changed components but no file paths. | With `--json`, a failed retrieve exits 0 with `success: false` and no files: it looked like an empty retrieve. The deploy then took no snapshot, skipped the drift check, and a later rollback restored nothing, redeployed the fix and reported success. |
| 90 | **A deploy that outlives its wait is recorded as `in_progress`, never as failed** (the CLI's `ClientTimeoutError`, exit code 69, or our own process time limit). `rollback_change` accepts it: it first reads the components from the org; while the org still holds the version from before, it does nothing; once the deploy has landed, it restores the snapshot; anything else needs `force`. A change whose rollback failed can be rolled back again. | The deploy carries on in the org after the CLI stops waiting (20 minutes by default; `RunLocalTests` in a full sandbox often takes longer). Recorded as "did not run", it changed the org with no record a rollback would accept. Polling with `--async` and `project deploy report` would need a new command in the allowlist and could not be tested against a real org here; reading the components back can. |
| 91 | **The org-memory check asks for each flow by name** (`FlowDefinitionView.ApiName IN (…)`, then the active version by Id) and reports a flow that is still there but no longer active as changed, not as missing. Classes and triggers are matched with `NamespacePrefix = null`. | The old query read the first 200 active flows of the org and called any other flow "no longer in the org". |
| 92 | **`recent_org_changes` says when sources failed**: "every source failed" is not "no changes recorded". | All five sources could fail (a malformed date) while the answer said nothing had changed. |
| 93 | **`find_dependencies` says when one name matched several components** (`matched_components`, `same_name_warning`): each row carries the Id of the component it was matched on. | A custom field is stored without its object (`Tier` is Account's and Case's `Tier__c`) and a validation rule name is unique only per object, so their dependencies were merged without a word. Looking each component's Id up first needs a different query per type, none of which could be checked against a real org here. |
| 94 | **Scratch orgs are recognised from their own Organization record** (a sandbox with a trial expiration date, the CLI's rule), and an org whose `org display` fails (a scratch org whose Dev Hub is not connected here) is still classified when the org itself answers, with writes refused because its username is unknown. | `org display` asks a scratch org's Dev Hub; without it the org could not be used at all. |
| 95 | **A Developer Edition org with a namespace is read-only whatever the settings say.** | It is the packaging org: the source every release of a managed package is built from. `allow_developer_edition_writes` was meant for practice orgs. |
| 96 | **The simulator answers like the real CLI on failure paths**: a missing component is a `Failed` file entry, a retrieve never says `Unchanged`, failed REST calls have status 1, a downloaded log comes back as its text and a missing one as a 404, and an org can carry a trial expiration date and a namespace. | The tests could not see the bugs above because the simulator only ever succeeded or failed outright. |
| 97 | **A ticket started in Claude Code can be continued from the page**: with no org recorded, the page asks for the alias once and stores it; any unexpected failure of a run marks it failed with the reason. | It has no `request.json`: the run died on a `KeyError` and the page showed "running" for good, on every retry. |
| 98 | **A long ticket given as a PDF is saved with its text cut** (a note says where, and that the PDF has the rest); the PDF itself is kept whole. | About 50 dense pages read as 200,000 characters; the header added afterwards pushed it over the limit and the ticket was refused. |
| 99 | **Attachment names keep their type whatever the name**: letters outside A-Z become `attachment`, a long name is shortened before its type, and a different file with the same cleaned name gets `-2` instead of replacing the first. The same file sent again keeps its name. | "Снимок.png" was refused as a file of unknown type, a long name lost its type, and "ошибка 1.png" was overwritten by "скрин 1.png" (both `1.png`): evidence lost without a word. |
| 100 | **`update_records` compares what it reads back the way Salesforce stores it**: datetimes as instants, Ids on their first 15 characters. | `2026-03-01T08:00:00Z` comes back as `…00.000+0000` and a 15-character Id with 18: a successful update was reported as changed by automation, which is false evidence. |
| 101 | **Input checks end with `\Z`, not `$`.** | `$` also matches before a final line break: `init_ticket("ABC-123\n")` made a folder whose name ends in one. |
| 102 | **Windows details**: git runs with `core.longpaths=true`; the org-memory lock treats "access denied" (a lock file still being deleted) as busy rather than impossible; each save writes its own temporary file and retries the final rename briefly. | Deep metadata paths under a long project folder failed with "Filename too long", and two programs could both write the org memory without the lock. |
| 103 | **pytest and the simulator ask before they run; a hardened hook and `--protect-code` are prepared for the owner** (`scripts/owner/`). The generator no longer allows `python -m pytest` or `python -m simulator` without asking. The proposed hook blocks running anything under `tickets/` and excuses only commands that start pytest. `--protect-code` denies edits to `core/`, `mcp_server/`, `webapp/`, `scripts/`, `CLAUDE.md` and the skills. | The safety review ran code from a saved debug log with `python -m pytest --doctest-glob=*.log tickets/…`: allowed without a prompt, it changed a record in production with nothing in the audit log. A permission rule matches the start of a command and cannot stop what follows. The assistant may not edit the settings or the hook, so both are prepared and tested (`tests/test_proposed_guard.py` runs every case of the installed hook through the new one) and the owner applies them. `--protect-code` is an option, not the default: in the folder where the tool is developed it would stop that work. |
| 104 | **A deploy whose read-back fails is still recorded** (deployed, not verified, no after-deploy state); `rollback_change` then reads the components from the org first, as for an unfinished deploy. | Second review: since a failed retrieve became an error (decision 89), a failure of the verify retrieve left the org changed with no record a rollback could use. |
| 105 | **Rollback details**: a refused rollback puts the user's working tree back; retrying a rollback that stopped half-way skips files that already hold the snapshot and new components already deleted; only `ClientTimeoutError`, exit 69 or our own time limit mean "still running"; a new component that is not what was deployed needs `force` before a rollback deletes it. | Second review: the retrieve overwrote the edit, a retry was refused as drift, "another deployment is still running" was taken for this one, and someone else's component could be deleted. |
| 106 | **git refuses a ticket repository whose own settings would run a program** (filter, diff and merge drivers, includes, aliases, fsmonitor, hooks path and the like), and runs with `core.fsmonitor=false`, `core.hooksPath` pointing nowhere and `core.pager=cat`. An investigation from the page may not write `.git/`, `.gitattributes` or `.gitmodules` under `tickets/`. | Second review: an unattended run may write anywhere under `tickets/`, including a metadata repository's `.git/config`; a planted `core.fsmonitor` ran on the next commit, unprompted and unaudited. |
| 107 | **Usernames, aliases and nicknames are redacted** in grouped queries and behind polymorphic owners; addresses are masked in the query text saved with the evidence and in a saved debug log read back with `read_file`. | Second review: `GROUP BY Owner.Username` returned the usernames, and two ways around the e-mail masking were left. |
| 108 | **`-oprod` and `-uprod` count as target flags** in the permit check. | Second review: attached short forms escaped the "exactly one `--target-org`" rule (no tool input can produce them today). |
| 109 | **The read-back comparison of `update_records` compares texts as texts**, recognises an 18-character Id by its checksum and keeps fractions of a second. | Second review: `00123` matched `123`, any 15/18-character pair matched as an Id, and `.500` matched `.000`. |
| 110 | **The report's status row is read by its first word**, also in bold: "Confirmed (not reproduced in a sandbox)" is confirmed. The same-name warning of `find_dependencies` says what to do when both components have the same type. | Second review. |
| 111 | **A renamed attachment gets `_2`, not `-2`.** | `follow-up-2.txt` would have been read as a note from the user that overrides the ticket. |
| 112 | **The proposed hook also sees a brace group (`{ python -c …; }`) and refuses doctest collection** (`--doctest-*`), which reaches saved logs under `tickets/` without naming the folder. | Second review of `scripts/owner/guard.py`. |
| 113 | **One file to run: `start.bat` on Windows, `start.command` on macOS.** The demo is a button on the page (**Try with the demo orgs**), Claude Code in the folder is `start.bat --claude`; the three other launchers are gone. The release zip ships the hardened permission settings and hook of decision 103, which only add protection. | The owner asked for a zip with just one file to run that opens and works. The page could already switch to the demo orgs, so the extra launchers only added choices. |
| 114 | **Not changed, on purpose:** a Person Account's name is redacted only when the query selects `IsPersonAccount` (adding the field fails in an org without Person Accounts); a custom lookup's history row with names and no matching Id row is shown; the phone check can warn about an IP address or a version number. | Each fix needs a describe per query or would hide ordinary data; the first two are rare, the last is a warning, not a refusal. Recorded here so the gap is known. |
| 115 | **The page logs in to an org for you: Log in with your browser.** It runs `sf org login web --alias <name> --instance-url <login address> --no-prompt --json` through a dedicated method of the CLI wrapper (like decision 77, not on the read allowlist); the Salesforce CLI opens the browser on the Salesforce login page and the page waits up to five minutes. Login addresses: `login.salesforce.com`, `test.salesforce.com` or a My Domain under `salesforce.com`; a `.lightning.force.com` address is refused with where to find the right one. Refused in demo mode, and a name already in use is confirmed before the browser opens. The terminal line stays as an alternative. | The owner asked to make it easier to work: connecting an org was the one step that needed a terminal. Logging in changes nothing in any org. `--no-prompt` because the CLI could otherwise wait for an answer that nobody can type. Checked in a browser against a stand-in CLI; a real login was not possible here. |
| 116 | **`start.bat` and `start.command` open the Python download page** when Python is missing, besides saying what to do. | One step less on a computer that has never had Python. |

## Open points for the owner

00. **First run on your computer.** Double-click `start.bat` or `start.command`, then **Try with the demo orgs**, then
   **Try the sample ticket** and **Investigate with Claude Code**. A real investigation from the
   page has now been run (decision 55), but on Linux and on the simulated orgs: your first run on
   Windows or macOS, and the first on a real org, are still firsts. Compare with
   `samples/DEMO-101/live-run/`.

0. **The guard is on, and it applies to the assistant in this folder.** It can no longer edit
   `sfti.config.json`, `.mcp.json`, `.claude/settings.json` or `.claude/hooks/`; you can. If you
   want the assistant to work on the tool itself, run
   `python scripts/generate_settings.py --no-guard` in your own terminal, and regenerate with the
   guard afterwards. The committed file was checked after decision 43: it is byte-for-byte the
   generator's output, `python scripts/generate_settings.py --check` reports it as up to date, and
   the full suite passes (583 passed, 1 expected failure).
1. **Apply the hardened settings and hook** ([`scripts/owner/README.md`](../scripts/owner/README.md),
   decision 103). In your own terminal: `python scripts/generate_settings.py`, then copy
   `scripts/owner/guard.py` over the installed hook (`guard.py` in the hooks folder under
   `.claude`), run the tests and commit both files. pytest and the simulator then ask before
   they run; the hook blocks running anything under `tickets/`, excuses only commands that really
   start pytest, and no longer blocks `grep deploy_component core/changes.py` (the false positive
   of decision 18). Until you do, the tests report two expected failures that name this step.
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
org): the commands, flags and result shapes listed in [VERIFICATION.md](VERIFICATION.md).

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
