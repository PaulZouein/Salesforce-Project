# DEMO-101: Premium customer cases are not set to High priority

| Field | Value |
|---|---|
| Key | DEMO-101 |
| Type | Bug |
| Status | Open |
| Priority | High (raised from Medium on 2026-11-12 10:00) |
| Created | 2026-11-12 09:40 |
| Last updated | 2026-11-12 15:02 (last comment) |
| Environment reported | Production |
| Org alias to investigate | `demo-prod` (production, read-only); `demo-sandbox` available for reproduction |

## Summary

Since the week of 9 November 2026, some cases that belong to Premium accounts are created with
Priority Medium instead of High. The business rule is that every case for a Premium account is
High priority, so these cases miss their response time. It does not happen for every case.

## Steps to reproduce

As first reported:

1. Take an account whose Support Tier is Premium.
2. Create a case for it.
3. Look at the Priority field.

Corrected by the comments of 11:15 and 11:48: creating a case by hand works. The wrong cases all
come from the nightly import from the order system, which creates cases at 02:00 through the REST
composite API, in batches of up to 200, as one integration user, without setting Priority.

## Expected result

Priority = High for every case whose account has Support Tier = Premium.

## Actual result

Some imported cases for Premium accounts have Priority = Medium. Re-saving such a case leaves it
at Medium (comment of 14:20). The "high priority case" notification was not received for them
(comment of 10:05).

## Affected records

Reported as wrong (case numbers, resolved to Ids during the investigation): 00001003, 00001004,
00001006.

Reported as correct, for comparison: 00001001 (same import as 00001003), 00001009 and 00001010
(import of 12 November), and a case created by hand on 11 November for a Premium account.

Debug log captured for the integration user during the import of 11 November: `07LWs00000L0001MAA`.

## Comment timeline

| When | Who (role, not name) | What it adds or changes |
|---|---|---|
| 2026-11-12 10:05 | Support agent | Suspects the Case Premium Notification flow, which "was changed recently". The usual notification did not arrive for the wrong cases. |
| 2026-11-12 10:32 | Salesforce admin | The flow was last activated on 20 October and has worked since. The three accounts are Premium. Sees no validation rule or workflow rule on Case. |
| 2026-11-12 11:15 | Support team lead (reporter) | All wrong cases were created at 02:00 by the new nightly import, live since the night of 9 to 10 November. Cases created by agents during the day are fine. |
| 2026-11-12 11:48 | Integration developer | The import uses the REST composite API in batches of up to 200 as one integration user and does not set Priority. Not every imported case is wrong: 00001001 is High. The import of 12 November looks fine (00001009, 00001010). |
| 2026-11-12 14:20 | Support agent | Workaround tried: re-saving a wrong case leaves Priority at Medium. Priorities are corrected by hand every morning. |
| 2026-11-12 15:02 | Salesforce admin | A debug log exists for the integration user for the import of 11 November: `07LWs00000L0001MAA`. |

## History

- 2026-11-12 09:40 Created
- 2026-11-12 10:00 Ticket priority changed from Medium to High
- 2026-11-12 11:20 Assigned to the Salesforce team

## Attachments

None.

## Ambiguities

1. **"Since the start of this week" versus the import go-live.** The description gives no date;
   the 11:15 comment ties the first wrong cases to the first import run (10 November, 02:00).
   Matters for the recent-changes check: the window to look at starts before 10 November.
2. **"It was changed recently" (flow) versus "last activated on 20 October".** The two comments
   disagree on whether the flow changed. Matters for whether the flow is a suspect at all.
3. **Is the missing notification a second defect or a consequence?** If the flow only notifies for
   High priority cases, a Medium case would not notify by design.
4. **Which cases of a batch go wrong.** 00001001 and 00001003 came from the same import with
   different results. The ticket does not say what differs between them (account, position in the
   batch, other cases in the same batch).
5. **Why 12 November was fine.** Nothing in the ticket says a fix was deployed in between.
6. **Does the sandbox hold the same code as production?** Needed before reproducing there.
