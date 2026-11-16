---
name: intake-ticket
description: Save a pasted Salesforce bug ticket into tickets/<KEY>/ticket.md in a structured form, read attachments, summarize it back and list ambiguities. Use when the user pastes a ticket or says a new ticket has arrived, before any investigation.
---

# Intake a ticket

Goal: a faithful, structured `tickets/<KEY>/ticket.md`, a short summary for the user, and a list
of ambiguities. No investigation happens in this skill.

## Steps

1. **Find the key.** Use the ticket key from the pasted text (for example `ABC-123`). If there is
   none, ask for it; do not invent one.
2. **Create the folder.** Call `init_ticket` with the key. It creates `tickets/<KEY>/` and a
   `ticket.md` template.
   A ticket submitted through the web page is already there: its text is in
   `tickets/<KEY>/attachments/ticket-original.txt` and the chosen org in `request.json`.
3. **Read everything before writing.** Description, every comment, the history, and each file in
   `tickets/<KEY>/attachments/` (open screenshots with the Read tool). Comments often change the
   picture: workarounds tried, which environments are affected, corrected reproduction steps,
   reports that contradict each other.
4. **Fill in `ticket.md`.** Keep the template's sections:
   - header table (key, type, status, priority, dates, environment reported, org alias if known);
   - Summary: two or three sentences in your own words;
   - Steps to reproduce, Expected result, Actual result: as reported. If a comment corrects the
     description, write the corrected version and note which comment changed it;
   - Affected records: record Ids only;
   - Comment timeline: one row per comment, with a role instead of a name ("reporter",
     "support agent", "admin", "developer") and what the comment adds or changes;
   - History: status changes, reassignments, links;
   - Attachments: file name and what it shows (exact error text from screenshots, verbatim);
   - Ambiguities.
5. **Leave personal data out.** No names, emails or phone numbers in `ticket.md`. Refer to people
   by role and to records by Id. Quote error messages exactly.
6. **Separate what was reported from what you infer.** Anything the ticket does not state is
   listed under Ambiguities, not written into the summary.

## Reply to the user

Keep it short:

- the summary (what is broken, for whom, since when, where);
- what the comments changed compared with the description;
- contradictions between reporters, if any;
- ambiguities, each with why it matters;
- at most three questions, and only ones whose answer changes what you would check. Always ask
  which org alias to investigate if the ticket does not make it certain.

Then offer to start the investigation (`investigate-ticket`).

## Checks before you finish

- Every comment appears in the timeline.
- Exact error messages are quoted, not paraphrased.
- The first-occurrence date and any "it used to work" statement are captured: they drive the
  recent-changes check later.
- Nothing in `ticket.md` is your guess about the cause.
