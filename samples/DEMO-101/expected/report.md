# Investigation report: DEMO-101

| | |
|---|---|
| Ticket | DEMO-101: Premium customer cases are not set to High priority |
| Org investigated | `demo-prod` (00DWs000000PrdAMAS) for records, code and the debug log; `demo-sandbox` (00DWs000000SbxAMAS) for the test run |
| Org classification | `demo-prod`: production, read-only. `demo-sandbox`: sandbox |
| Mode | advise |
| Root cause status | Confirmed |
| Report date | 2026-11-15 |

> **Simulated orgs.** `demo-prod` and `demo-sandbox` are the simulated orgs created by
> `python -m simulator setup`. The simulator serves metadata, records and a recorded debug log, and
> it does not execute Apex: the Apex test outcomes below come from the simulator's rules, not from
> a Salesforce runtime. The evidence chain itself (records, source lines, log lines) is read exactly
> as it would be from a real org.

## Ticket summary

Since the nightly case import went live (first run 10 November 2026, 02:00), some cases for
Premium accounts are created with Priority Medium instead of High. Cases created by agents are
correct, some imported cases are correct, and the import of 12 November was entirely correct.
Re-saving a wrong case does not correct it. The reporter's team suspected the
Case Premium Notification flow.

## Investigation steps

1. **Scope.** `get_config`: mode advise. `org_info`: `demo-prod` is production (read-only),
   `demo-sandbox` is a sandbox. `search_tickets` found no earlier ticket on this subject.
2. **Records.** Queried every case created since 9 November with its account tier, creator and
   creation time (E1), then only the Premium-account cases whose priority is not High (E2).
3. **What runs on Case.** `describe_object` with automation: one trigger (`CaseTrigger`, before
   insert and before update), one record-triggered flow (`Case_Premium_Notification`, after save,
   on create), no validation rule, no workflow rule.
4. **Source.** Retrieved the trigger, its handler, the service, the test class and the flow from
   `demo-prod` (metadata commit `fe98962`) and read the path for a case insert (E3 to E7).
   `scan_apex` gave one lead, a `return` inside a loop; it was read in context before being used.
5. **Debug log.** Downloaded the log of the 11 November import and read which statements ran
   (E8 to E12).
6. **Timeline.** Setup audit trail and last-modified dates since 3 November (E13, E14, E15).
7. **Dependencies.** `find_dependencies` on `CaseEscalationService`, confirmed with
   `search_metadata` (E16).
8. **Tests.** Ran the existing test class on `demo-sandbox` (E17) after checking that the sandbox
   holds the same class versions as production (E14, E18).
9. **Fix.** Drafted the change locally and produced `fix.diff`. Nothing was deployed: advise mode.

Hypotheses and how each was decided:

| # | Hypothesis | Result | Decided by |
|---|---|---|---|
| H1 | The priority rule in Apex does not handle a batch that mixes Premium and non-Premium accounts | **Confirmed** | E1, E5, E8 to E10 |
| H2 | The Case Premium Notification flow is broken or changes the priority | Ruled out | E7, E11, E12, E15 |
| H3 | The import bypasses the trigger, or sets Priority itself | Ruled out | E8, E10, and three imported cases are High in E1 |
| H4 | The affected accounts are not Premium at the time of creation | Ruled out for the current data | E1, E9 |
| H5 | A change deployed just before the first failure introduced the defect | Open, see Open questions | E13, E14 |

## Evidence

Paths are relative to `tickets/DEMO-101/`. Source citations refer to the version retrieved from
`demo-prod` (metadata commit `fe98962`); the working tree now also holds the proposed fix, shown in
`fix.diff`, which replaces line 30 of the service.

| # | Artifact | What it shows |
|---|---|---|
| E1 | `queries/20261115-115410-cases-since-9-nov.json` | 10 cases since 9 November. The 7 created by the integration user (005Ws000001IntgIAB) fall into three batches with identical creation times: 10 Nov 02:00:14 (4 cases), 11 Nov 02:00:09 (2 cases), 12 Nov 02:00:11 (2 cases). In case-number order, every Premium case that comes after a Standard-account case of its batch is Medium (500Ws00000C0003IAA, 500Ws00000C0004IAA, 500Ws00000C0006IAA); every Premium case with no Standard case before it is High (500Ws00000C0001IAA, 500Ws00000C0009IAA, 500Ws00000C0010IAA). The 12 November batch contains no Standard case. The two agent-created cases are High. |
| E2 | `queries/20261115-115449-premium-cases-not-high.json` | Exactly 3 cases belong to a Premium account and are not High: the three named above. |
| E3 | `metadata/force-app/main/default/triggers/CaseTrigger.trigger:5-7` | On before insert the trigger passes the whole batch (`Trigger.new`) to `CaseTriggerHandler.beforeInsert`. |
| E4 | `metadata/force-app/main/default/classes/CaseTriggerHandler.cls:6-8` | `beforeInsert` passes the whole list to `CaseEscalationService.applyPremiumPriority`. |
| E5 | `metadata/force-app/main/default/classes/CaseEscalationService.cls:26-33` | The loop over the batch. For a case whose account is missing or not Premium, line 30 executes `return;`, which leaves the method. `c.Priority = HIGH_PRIORITY;` on line 32 is therefore never reached for any later case of the batch. |
| E6 | `metadata/force-app/main/default/classes/CaseTriggerHandler.cls:10-20` | On update, the rule is re-applied only to cases whose `AccountId` changed (line 14). Re-saving a case without changing its account does not re-evaluate the priority. |
| E7 | `metadata/force-app/main/default/flows/Case_Premium_Notification.flow-meta.xml:31-41` | The flow's only decision tests `$Record.Priority` equal to High and, if true, calls the notification action. The flow runs after save on create (lines 55-57) and contains no element that updates a record. |
| E8 | `logs/07LWs00000L0001MAA.log:4` | In the import transaction of 11 November, `CaseTrigger` ran on before insert: the import does not bypass the trigger. |
| E9 | `logs/07LWs00000L0001MAA.log:22` | In the second loop, the first case's account is 001Ws00000A1b2dIAB with Support Tier Standard. |
| E10 | `logs/07LWs00000L0001MAA.log:23-25` | Statement 28 (the tier test) and then statement 30 (`return;`) execute, and the next event is the exit of `applyPremiumPriority`. The log, captured with Apex code at FINEST (line 1), contains no execution of statement 32. |
| E11 | `logs/07LWs00000L0001MAA.log:40` | The insert saves 2 cases, after the trigger finished. |
| E12 | `logs/07LWs00000L0001MAA.log:44-51` | The flow then runs once per case and its `High_Priority` outcome evaluates to false both times (lines 46 and 51): it sees the Medium priority that the trigger left in place. |
| E13 | `queries/20261115-115436-setup-audit-trail.json` | Setup changes since 3 November: the connected app for the nightly import was created on 9 November 11:20 and the Apex class `CaseEscalationService` was changed on 9 November 16:42, both before the first import run. |
| E14 | `queries/20261115-115436-apex-class-last-modified.json` | `CaseEscalationService` was last modified on 9 November 16:42; the handler and the test class on 18 August. |
| E15 | `queries/20261115-115436-flow-versions.json` | The only flow version is version 2, active, last modified on 20 October: the flow did not change in November. |
| E16 | `metadata/force-app/main/default/classes/CaseTriggerHandler.cls:19` | The second and last caller of `applyPremiumPriority` (the first is E4). `find_dependencies` lists the handler and the test class as the only components using the service. |
| E17 | `queries/20261115-115436-apex-tests.json` | On `demo-sandbox`, `CaseEscalationServiceTest` passes: 2 tests, 0 failures. |
| E18 | `queries/20261115-115436-sandbox-apex-class-last-modified.json` | The sandbox holds the same last-modified dates for the three classes as production (E14). |
| E19 | `metadata/force-app/main/default/classes/CaseEscalationServiceTest.cls:10-34` | The two existing tests each insert a single case. Neither inserts a batch that mixes account tiers. |

## Root cause

`CaseEscalationService.applyPremiumPriority` stops processing the whole batch at the first case
whose account is not Premium: line 30 is `return;` inside the loop over the batch, where the
comment above it ("nothing to do" for this case) describes a `continue` (E5). Every Premium case
that comes after such a case in the same insert keeps the priority it arrived with.

Evidence chain:

1. **Symptom in data.** The only Premium cases that are not High are three imported cases, and
   each one follows a Standard-account case in its import batch (E1, E2).
2. **Code that produces it.** The trigger hands the whole batch to the service (E3, E4), and the
   service returns from the method at the first non-Premium case (E5).
3. **Observed in the failing transaction.** In the 11 November import, the first case belongs to
   a Standard account (E9), statement 30 executes, the method exits and statement 32 never runs
   (E10). The Premium case inserted in the same batch, 500Ws00000C0006IAA, is Medium (E1, E11).
4. **Why only sometimes.** A case created by hand is a batch of one, so the early return has
   nothing left to skip. An imported batch is affected only when a non-Premium case precedes a
   Premium one: the 12 November batch contains only Premium cases and is correct (E1).
5. **Why re-saving does not help.** The update path re-applies the rule only when the account
   changes (E6).
6. **Why the notification is missing.** The flow notifies only when Priority is High (E7). It
   evaluated to false for both cases of the 11 November batch because the trigger had left them at
   Medium (E12). The missing notification is a consequence of this defect, not a second defect.
   The flow does not write Priority (E7) and did not change in November (E15).

One point is not established by an artifact and is labelled accordingly. **Hypothesis:** the
position of each case inside the 10 November batch is taken from case-number order (E1); there is
no debug log for that run. The 11 November batch, for which the log exists, does not depend on
this.

## Fix

Change one statement in `CaseEscalationService.cls` and add the missing test.

```diff
--- a/force-app/main/default/classes/CaseEscalationService.cls
+++ b/force-app/main/default/classes/CaseEscalationService.cls
@@ -27,7 +27,7 @@
             Account account = accountsById.get(c.AccountId);
             if (account == null || account.Support_Tier__c != PREMIUM_TIER) {
                 // Not a premium customer: nothing to do.
-                return;
+                continue;
             }
             c.Priority = HIGH_PRIORITY;
         }
```

The full diff, including the new test method `mixedBatchEscalatesEveryPremiumCase` in
`CaseEscalationServiceTest`, is in `fix.diff`.

**Why this layer.** The rule already lives in `CaseEscalationService`, and the defect is one
statement in it. Correcting that statement keeps a single place that decides the priority. Moving
the rule into the flow, or adding a second automation, would leave two places to maintain and
would not remove the faulty loop.

**Impact.** The method has two callers, both in `CaseTriggerHandler` (E4, E16); the test class is
the only other component that references the service (E16). For a batch of one, and for a batch
whose cases are all Premium or all non-Premium, behaviour is unchanged. For a mixed batch, Premium
cases after a non-Premium case now become High, on insert and on an account change. The flow needs
no change: once the priority is High before save, its decision is true and it notifies (E7).

**What you change (advise mode: nothing was deployed).**

1. In `CaseEscalationService.cls`, line 30: replace `return;` with `continue;`.
2. In `CaseEscalationServiceTest.cls`: add the test method from `fix.diff`.
3. Deploy both classes together to `demo-sandbox`, running `CaseEscalationServiceTest`.
4. Promote to production through your normal release path.

**Existing records.** A code fix does not repair the three cases already saved with the wrong
priority (E2): 500Ws00000C0003IAA, 500Ws00000C0004IAA, 500Ws00000C0006IAA. This is a data
correction, proposed separately from the code change: set Priority to High on those three cases
after confirming with the support lead that none was deliberately lowered. Re-run the query of E2
first, because imports before the fix can add more.

## Test results

- **Existing tests, `demo-sandbox`:** `CaseEscalationServiceTest`, 2 tests, both pass (E17). They
  pass with the defect present because neither covers a mixed batch (E19). That gap is how the
  defect reached production unnoticed by the test suite.
- **New test:** `mixedBatchEscalatesEveryPremiumCase` inserts a Standard-account case followed by
  a Premium-account case in one statement and asserts Medium and High. It was not run: the tool is
  in advise mode, and the simulated org does not execute Apex.
- **Reproduction:** not executed in the sandbox for the same reasons. The failing behaviour is
  taken from the production debug log (E8 to E12) rather than from a new run.

Test plan after you deploy to the sandbox:

1. Run `CaseEscalationServiceTest`: expect 3 passing tests. Without the code change, the new test
   fails on its second assertion.
2. Insert two cases in one API call, the first for a Standard account and the second for a
   Premium account, without Priority: expect Medium and High, and one notification.
3. Insert one case by hand for a Premium account: expect High, as today.
4. After the next nightly import, re-run the query of E2: expect no new rows.

## Risks

- **Low code risk.** One statement; no signature, query or DML changes.
- **More cases become High** than in the last days, which is the intended rule, so more
  notifications are sent by the flow.
- **Data already affected** stays wrong until the separate data correction is done.
- **Until the fix is in production,** every nightly import that mixes tiers adds wrong cases. The
  manual morning correction has to continue until then.

## Open questions

1. **Did the 9 November change introduce the `return`?** The class was modified on 9 November
   16:42, the evening before the first failing import (E13, E14). **Hypothesis:** that change
   introduced the early return. It is not confirmed: the org holds only the current version of the
   class. To confirm it, compare with the previous version in your source control or deployment
   history. The root cause above does not depend on the answer.
2. **Are older mixed-batch inserts affected?** The queries cover cases created since 9 November.
   If other bulk loads into Case ran since the class changed, widen the query of E2.
3. **Sandbox equality.** E18 compares last-modified dates, not source text. Retrieve the service
   from `demo-sandbox` before deploying if the sandbox was refreshed or edited recently; the
   deploy tool also checks for drift.
