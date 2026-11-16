---
name: investigate-ticket
description: Investigate a Salesforce bug ticket that has already been through intake. Scopes the org, builds ranked hypotheses, gathers evidence with the sf-investigator tools and the code-tracer subagent, states the root cause with its evidence chain, and writes tickets/<KEY>/report.md. Use after intake-ticket, or when the user asks to find the root cause of a ticket.
---

# Investigate a ticket

Prerequisite: `tickets/<KEY>/ticket.md` exists. If not, run `intake-ticket` first.

## 1. Scope

1. `get_config`: note the mode (advise or apply).
2. Confirm the org alias with the user unless they already gave it. `org_info` on it. State the
   classification in one line: "`<alias>` is production: read-only" or "`<alias>` is a sandbox".
3. If the ticket is about production and a sandbox is also available, read from production for
   real records and logs, and keep reproduction and any change for the sandbox.
4. `search_tickets` for the error text or the main component: an earlier ticket may already
   explain it.
5. `recall_org_knowledge` with the ticket's key words (object, feature, symptom). It returns what
   earlier tickets learned about this org and checks each remembered component against the org.
   Use it to decide what to retrieve and read **first**. It is a lead, never evidence:
   - `unchanged`: start there, and still read the file before citing it;
   - `changed`, `stale`, `not_found_in_org` or `not_checked`: the memory may be wrong. Retrieve the
     component again and trust what you read today;
   - nothing remembered: investigate as usual.

## 2. Hypotheses

Write them down before retrieving anything, in the report draft or in the chat:

| # | Hypothesis | Confirms it | Rules it out |
|---|---|---|---|

Rank by how well each explains **all** the symptoms, including the odd ones ("only in bulk",
"only for some users", "started on a given day"). Three to five is usual. Typical families:

- Apex: trigger or handler logic, bulk handling, recursion, order of triggers, exceptions swallowed
- Declarative: record-triggered flow entry conditions, flow order, validation rule, workflow rule,
  assignment or escalation rule, duplicate rule
- Data: unexpected field values, missing lookup, record type, picklist value not in the set
- Access: profile or permission set, field-level security, sharing, running user of an integration
- Change: a deploy or setup change shortly before the first occurrence
- Platform: governor limits, mixed DML, async timing, API version differences

## 3. Targeted investigation

Work one hypothesis at a time, cheapest decisive check first.

**What runs on the object**
- `describe_object` with `include: ["automation"]` lists triggers, record-triggered flows,
  validation rules and workflow rules. Retrieve only the ones a hypothesis needs:
  `retrieve_components` with `ApexTrigger:<Name>`, `ApexClass:<Handler>`, `Flow:<ApiName>`,
  `ValidationRule:<Object>.<Rule>`, `CustomObject:<Object>`, `AssignmentRules:<Object>`,
  `PermissionSet:<Name>`, `CustomMetadata:<Type>.<Record>`, `Layout:<Name>`.

**Execution path**
- Launch the `code-tracer` subagent with the ticket key, org alias, object and event (for example
  "Case, before insert and after insert"). It returns the call chain with `file:line` references
  and lists what it could not resolve. Retrieve the unresolved pieces and trace again if they matter.
- `summarize_flow` for each flow in the path. `scan_apex` gives leads; read each in context before
  believing it.

**Records**
- `soql_query` on the affected records with only the fields that matter, `ticket_key` set and a
  `save_as` name. Compare failing records with working ones: what differs (creator, time, origin,
  record type, field values, batch)?
- `record_history` for a single record's field changes.

**Time**
- `recent_org_changes` with `since` a few days before the first occurrence. A change just before
  the first failure is a strong lead and still a hypothesis until the component is read.

**Logs**
- `list_debug_logs`, then `get_debug_log` and `analyze_debug_log`. If there is no log for the
  failing action, say so. On a non-production org you can ask to set a trace flag
  (`set_trace_flag`, needs approval) and have the user reproduce.

**Dependencies**
- `find_dependencies` on the suspected component, confirmed with `search_metadata`.

**Reproduction**
- Non-production org: `run_apex_tests` on the existing test class. A passing suite that does not
  cover the failing scenario is itself a finding.
- Apply mode: a failing test or an anonymous Apex script that rolls back
  (`Savepoint` + `Database.rollback`) is the strongest confirmation.

After each check, update the hypothesis table: confirmed, ruled out, or still open, with the
artifact that decided it. Drop a hypothesis only on evidence.

## 4. Root cause

State it in one or two sentences, precisely: which component, which line or setting, under
which condition, producing which effect. Then the chain:

1. the symptom in data (query result),
2. the code or configuration that produces it (`file:line`),
3. why it only happens under the reported conditions,
4. what rules out the other hypotheses.

If any link is missing, the status is **Not confirmed**. Say exactly what is missing and what the
user can provide or do (a record Id, a trace flag window, a reproduction in a sandbox).

## 5. Report

1. `evidence_index` lists every saved artifact with the exact string to cite. Then
   `write_template` with kind `report`, and fill in `tickets/<KEY>/report.md`:
   ticket summary, investigation steps, evidence table (E1, E2, …), root cause, fix, test
   results, risks, open questions. Set "Root cause status" to Confirmed or Not confirmed.
2. For the Fix section run `propose-fix`. If the user only asked for the cause, write
   "Not yet proposed" there.
3. `validate_report` and fix every problem it lists. Read the warnings.
4. `export_report_html` if the user wants a readable copy.
5. Offer `write-ticket-comment`.

## 6. Remember what you learned about the org

Before you finish, save what a future investigation on this org would want to know, with
`remember_org_fact`: three to six facts, each one or two sentences, each with `topics` (the words
someone would search for), the `components` it is about and the `evidence` you read it from.

- Good: how the org is built. "CaseEscalationService decides case priority from the account's
  Support_Tier__c; CaseTriggerHandler calls it on insert and when the account changes." "Cases are
  also created in bulk by a nightly import through the composite API."
- Not for the memory: this ticket's conclusion, record data, personal data, guesses.
- If `recall_org_knowledge` returned a note that turned out wrong or outdated, replace it
  (`supersedes`) or remove it (`forget_org_fact`).

The components you retrieved are already in the org map; you do not need to describe them again.

## Reply to the user

Lead with the root cause and its status, then the two or three decisive pieces of evidence, then
what you recommend next. Point to `tickets/<KEY>/report.md` for the rest.

## Do not

- Retrieve whole metadata types "to be safe".
- Conclude from one record.
- Report a `scan_apex` lead or a dependency list as a finding.
- Use words like "probably" in the root cause without the **Hypothesis** label.
- Put personal data in the report.
