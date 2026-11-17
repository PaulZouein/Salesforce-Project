# Testing with your Developer Edition org

Everything so far was checked against simulated orgs. Your Developer Edition org can check it
against the real thing. You do the steps on your own computer, because:

- the investigator reaches Salesforce through the Salesforce CLI, which logs in through your
  browser. That works on your computer;
- the cloud session where the tool was built cannot reach Salesforce (its network does not allow
  it), and the project's rules stop the assistant from running the Salesforce CLI itself. That
  rule is what keeps production read-only, so it is not worked around, not even for a test org.

Pick how far you go. Each part is useful on its own.

| Part | Time | What it shows | Changes your org? |
|---|---|---|---|
| [1. Connect](#1-connect-the-org-once) | 2 min | | No |
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
2. Open Claude Code in the project folder: `claude-here.bat` or `claude-here.command`.
3. Type: *Continue LIVE-1 in apply mode on my-dev: deploy the fix, run CaseEscalationServiceTest,
   check the cases, then roll back the deploy.*
4. Approve or refuse each step when it asks.
5. Set the two settings back to `"advise"` and `false`.

## Removing the sample afterwards

In your dev org: delete the cases "Order 5001" to "Order 5004" and the three "Sample" accounts.
Then in **Setup**: delete the trigger `CaseTrigger` (Apex Triggers), the classes
`CaseEscalationServiceTest`, `CaseTriggerHandler` and `CaseEscalationService` (Apex Classes, in
that order), the permission set **Ticket Investigator Sample**, and the field **Support Tier** on
Account (Object Manager). On your computer, `tickets/LIVE-1`, `tickets/LIVE-CHECK` and
`live-check/` can be deleted, and `sf org logout --target-org my-dev` forgets the login.
