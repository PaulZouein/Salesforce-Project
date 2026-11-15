---
name: write-ticket-comment
description: Write a concise draft ticket comment in tickets/<KEY>/comment.md from the investigation report, for the user to paste into Jira. Never posts anything. Use when the report is done or the user asks for a ticket update.
---

# Write the ticket comment

The comment is for people who will not open the report: the reporter, the support lead, the
release manager. They need the answer, the proof in one glance, and what happens next.

## Steps

1. Read `tickets/<KEY>/report.md`. If it does not exist or `validate_report` fails, fix that first.
2. `write_template` with kind `comment`, then write `tickets/<KEY>/comment.md`:
   - **Root cause** and its status (confirmed or not confirmed), in two or three plain sentences.
     Name the component; skip the line numbers unless a developer is the audience.
   - **Evidence**: two to four bullets, each one artifact and what it shows. Counts and record
     Ids, no personal data.
   - **Proposed fix**: what changes and where, in one or two sentences. Say whether it has been
     applied anywhere (advise mode: not applied; apply mode: which sandbox, change Id).
   - **Testing**: what was run and the result, or what still has to be run.
   - **Next step**: one line, with who needs to do it.
3. If the cause is not confirmed, say so in the first line and ask for exactly what is needed.
4. `validate_comment` and fix every problem.
5. Show the comment in the chat and say where the file is. The user posts it.

## Style

- Under about 250 words.
- Plain language first; technical names in backticks.
- No speculation presented as fact. If something is a hypothesis, the comment says "hypothesis".
- No names, emails or phone numbers. "The integration user (`005…`)" is fine.
- No blame.

## Never

- Post, send or upload the comment anywhere.
- Claim a fix is deployed or verified when it is not.
