# DEMO-101: Premium customer cases are not set to High priority

| Field | Value |
|---|---|
| Key | DEMO-101 |
| Type | Bug |
| Status | Open |
| Priority | High |
| Created | 2026-11-12 09:40 |
| Last updated | 2026-11-12 15:02 (last comment) |
| Environment reported | Production (demo-prod); sandbox demo-sandbox available |
| Org alias to investigate | demo-prod (read-only); sandbox demo-sandbox for any change |

## Summary

Since the week of 9 November, some cases for Premium accounts are created with Medium instead of
High priority, so they miss their response time. A comment narrows it to cases created at 2 am by
the new nightly order import (integration user, REST composite API, batches up to 200). Not every
imported case is wrong. Cases created by agents, and the 12 November import, were fine.

## Steps to reproduce

1. Take an account whose Support Tier is Premium.
2. Create a case for it.
3. Look at Priority.

Corrected by the 2026-11-12 11:15 and 11:48 comments: the failures occur in the nightly import
(2 am), not for cases created by hand.

## Expected result

Priority = High for every case of a Premium account.

## Actual result

Sometimes Priority = Medium. Saving a wrong case again leaves it Medium.

## Affected records

Reported: 00001003, 00001004, 00001006. Reported OK: 00001001, 00001009, 00001010.

## Comment timeline

| When | Who (role, not name) | What it adds or changes |
|---|---|---|
| 2026-11-12 10:05 | support agent | Suspects the Case Premium Notification flow; no notification received for wrong cases; says it changed recently |
| 2026-11-12 10:32 | admin | Flow last activated 20 Oct and worked since; the three accounts are Premium; no validation or workflow rule on Case |
| 2026-11-12 11:15 | support team lead | All wrong cases created at 2 am by the new nightly import (live since 9-10 Nov); agent cases fine |
| 2026-11-12 11:48 | integration developer | Composite API, batches up to 200, one integration user, Priority not set by import; 00001001 same import as 00001003 and OK; 12 Nov import OK (00001009, 00001010) |
| 2026-11-12 14:20 | support agent | Workaround: re-saving a wrong case keeps Medium; priority fixed by hand each morning |
| 2026-11-12 15:02 | admin | Debug log 07LWs00000L0001MAA captured for the integration user during the 11 Nov import |

## History

- 2026-11-12 09:40 Created
- 2026-11-12 10:00 Priority changed from Medium to High
- 2026-11-12 11:20 Assigned to the Salesforce team

## Attachments

`attachments/ticket-original.txt`: full ticket text. No other files.

## Ambiguities

- Description says "since the start of this week" but the 11:15 comment says the import went live
  Monday night (9 to 10 November); the earliest wrong cases were created 10 November. Assumption: first occurrence is the first import run.
- The agent's 10:05 comment ties the problem to the notification flow; the admin disputes it. Assumption: the flow is a symptom (notification missing because Priority was not High), to be checked.
- "Not every imported case is wrong": which differ is not stated. Would have asked: are the affected cases in mixed batches with non-Premium accounts?
- Whether cases created at 2 am by the integration have other pre-existing wrong records beyond the three listed (the query shows more).
