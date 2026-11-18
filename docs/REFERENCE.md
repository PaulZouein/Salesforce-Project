# Reference

Every tool of the `sf-investigator` MCP server, calling them from the command line, using `core/`
from another project, and the test suite.

- [Tool reference](#tool-reference)
- [Using `core/` from another project](#using-core-from-another-project)
- [Tests](#tests)

## Tool reference

Read-only tools run without a prompt. Paths in results are relative to `tickets/<KEY>/`.

| Tool | Purpose |
|---|---|
| `get_config` | Mode, row limits, optional switches |
| `list_orgs`, `org_info` | Authenticated orgs; classification of one org |
| `init_ticket`, `list_tickets`, `search_tickets` | Ticket folder; earlier tickets ("has this been seen before?") |
| `jira_get_ticket` | Optional, off by default: read one ticket from Jira Server / Data Center |
| `recall_org_knowledge`, `org_map`, `verify_org_knowledge` | The org memory: what earlier tickets learned about this org, searched by subject and checked against the org's current state |
| `remember_org_fact`, `forget_org_fact` | Save a fact about how the org is built, or remove one that no longer holds. Local files only |
| `retrieve_components` | Retrieve `Type:Name` components into the ticket's git repository (at most 50 per call) |
| `list_files`, `search_metadata`, `read_file` | List, regex-search and read what was retrieved, with line numbers |
| `summarize_flow` | Outline of a Flow: trigger, entry conditions, decisions, record operations, order |
| `scan_apex` | Bug-prone patterns in retrieved Apex. Leads, not findings |
| `describe_object` | Fields, record types, child relationships, and the automation on an object. With `ticket_key` the answer is saved as evidence |
| `soql_query`, `tooling_query` | `SELECT` with enforced limits and redaction; saved as evidence when given a `ticket_key` |
| `find_dependencies` | What uses a component and what it uses. An empty list means "none recorded". With `ticket_key` the answer is saved as evidence |
| `recent_org_changes`, `record_history` | Setup audit trail and modified code in a time window; field history of one record. With `ticket_key` the answer is saved as evidence |
| `list_debug_logs`, `get_debug_log`, `analyze_debug_log` | Logs: list, download with an excerpt, and analyze (code units, executed source lines, flow decisions, DML, SOQL, limits) |
| `run_apex_tests` | Run test classes on a non-production org |
| `workspace_diff` | Local edits as a unified diff against the org's version; saved to `fix.diff` |
| `evidence_index` | Every saved artifact of a ticket with a one-line description and the string to cite |
| `write_template`, `validate_report`, `validate_comment`, `export_report_html` | Report and comment: template, checks, HTML copy |
| `list_changes`, `audit_log` | What the tool changed, and every write attempt including the blocked ones |

Write tools ask every time and are refused on production:

| Tool | Mode | Notes |
|---|---|---|
| `set_trace_flag` | both | Diagnostic. Creates or extends a trace flag (and one debug level) for a user |
| `deploy_component` | apply | Commits your edit, snapshots the org's version, stops on drift, deploys, verifies, returns a `change_id`. Up to 5 components per call. `validate_only` for a check-only deploy |
| `rollback_change` | apply | Restores the snapshot of a `change_id` (or deletes a component the change created; or restores record values). Stops if the org changed again afterwards, unless `force` |
| `update_records` | apply | At most 10 records; previous values kept for rollback |
| `execute_anonymous_apex` | apply | Script and log are saved. Cannot be rolled back automatically |
| `run_apex_tests_on_production` | both | Only with `allow_apex_tests_on_production: true` |

### Calling a read-only tool from the command line

For debugging, without Claude Code:

```
python scripts/call_tool.py --list
python scripts/call_tool.py org_info org=demo-prod
python scripts/call_tool.py soql_query org=demo-prod query="SELECT Id, Priority FROM Case" limit=5
```

Write tools are refused there, always. They run only through Claude Code, where each call asks.

## Using `core/` from another project

`core/` depends only on the standard library and knows nothing about Claude Code or MCP:

```python
from pathlib import Path
from core.app import Investigator
from core.errors import SfInvestigatorError

inv = Investigator(root=Path("/path/to/a/project/root"))   # holds sfti.config.json, tickets/, audit/
try:
    info = inv.org_info("my-sandbox")
    rows = inv.soql_query("my-sandbox", "SELECT Id, Priority FROM Case", ticket_key="ABC-1", save_as="cases")
except SfInvestigatorError as exc:
    print(exc.to_dict())      # {"type", "code", "message", "hint"}
```

Every method returns JSON-serializable data and raises `SfInvestigatorError` subclasses. The
guards travel with the package: a front end that calls `inv.deploy_component(...)` gets the same
production block, mode check and audit row. What it must add itself is the **approval step**,
which here is Claude Code's permission prompt; pass who approved as `approved_by`.

## Tests

```
python -m pip install -e ".[dev]"
python -m pytest
```

No org, no Salesforce CLI, no Claude Code and no network are needed. Integration tests start the
simulator's `sf` launcher exactly as the wrapper would start the real CLI; the web interface is
tested through a real HTTP server on a free local port, with a stand-in for Claude Code. `.github/workflows/tests.yml` runs the
suite on Windows and macOS, then builds the release zip and has it set itself up from an empty
folder (`python scripts/check_release.py`).

The suite works in temporary folders only. A check that spans the whole run fails it if a file
in `tickets/`, `knowledge/` or `audit/` of the checkout changed, so running the tests next to
your real tickets is safe.

To build the zip to hand to someone: `python scripts/make_release.py` writes
`dist/salesforce-ticket-investigator-<version>.zip`. It is built from an explicit list, never
from "everything in the folder", and is refused if `sfti.config.json` is not in its default state.

After adding or renaming a tool in `mcp_server/server.py`, regenerate the permission lists, which
are built from the server's own tool lists:

```
python scripts/generate_settings.py
```

Design decisions and the list of open points are in [`docs/DECISIONS.md`](DECISIONS.md); the
original plan is in [`docs/PLAN.md`](PLAN.md).
