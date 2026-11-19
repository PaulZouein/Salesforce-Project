# How it stays safe

The rules in [CLAUDE.md](../CLAUDE.md) are enforced in code. This is how, layer by layer, and what
each layer does not guarantee. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

- [Safety model](#safety-model)
- [Permission settings and the guard](#permission-settings-and-the-guard)

## Safety model

Three independent gates stand in front of every write to an org:

| Gate | Where | What it does | Can it be switched off? |
|---|---|---|---|
| 1. Org classification | `core/org_guard.py`, `core/sf_cli.py` | Reads `Organization.IsSandbox` and `OrganizationType` live before every write. Anything that is not provably a sandbox, a scratch org or a Developer Edition org is production, including an org that cannot be inspected. Production is refused, and so is a Developer Edition org with a namespace (the source of a managed package). | No. There is no parameter, setting or environment variable. |
| 2. Mode | `sfti.config.json` | Deploys, record updates, anonymous Apex and rollbacks need `apply`. | By you, in the config file. |
| 3. Approval | `.claude/settings.json` | Every write tool is in the `ask` list: Claude Code shows the tool and its exact arguments and waits for you. | Only by changing the settings or running Claude Code with permission prompts bypassed. |

How gate 1 is made structural rather than a convention:

- `core/sf_cli.py` classifies every `sf` command before running it. A fixed allowlist of read
  commands, with the exact flags allowed on each, runs freely. **Everything else is a write**,
  including commands the wrapper has never seen.
- A write runs only with a permit. Permits are issued only by `org_guard`, only for an org that
  classifies as non-production, and cover a fixed number of commands. The permit class itself
  refuses to be created for a production classification.
- A permit is bound to the org's **username**, read when the org was checked, and every permitted
  command runs against that username. An alias can be pointed at another org at any moment (in
  another terminal, or by connecting an org under a name already in use); a username cannot. A
  permitted command must name its org exactly once, as `--target-org`.
- Every attempt is written to a local audit log (`audit/audit.db`, not committed), including the
  blocked ones. `audit_log` and `list_changes` show it.

Other guards in `core/`:

- **Secrets.** Access tokens and similar fields are stripped from everything the CLI returns
  before it is returned, logged or put in an error.
- **Row limits and read-only queries.** Only a single `SELECT` runs. `FOR UPDATE`, `FOR VIEW`,
  `FOR REFERENCE`, `UPDATE TRACKING` and `UPDATE VIEWSTAT` are refused: each makes a read change the
  org (a lock, last-viewed dates, Knowledge statistics), and the read tools run without approval.
  `COUNT()` and other single-row aggregates are left without `LIMIT`, which would cap the count.
- **Redaction.** Values of fields that look like personal data (email, phone, address, birthdate,
  names on Contact, Lead and User, and more) are replaced with `[REDACTED]`. So are the name of the
  record behind an owner or other polymorphic lookup, the names of person accounts, grouped
  (aggregate) columns taken from such fields, the names row of a lookup's field history, the
  descriptions of user-management entries in the setup audit trail, and e-mail addresses in text
  quoted from debug logs. `reveal_fields` shows a value in the chat when the value itself is the
  bug; files saved under `queries/` stay redacted regardless.
- **Debug logs.** A log Salesforce cut off at its size limit is flagged as truncated, with the
  warning that a statement missing from it may still have run.
- **Path confinement.** File tools only read inside `tickets/<KEY>/`, and never inside `.git`.
- **Report check.** `validate_report` fails a report whose citations do not resolve to a real
  file and line, whose confirmed root cause cites no evidence, or which contains an email address
  (only `example.com`-style and system mailboxes pass: the `.invalid` addresses of a refreshed
  sandbox are real people's), and warns about phone numbers.
- **The local page.** Report text is escaped before it is shown, links included. The page runs
  only its own script file, never inline script, and exported reports are served in a sandbox with
  no script at all. An investigation started from the page gets the read-only tools only, no shell
  and no web access, runs in Claude Code's `dontAsk` mode, and may write files only under
  `tickets/`.
- **Nothing is posted.** No tool sends anything to a ticket system, chat or email. The optional
  Jira reader only sends `GET` requests.

### The hook, and what it does not guarantee

`.claude/hooks/guard.py` is a PreToolUse hook, installed by the committed
[permission settings](#permission-settings-and-the-guard). It blocks three things:

1. running `sf` or `sfdx` directly from Bash (also through `npx`, a path, or after `&&`);
2. calling the core write functions from a Python one-liner in Bash, which would skip the
   approval prompt;
3. editing `sfti.config.json`, `.mcp.json`, `.claude/settings.json`, `.claude/hooks/` and the
   audit database.

It is **pattern matching, not a sandbox**. It stops mistakes and shortcuts; a disguised command
gets past it (`bash -c '…'`, `eval`, the CLI's JavaScript run with `node`, a script file that
calls the CLI). It also has one known false positive: a Bash command that both reads a `.py` file
and names a write function (for example `grep deploy_component core/changes.py`) is blocked.

The hard guarantee, no writes to production, is gate 1 above, and it covers everything that goes
through `core/`: every tool of the MCP server. A command that runs the Salesforce CLI itself does
not go through `core/`. In a normal session such a command waits for your approval, because Bash
commands ask; with permission prompts bypassed, only this hook stands in its way. So in a session
with prompts bypassed, do not stay logged in to a production org with the Salesforce CLI.

The hook is built so that it cannot lock a session: the command does nothing when the script is
missing, and the script exits 0 on any internal error.

### If you run Claude Code with permission prompts bypassed

Gate 3 disappears: write tools no longer wait for you. Gates 1 and 2 are unaffected for the
tools, so production stays read-only through them and advise mode still refuses fix-applying
writes. Bash commands no longer ask either, so a command that runs the Salesforce CLI directly is
stopped only by the hook (see above). Do not combine
bypassed permissions with `apply` mode unless the only non-production orgs you are logged in to
are ones you are happy to see changed without being asked.

## Permission settings and the guard

`.claude/settings.json` holds the generator's output for the MCP server's own tool lists
(`scripts/generate_settings.py`), with the guard on. A test fails when the committed file and the
generator disagree.

| What | How it is set |
|---|---|
| Write tools | all 6 in `ask`: every call prompts |
| Read-only tools | all 36 in `allow` |
| Direct `sf` / `sfdx` in Bash | denied by permission rule and by the hook |
| Guard hook | installed for Bash, Edit and Write |
| Edits to `sfti.config.json`, `.mcp.json`, `.claude/settings.json`, `.claude/hooks/` | denied by permission rule and by the hook |
| Production write block, mode gate, redaction, limits | in `core/`, independent of these settings |

The guard applies to the assistant in this folder: it cannot edit those four safety files. You
can, in your own editor or terminal.

```
python scripts/generate_settings.py            # regenerate after adding or renaming a tool
python scripts/generate_settings.py --check    # is the committed file up to date?
python scripts/generate_settings.py --no-guard # same permission lists, no hook, no edit protection
```

Use `--no-guard` only while you want the assistant to work on the tool itself (its settings, its
hook or the config file), and regenerate with the guard afterwards.

How the guard came to be on: the session that finished the build was not permitted to write
`.claude/settings.json` and stopped there, leaving it to you. The session that started the build,
acting on your brief ("read-only tools allowed without prompting; every write tool set to always
ask"), then committed the file. It could not run the generator, so the file was written by hand
to equal its output; the test above confirms the content is equal. If `--check` reports a
difference in formatting, run the generator once. See decisions 19 and 43 in
[`docs/DECISIONS.md`](DECISIONS.md).

Since then the file has been regenerated once by the generator itself, with the owner's explicit
go-ahead, to add the five org-memory tools to the `allow` list (decision 52). Nothing else in it
changed.
