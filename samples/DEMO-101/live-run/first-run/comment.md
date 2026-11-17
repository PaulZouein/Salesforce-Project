**Root cause (confirmed):** `CaseEscalationService.applyPremiumPriority` stops processing the whole insert batch as soon as it meets a case whose account is not Premium (a `return` inside the loop where a `continue` was meant). Cases after it in the same batch keep the default Medium priority. The nightly import sends many cases per batch, so this happens only there; agent-created cases are inserted one at a time. The `Case Premium Notification` flow is not at fault: it only reads Priority, so no notification was sent because Priority was not High.

**Evidence**
- Debug log `07LWs00000L0001MAA` (11 Nov import): 2 cases in one trigger run, the first account is Standard, and the method exits with no Priority set.
- Cases 00001003, 00001004 (10 Nov) and 00001006 (11 Nov) are Medium on Premium accounts; 00001001, 00001009 and 00001010 are High.
- A plain re-save does not repair a case because priority is only re-applied when the account changes.

**Proposed fix (not applied anywhere):** change `return;` to `continue;` in `CaseEscalationService` and add a mixed-batch test to the existing `CaseEscalationServiceTest`. Diff is in `fix.diff`.

**Testing:** nothing run yet (production is read-only; the new test is uncompiled). Please deploy to `demo-sandbox`, run `CaseEscalationServiceTest`, then re-run a two-case import there.

**Caveat:** the 10 Nov batch grouping is inferred from identical timestamps and case order, not read from the import.

**Next step:** a developer deploys the fix via the normal path. Already-wrong cases (at least the three above) need a separate, approved data correction.
