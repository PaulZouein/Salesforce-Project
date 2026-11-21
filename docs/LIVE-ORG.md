# Testing with your Developer Edition org

The investigator has so far been run only against simulated orgs. This guide checks it on a real
Developer Edition org, in one of two ways:

- **On your own computer**, parts 1 to 5 below. The investigator reaches Salesforce through the
  Salesforce CLI, which logs in through your browser.
- **In Claude's cloud session**, where the tool is being built: see
  [Letting a cloud session use your org](#letting-a-cloud-session-use-your-org). You log in, on
  your computer; the session gets an authorization you paste into the investigator's page. The
  project's rules stop the assistant from running the Salesforce CLI itself, and that rule is not
  worked around, not even for a test org.

Pick how far you go. Each part is useful on its own.

| Part | Time | What it shows | Changes your org? |
|---|---|---|---|
| [1. Connect](#1-connect-the-org-once) | 2 min | the org connected to the Salesforce CLI | No |
| [2. Live check](#2-the-live-check-read-only) | 5 min | every read-only tool against a real org | No |
| [3. Put the sample bug in your org](#3-put-the-sample-bug-in-your-org) | 10 min | the bug, for real: real Apex runs | Yes: adds a field, 3 classes, a trigger, a permission set, 7 records |
| [4. Investigate it from the page](#4-investigate-it-from-the-page) | 5 min | a whole investigation on a real org | No |
| [5. Let it apply the fix](#5-optional-let-it-apply-the-fix) | 10 min | deploy, real Apex tests, rollback | Yes, each step after you approve it |

What to send back: the live check report (part 2) and the result zip of the investigation
(part 4). Neither contains record values, names or email addresses; read them before sending.

> **Not tried against a real org yet.** These steps follow the Salesforce CLI's documentation and
> were checked against the simulated orgs. If a command fails, its exact error message is the most
> useful thing you can send.

Use a Developer Edition org that holds nothing you need. Before you start: Python, the Salesforce
CLI and Claude Code installed (see `QUICKSTART.md`), and the investigator started once with
`start.bat` or `start.command`, which creates its `.venv` folder.

## 1. Connect the org (once)

In a terminal:

```
sf org login web --alias my-dev
```

Your browser opens on `login.salesforce.com`. Log in with your Developer Edition username and
press **Allow**. Then check:

```
sf org display --target-org my-dev
```

It should say `Connected Status  Connected`. The investigator will call this org `my-dev`.

## 2. The live check (read-only)

In a terminal opened in the project folder:

| Windows | macOS |
|---|---|
| `.venv\Scripts\python scripts\live_check.py --org my-dev` | `.venv/bin/python scripts/live_check.py --org my-dev` |

It takes a minute or two and changes nothing in the org. It runs every read-only tool once (what
kind of org it is, the automation on Case, a query, Tooling queries, recent changes,
dependencies, a retrieve, debug logs, field history, the org memory) and writes
`live-check/live-check-<date>-<time>.md`. Send that file.

After part 3, run it again with `--tests CaseEscalationServiceTest` at the end: it then also runs
the sample's Apex tests in your org (allowed on a Developer Edition org, refused on production).

## 3. Put the sample bug in your org

The sample is the bug of the demo: a `return` where a `continue` was meant, so that one Standard
case in a batch stops the Premium cases after it from getting High priority. In your org, the
Apex code really runs.

From the project folder, deploy the field, the classes and the trigger:

```
cd samples/planted-bug
sf project deploy start --source-dir force-app/main/default/objects --source-dir force-app/main/default/classes --source-dir force-app/main/default/triggers --target-org my-dev
```

Then give yourself access to the new field (a field deployed this way is visible to nobody until
access is granted):

```
cd ../live-org
sf project deploy start --source-dir force-app --target-org my-dev
sf org assign permset --name Ticket_Investigator_Sample --target-org my-dev
```

The sample's flow is left out on purpose: it sends a custom notification, which needs a
notification type and recipients that the sample does not set up, and the bug is not in it.

Turn on a debug log for yourself, so that the investigator has a log of the import: **Setup**,
search **Debug Logs**, **New**, Traced Entity Type **User**, your user, Debug Level
**SFDC_DevConsole**, **Save**.

Now import the sample data, still in `samples/live-org`: three accounts, then four cases in one
batch. The batch is what triggers the bug.

```
sf data import tree --plan data/plan.json --target-org my-dev
```

In the org, the case "Order 5001" should be High, "Order 5002" Medium (its account is Standard,
so that is right), and "Order 5003" and "Order 5004" Medium: those two are the bug.

## 4. Investigate it from the page

1. Start the investigator with `start.bat` or `start.command` (not the demo).
2. **Get started**: `my-dev` is listed as a Developer Edition org. The investigator only reads
   from it.
3. **New ticket**: paste the text of `samples/live-org/ticket.txt`, choose `my-dev`, and
   **Save and continue**.
4. **Investigate with Claude Code**. Tick **Use fewer tokens** if you like.
5. When it is done, **Download the result** and send the zip, with what the usage box says it
   cost.

## 5. Optional: let it apply the fix

This is the full loop with real Apex tests: deploy the fix to your dev org, run the tests, check
the records, then roll back. Nothing happens without your approval of each step.

1. In `sfti.config.json`, set `"mode": "apply"` and `"allow_developer_edition_writes": true`
   (you edit this file yourself; the assistant cannot).
2. Open Claude Code in the project folder: `start.bat --claude` (Windows) or `sh start.command --claude` (macOS), in a terminal in the folder.
3. Type: *Continue LIVE-1 in apply mode on my-dev: deploy the fix, run CaseEscalationServiceTest,
   check the cases, then roll back the deploy.*
4. Approve or refuse each step when it asks.
5. Set the two settings back to `"advise"` and `false`.

## Letting a cloud session use your org

*Advanced, and only for Claude Code cloud sessions. On your own computer, parts 1 to 5 are all you need.*

A Claude Code cloud session runs in a container with no browser, and its network reaches only
the hosts its environment allows. The Salesforce CLI 2.158 no longer has a code-based (device)
login. So the session uses the same route as any computer without a browser: you log in on your
computer and paste the resulting authorization into the investigator's page running in the
session.

1. On your computer, with the org connected as in part 1, show its authorization:

   ```
   sf org auth show-sfdx-auth-url --target-org my-dev
   ```

   Keep the line that starts with `force://`. It works like a password: never paste it in the
   chat.
2. The session needs to reach your org's address and nothing else: the part after `@` in that
   line, also shown in Salesforce under **Setup → My Domain** (for example
   `yourname-dev-ed.develop.my.salesforce.com`). Claude asks you to allow it with an approval
   card; that changes the session's network settings, which only you can do.
3. Claude starts the page in the session (`python -m webapp --no-browser`, with `--allow-host`
   for the preview's name if the preview needs it) and tells the Claude app its port. Open the
   preview, then **Get started → Connect an org → No browser on the computer that runs the
   investigator?** Type the name `my-dev`, paste the line, press **Connect**.
4. Tell Claude it is connected. It checks the read-only tools against your org through its own
   investigator tools, never with the Salesforce CLI directly. Writes to a Developer Edition
   org stay blocked unless you set `"allow_developer_edition_writes": true` in
   `sfti.config.json` yourself, and each one still asks you.

To end it: the session's container is discarded when the session ends. To cut the access at
once, revoke it in Salesforce: your personal settings, **Advanced User Details**, **OAuth
Connected Apps**, **Revoke** next to the Salesforce CLI. That also signs out the Salesforce CLI on
your own computer for this org; log in there again with part 1.

## Removing the sample afterwards

In your dev org: delete the cases "Order 5001" to "Order 5004" and the three "Sample" accounts.
Then in **Setup**: delete the trigger `CaseTrigger` (Apex Triggers), the classes
`CaseEscalationServiceTest`, `CaseTriggerHandler` and `CaseEscalationService` (Apex Classes, in
that order), the permission set **Ticket Investigator Sample**, and the field **Support Tier** on
Account (Object Manager). On your computer, `tickets/LIVE-1`, `tickets/LIVE-CHECK` and
`live-check/` can be deleted, and `sf org logout --target-org my-dev` forgets the login.
