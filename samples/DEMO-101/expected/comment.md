<!-- Draft for DEMO-101. Paste into the ticket yourself; nothing is posted automatically. -->

**Root cause** (confirmed)

The Apex class `CaseEscalationService` stops working through a batch of new cases as soon as it
meets a case whose account is not Premium. Every Premium case that comes later in the same batch
keeps Medium. Cases created one at a time are not affected, which is why agents' cases are fine
and only the nightly import (up to 200 cases per call) shows the problem. The flow is not at
fault: it notifies only for High priority cases, so the missing notifications are a consequence.

**Evidence**

- All 3 Premium cases that are not High were imported, and each follows a Standard-account case in
  its batch: 500Ws00000C0003IAA, 500Ws00000C0004IAA, 500Ws00000C0006IAA. The import of 12 November
  contained only Premium cases and is correct.
- `CaseEscalationService.cls` line 30 is `return;` inside the loop over the batch.
- Debug log `07LWs00000L0001MAA` (import of 11 November): the first case is Standard, line 30
  runs, the method exits, and the line that sets High never runs.
- Re-saving does not help because the rule is re-applied on update only when the account changes.

**Proposed fix**

Replace `return;` with `continue;` on that line, and add a test that inserts a mixed batch. Not
applied anywhere yet: the diff is ready for a developer to deploy to the sandbox first.

**Testing**

The 2 existing tests pass in the sandbox, with the defect present: none covers a mixed batch. The
new test has not been run yet.

**Next step / needed from you**

Developer: deploy the fix to `demo-sandbox` and run `CaseEscalationServiceTest`. Support lead:
confirm the 3 cases can be set to High, and keep the morning check until the fix is in production.
