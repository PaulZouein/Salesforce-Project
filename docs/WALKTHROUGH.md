# Walk-through: every step, with a screenshot

From a computer that has nothing set up to a finished investigation. The screenshots and the
video below were recorded by a script from the running page, on the simulated demo orgs, with a
real investigation by Claude Code (model: Sonnet). The simulated orgs do not execute Apex. The
machine that recorded them runs Linux, which is why step 1 says "How to fix it on Linux"; on
Windows and macOS the page shows the command for your system.

**The video (about three minutes, no sound):**
[`docs/demo/ticket-investigator-demo.mp4`](demo/ticket-investigator-demo.mp4). The part where
the investigation runs is shown faster; everything else is real time.

To follow along on your own computer: double-click `start-demo.bat` (Windows) or
`start-demo.command` (macOS). [`QUICKSTART.md`](../QUICKSTART.md) has what you need installed.

## 1. Get started: the page checks your computer

The first page checks Python, git, the Salesforce CLI and Claude Code. Anything missing comes
with the exact command that installs it on your system and a **Check again** button. Here the
Salesforce CLI is not installed yet.

![Get started](images/steps/01-get-started.png)

## 2. Connect an org: the page builds the command

Type a short name for the org and pick what kind it is. The page shows the one command to run in
a terminal. Your browser opens, you log in to Salesforce there, and you come back and press
**I have connected it**. The investigator never sees your password.

![Connect an org](images/steps/02-connect-an-org.png)

## 3. No org at hand: the demo orgs

**Try with the demo orgs** switches to three simulated orgs with a planted bug. Each org is shown
for what it is: production (always read-only), sandbox, Developer Edition. With your own orgs the
same table appears after you connect them.

![The demo orgs](images/steps/03-demo-orgs.png)

## 4. New ticket: paste it, choose the org

Paste the ticket exactly as it is in your ticket system, comments included. The key is found in
the text. Choose the org where the problem happens, a sandbox for tests if you have one, and the
model: Sonnet is quicker and uses less of your Claude usage. With the demo orgs, **Load the sample
ticket** fills the form.

![New ticket](images/steps/04-new-ticket.png)

## 5. The ticket is saved; nothing runs until you say so

**Investigate with Claude Code** starts the investigation. It is given the read-only tools: it
reads from the org and writes into this ticket's folder. The tools that change an org are not
given to it. Under **Add information** you can add a note or a file before it starts.

![Ready to start](images/steps/05-ready-to-start.png)

## 6. Investigating: every step as it happens

Each step appears in plain words (reading the ticket, asking the org memory, retrieving source,
querying records, reading a debug log, checking the report's evidence), and the list on the right
fills in. **Stop** ends it; what was gathered is kept.

![Investigating](images/steps/06-investigating.png)

## 7. The result

At the top: the root cause in one paragraph, whether it is **Confirmed**, and the evidence check
(how many citations were verified against saved files). Below it, the questions the investigator
could not ask you. **Download the result** gives the report, the comment and the fix as one zip.

![The result](images/steps/07-result.png)

## 8. Root cause: the chain of evidence

Each step of the reasoning points to a numbered piece of evidence: a file and line, a saved
query result, a debug log line. What is not proven is labelled **Hypothesis**.

![Root cause](images/steps/08-root-cause.png)

## 9. Fix: the smallest change, as a diff

The change, why at that layer, what else it affects, the risks and a test plan. Nothing is
deployed: you apply it, or you use Claude Code in `apply` mode, where every deploy to a sandbox
asks you first and production is refused.

![Fix](images/steps/09-fix.png)

## 10. Ticket comment: a draft you paste yourself

Short, with record Ids and no personal data. **Copy the comment**, read it, and paste it into
your ticket. Nothing is posted anywhere by the investigator.

![Ticket comment](images/steps/10-ticket-comment.png)

## 11. Evidence: the saved files behind every citation

Query results (stored with personal data redacted), debug logs, retrieved source. They are in
`tickets/<KEY>/` on your computer.

![Evidence](images/steps/11-evidence.png)

## 12. Add information and investigate again

Answer an open question, correct something, add a record Id, attach a screenshot or an exported
log. **Investigate again** continues from what was found instead of starting over. In the
recorded follow-up of this ticket (`samples/DEMO-101/live-run/`) that took one minute: it
re-queried what the answer changed, cited the answer as what the user said, and dropped the
hypothesis the answer settled.

![Add information](images/steps/12-add-information.png)

## 13. Tickets: everything investigated on this computer

![Tickets](images/steps/13-tickets.png)

## 14. Org memory: what the next ticket starts from

Every investigation adds to a map of the org: what runs on each object, what each class and flow
does, how they connect, and a few facts worth keeping. **Ask the memory** runs the search the
investigator runs at the start of a ticket.

![Org memory](images/steps/14-org-memory.png)

## 15. The double check: has anything changed?

Orgs change. **Check against the org** asks the org whether each remembered component was
modified since it was learned; the investigator does the same before it relies on anything.
Memory is a lead, never evidence: a report cites only what was read during that investigation.

![The double check](images/steps/15-memory-double-check.png)

## 16. How it stays safe

![How it stays safe](images/steps/16-how-it-stays-safe.png)

## Newer screenshots

The video and the screenshots above were recorded before three additions.

**The usage box.** In the sidebar (a pill on a phone): what the running investigation has used,
in tokens and as an estimated price, updating as it works; then the last run, today's total and
your plan's limits.

![The usage box while an investigation runs](images/web-usage-box.png)

**Use fewer tokens**, and a spending limit, on New ticket and next to the Investigate button.
**Upload a PDF** on New ticket, for a ticket exported or printed from Jira.

![New ticket with a PDF read, Use fewer tokens and a spending limit](images/web-new-ticket-pdf.png)

## What the walk-through does not show

- **The first start.** The start script creates a private Python environment and installs the
  project (about a minute, once), then opens the page. See `QUICKSTART.md`.
- **A real org.** Everything above ran on the simulated orgs. On a real org the steps are the
  same; time and usage are higher, and a few Salesforce behaviours are still unverified (the
  [VERIFICATION.md](VERIFICATION.md) lists them).
- **Applying a fix.** That is done from Claude Code in the terminal (`claude-here`), in `apply`
  mode, with your approval for each step. [VERIFICATION.md](VERIFICATION.md) shows the full loop on the simulated sandbox.
