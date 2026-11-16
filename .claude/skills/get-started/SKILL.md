---
name: get-started
description: Walk a new user through setting up the Salesforce Ticket Investigator and their first ticket, one step at a time: check the tools on their computer, connect a Salesforce org with the exact command, then ask for the ticket. Use when the user says they are new, asks how to start or how to connect their org, when no org is connected, or types /get-started.
---

# Get started

Goal: take someone who has never used this project to their first investigated ticket, without
assuming they know the Salesforce CLI or Claude Code. One step at a time: do the step, say what
you found in a sentence or two, and only then move on. Never dump all the steps at once.

Speak plainly. The reader may be a support lead or an admin, not a developer. Give every command
in a code block, complete, ready to copy. Say where to run it ("in a terminal, not here").

## Step 1: what is on this computer

1. Run `python -m core.prereqs` (it is allowed without a prompt). If `python` is not found, try
   `python3 -m core.prereqs`.
2. Report only what matters: everything is fine, or what is missing and the one command that
   fixes it:
   - Salesforce CLI missing: `npm install --global @salesforce/cli`, then open a new terminal.
   - `mcp` package missing: `python -m pip install -e ".[dev]"` in the project folder.
   - git missing: point to https://git-scm.com/downloads.
3. `get_config`: tell them the mode in one line. `advise` means "I find the cause and hand you
   the fix; you apply it."

If the `sf-investigator` tools are not available at all, say so first: the MCP server did not
start. Ask them to run `/mcp` and, if it shows the server as failed, to follow "Troubleshooting"
in the README (usually the project is not installed in the Python that Claude Code uses).

## Step 2: connect a Salesforce org

1. `list_orgs`. For each org, `org_info`, and present a short table: alias, what it is
   (production, sandbox, scratch, Developer Edition), and what you may do there.
2. If no org is connected, or the one they need is missing, ask which kind of org it is and give
   the exact command for **their own terminal** (you never run `sf` yourself):

   | Org | Command |
   |---|---|
   | Sandbox | `sf org login web --alias my-sandbox --instance-url https://test.salesforce.com` |
   | Production | `sf org login web --alias my-prod` |
   | Developer Edition | `sf org login web --alias my-dev` |
   | Custom domain | `sf org login web --alias my-org --instance-url https://THEIR-DOMAIN.my.salesforce.com` |

   Explain what happens: a browser window opens, they log in to Salesforce and allow access, and
   they can close it. Ask them to tell you when it is done, then `list_orgs` again and confirm.
   Suggest a login that has only the access the investigation needs.
3. State the safety rule once, in one sentence: production is read-only here and that cannot be
   changed; anything that changes a sandbox asks them first.
4. No org at hand, or they only want to see how it works: offer the demo orgs. They run
   `python -m simulator setup` and follow what it prints, or simply `python -m webapp --demo`.
   Say plainly that those orgs are simulated and do not execute Apex.

## Step 3: the ticket

1. Ask them to paste the whole ticket: description, **every comment**, history. Mention that
   screenshots and files can be dropped into `tickets/<KEY>/attachments/` once the folder exists.
2. If the optional Jira reader is on (`get_config` shows `jira.enabled`), they can give a key
   instead and you call `jira_get_ticket`.
3. Ask which org the problem happens in, and whether a sandbox is available for tests, unless the
   ticket already says so.
4. Hand over to `intake-ticket`, then `investigate-ticket`.

## Also mention, once

- **The web page.** `python -m webapp` opens a page that does the same set-up checks, takes the
  ticket in a form, shows the progress and the result, and shows what has been learned about each
  org. It is the easier way to show the tool to someone else.
- **It gets better with use.** What an investigation learns about an org is remembered for the
  next ticket on that org, and is checked against the org before it is relied on.

## Do not

- Run `sf` yourself, or ask for a password, token or security code. Logging in happens in their
  browser.
- Move past a failed step. If the CLI is missing, the next step is installing it, not the ticket.
- Explain the architecture. They asked how to start.
