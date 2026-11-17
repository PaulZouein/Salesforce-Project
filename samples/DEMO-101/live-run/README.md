# DEMO-101, investigated live from the web page

What the investigator wrote, unedited, in two real runs started from the page
(`python -m webapp --demo`, sample ticket, **Investigate with Claude Code**) on 16 November 2026.
Both ran on the simulated demo orgs, which do not execute Apex. The model was Sonnet.

| | `first-run/` | `follow-up/` |
|---|---|---|
| Started with | the pasted ticket | **Add information**: answers to the open questions of the first run (`follow-up-01.txt`), then **Investigate again** |
| Time | 2 min 6 s | 57 s |
| Steps (model turns) | 57 | 31 |
| Root cause | Confirmed | Confirmed |
| Citations verified by the evidence check | 14 | 18 |
| Usage reported by Claude Code | 0.74 USD | 0.35 USD |

Files in each folder: `ticket.md` (the structured intake), `report.md`, `comment.md` (the draft
for the ticket), `fix.diff`, and `activity.txt`, which is the progress list the page showed
(written out from the run's events; paths shortened).

Things to notice:

- **First run.** It reads the trigger, the handler, the service and the flow, compares failing
  and working cases, reads the debug log, and separates what it proved from what it assumed:
  two statements are labelled **Hypothesis**, and it lists three questions it could not ask.
  For two claims it had to write "no saved artifact", because the tools that list an object's
  automation and recent changes could not save their answer then. They can now.
- **Follow-up.** It does not start again. It reads the note, re-queries what the note changes,
  saves the answer about recent changes as evidence (E13), cites the user's note as what the
  user said (E14), drops the hypothesis the note settled, keeps the one it did not, and replaces
  the answered questions with two new ones.
- The folder had never been opened in Claude Code. The page gives the run the read-only tools by
  name and removes the ones that change an org.

The citations point into the ticket's own folder (`queries/…`, `logs/…`, `metadata/…`), which is
not copied here, so the evidence check cannot be re-run on these copies. To get a ticket folder
you can check, run the sample yourself; `samples/DEMO-101/expected/` is the recorded sample that
the tests replay and check.

Usage and time will differ on your machine, with another model, and on a real org.
