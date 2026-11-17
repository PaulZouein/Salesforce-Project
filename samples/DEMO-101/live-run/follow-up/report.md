# Investigation report: DEMO-101

| | |
|---|---|
| Ticket | DEMO-101 |
| Org investigated | demo-prod (00DWs000000PrdAMAS) |
| Org classification | production (read-only; IsSandbox = False, Enterprise Edition) |
| Mode | advise |
| Root cause status | Confirmed |
| Report date | 2026-11-16 |

## Ticket summary

Since the nightly order import went live (9-10 November), some cases for Premium accounts get
Medium instead of High priority. Reported: 00001003, 00001004, 00001006. Cases created by agents
and the 12 November import (00001009, 00001010) are High. Saving a wrong case again does not
correct it. The support team also lacks the "high priority case" notification for these cases.

## Investigation steps

1. Mode is advise; `demo-prod` is production, so everything was read-only. Org memory held nothing for this topic.
2. `describe_object` Case: one trigger (`CaseTrigger`, before insert/update), one record-triggered flow (`Case_Premium_Notification`, after create), no validation or workflow rules.
3. Read the flow: it only notifies when Priority is High and does not change the case (E6, E7). It cannot set Priority; the missing notification is a consequence of Priority not being High (E8, flow decision result false).
4. Read `CaseTrigger` -> `CaseTriggerHandler.beforeInsert` -> `CaseEscalationService.applyPremiumPriority` (E3, E4, E1).
5. Queried the cases and their accounts (E2, E5), and read the 11 November import debug log (E8).
6. `recent_org_changes` (saved, E13): `CaseEscalationService` was changed on 2026-11-09 16:42 UTC, the day before the first wrong cases.
   Follow-up 01 (`attachments/follow-up-01.txt:3`): the integration developer confirms the import sends cases in case-number order within a batch, and that the 10 Nov import was one batch of four cases, 00001001 to 00001004. Re-queried Premium-account cases not High (E12).
7. `find_dependencies`: callers are `CaseTriggerHandler` and the existing test `CaseEscalationServiceTest`; retrieved and read the test (E9).

## Evidence

Paths are relative to `tickets/DEMO-101/`.

| # | Artifact | What it shows |
|---|---|---|
| E1 | `metadata/force-app/main/default/classes/CaseEscalationService.cls:26-33` | Loop over the batch. The org's version had `return;` at line 30 (inside the loop) where this working copy now has `continue;`; the original line is the `-` line in E10. A non-Premium case ends the whole method |
| E2 | `queries/20261116-184316-cases.json` | 00001003 and 00001004 (created 10 Nov 02:00:14) and 00001006 (11 Nov 02:00:09) are Medium with integration-user creator. 00001001 (same 10 Nov timestamp) and 00001009/00001010 (12 Nov) are High |
| E3 | `metadata/force-app/main/default/classes/CaseTriggerHandler.cls:6-8` | `beforeInsert` passes all new cases to `applyPremiumPriority` in one call |
| E4 | `metadata/force-app/main/default/triggers/CaseTrigger.trigger:4-7` | Trigger runs before insert, once per batch chunk |
| E5 | `queries/20261116-184320-account-tiers.json` | Account `001Ws00000A1b2cIAB` and `...2eIAB` are Premium; `...2dIAB` is Standard |
| E6 | `metadata/force-app/main/default/flows/Case_Premium_Notification.flow-meta.xml:25-31` | Flow decision: notify only if `$Record.Priority` = High |
| E7 | `metadata/force-app/main/default/classes/CaseTriggerHandler.cls:10-20` | On update, priority is only re-applied when `AccountId` changed, so a plain re-save does not repair a case |
| E8 | `logs/07LWs00000L0001MAA.log:22-25` | 11 Nov import: 2 cases, 2 account rows; first account read is Standard (line 22); execution then goes to statement [30] (line 24) and the method exits (line 25) with no Priority assigned |
| E9 | `metadata/force-app/main/default/classes/CaseEscalationServiceTest.cls:10-34` | Existing tests insert one case at a time, so none covers a mixed batch |
| E10 | `fix.diff` | The proposed change; shows original `return;` replaced by `continue;` |
| E11 | `logs/07LWs00000L0001MAA.log:46` | Flow decision `High_Priority` = false for the first interview (same at line 51 for the second): both cases left the trigger not High |
| E12 | `queries/20261116-191631-premium-not-high.json` | Current data: exactly three cases on Premium accounts are not High: 00001003, 00001004, 00001006 (all Medium) |
| E13 | `queries/20261116-191632-recent-changes-escalation.json` | Setup audit trail: `CaseEscalationService` Apex code changed 2026-11-09 16:42 UTC; no other change to it since 1 Nov |
| E14 | `attachments/follow-up-01.txt:3` | User-supplied: batch order is case-number order; 10 Nov import was one batch of 00001001-00001004 |

## Root cause

**Confirmed.** In `CaseEscalationService.applyPremiumPriority`, the loop over the batch exits the
whole method with `return;` when it meets a case whose account is not Premium (or has no
account row), instead of skipping only that case with `continue` (E1, E10). Every case after the
first non-Premium case in the same insert batch therefore keeps its incoming priority, which the
import leaves at the default Medium.

Chain:
1. Symptom in data: Premium-account cases are Medium only in the imports of 10 and 11 Nov (E2, E5). 00001003/00001004 (10 Nov) and 00001006 (11 Nov) are Premium-account cases; 00001002 and 00001005 are Standard-account cases (Medium is correct for them).
2. Pattern fits exactly: on 10 Nov the import was one batch of 00001001-00001004 in case-number order (E14, matching the shared timestamp in E2). 00001001 (Premium) is processed first and gets High; 00001002 (Standard) triggers the `return`, so 00001003/00001004 stay Medium (E2, E5). On 11 Nov the log confirms a two-case batch whose first account is Standard, and the method exits there (E8); 00001006 is the case after it. On 12 Nov both cases are Premium, no early exit, both High (E2). The batch order rests on the developer's statement (E14), not on a log of the 10 Nov run.
3. Why only the import: agents insert one case at a time (E2, 00001007/00001008 High), and a single-case insert cannot be preceded by a non-Premium case. The composite API batches many cases in one before-insert call (log shows both cases in one trigger run, E8).
4. Why re-saving does not fix it: update only re-applies the rule when `AccountId` changes (E7).
5. Notification flow ruled out as cause: it only reads Priority and changes nothing (E6); the decision evaluated false because Priority was not High (E11). No validation or workflow rule exists on Case (describe_object output, no saved artifact).
6. Timing: the class was last modified 2026-11-09 16:42 UTC (E13), the day before the first wrong cases. **Hypothesis**: the `return` was introduced in that change; no deployment record or version history exists (follow-up 01), so the earlier version could not be compared. That the new import is the first source of mixed batches is also a hypothesis; it does not affect the root cause.

## Fix

Change `return;` to `continue;` at line 30 of `CaseEscalationService.cls` (cited files carry this local edit; the original lines are in `fix.diff`), and add a mixed-batch test to the
org's existing `CaseEscalationServiceTest`. Full diff in `fix.diff`.

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

Plus `premiumCasesAfterStandardCaseInSameBatchAreHighPriority` in `CaseEscalationServiceTest` (see `fix.diff`).

Why this layer: the business rule is owned by `CaseEscalationService`; fixing it in the import or in a flow would create a second place that decides priority. One word changed.

Impact: callers are `CaseTriggerHandler` on insert and on account change (E3, E7). Both pass lists, and both only gain correct handling of the remaining cases. Single-record behaviour is unchanged. The dependency list is "none recorded" beyond these; the search over retrieved source found no other reference.

## Test results

Nothing was run: production is read-only, tests are disabled there, and the tools that deploy or run tests in the sandbox are not available in this unattended advise-mode run. The new test has not been compiled or executed. Expected: it fails on the current code (the second and third cases stay Medium) and passes after the change.

## Risks

- Existing wrong records are not repaired by the code fix. A one-time data correction is wanted (follow-up 01); the support team lead approves it after the fix is deployed. Scope today: exactly 00001003, 00001004, 00001006 (E12; ids `500Ws00000C0003IAA`, `500Ws00000C0004IAA`, `500Ws00000C0006IAA`). Plan: after deploy, re-run the E12 query, then set Priority = High on the rows it returns (3 rows, previous values Medium recorded), by an approved path in a non-advise session or by the user. Not done here: production is read-only.
- Cases created since 12 November: the E12 query covers all current cases on Premium accounts and shows no others, so none since then are wrong (as of 16 Nov 19:16 UTC).
- Test data in the new test assumes Account and Case need no extra required fields, as in the existing tests.
- Deploy to the sandbox first and run `CaseEscalationServiceTest`; production deploy needs the org's normal test run.

## Open questions

Answered by follow-up 01: batch order (case-number order, 10 Nov = one batch of four); data correction wanted, approved by the support team lead after deploy; no deployment record exists for the 9 Nov change.

- Who made the 9 Nov change and what was its intent? Setup audit trail only shows that the class changed (E13), not the diff. Would have asked the admin who deployed it.
- Do any other import sources or Apex callers also depend on a batch-wide exit? None found in retrieved source; unconfirmed beyond it.
