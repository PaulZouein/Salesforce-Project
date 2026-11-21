# Quick start

Paste a Salesforce bug ticket, get back the root cause with its evidence, the smallest fix as a
diff, and a comment ready for the ticket. Everything runs on your computer. Nothing is posted
anywhere, and a production org is never changed.

Works on **Windows** and **macOS**.

## 1. What you need

| | Why | Get it |
|---|---|---|
| **Python 3.11 or newer** | runs the investigator | <https://www.python.org/downloads/> (on Windows, tick *Add python.exe to PATH*) |
| **Claude Code**, signed in | it is the investigator | `npm install --global @anthropic-ai/claude-code`, then run `claude` once and sign in |
| **Salesforce CLI** | reads your org | `npm install --global @salesforce/cli` (not needed for the demo) |
| **git** | keeps a history of what was retrieved | Windows: <https://git-scm.com/download/win> · macOS: `xcode-select --install` |

`npm` comes with Node.js: <https://nodejs.org/>. If something is missing, start anyway: the page
tells you what, with the exact command for your system.

## 2. Start it

Unzip the folder anywhere, then double-click the one file to run:

| **Windows** | `start.bat` |
|---|---|
| **macOS** | `start.command` |

No Salesforce org yet, or just looking? On the page, click **Try with the demo orgs**: simulated
orgs with a planted bug, no Salesforce account needed. **Use my own Salesforce orgs** switches back.

The first start takes about a minute (it installs what it needs into a `.venv` folder next to
the files). Then your browser opens on `http://127.0.0.1:8765` (the address is also shown in the window). Keep the small window that
opened; closing it stops the investigator.

> **macOS says the file cannot be opened?** Right-click the file, choose **Open**, then **Open**
> again. This is asked once. Or, in Terminal: `sh start.command`
>
> **Windows shows "Windows protected your PC"?** Choose **More info**, then **Run anyway**.

## 3. Follow the page

**Get started** checks your computer and walks you through four steps. The one that needs you is
connecting an org: give it a short name, choose what kind of org it is (sandbox, production,
Developer Edition or your My Domain address), and press **Log in with your browser**. Log in to
Salesforce in the window that opens; the page lists the org when you are done. Production is
read-only here whatever your user is allowed to do.

The same login from a terminal, if you prefer (the page shows the exact line under
**Or connect from a terminal**):

```
sf org login web --alias my-sandbox --instance-url https://test.salesforce.com
```

## 4. Investigate a ticket

1. **New ticket**: paste the whole ticket, comments included, or **Upload a PDF** of it
   (exported or printed from Jira). Choose the org where it happens.
2. **Investigate with Claude Code**: you see each step as it happens. A typical ticket takes a
   few minutes.
3. Read the result: **Root cause** (every claim tied to a file and line, a saved query or a log
   line), **Fix** (a diff and a test plan), **Ticket comment** (a draft you paste yourself),
   **Evidence**. **Download the result** gives you all of it as one file.
4. Something to add, or the investigator had a question? Write it under **Add information**,
   attach a screenshot or a log if you have one, and press **Investigate again**. It continues
   from what it already found.

With the demo: **New ticket → Load the sample ticket**. *Replay the recorded sample* shows a
complete result in seconds; *Investigate with Claude Code* runs a real investigation on the
simulated org.

## 5. It learns your org

Every investigation adds to an **Org memory**: which automation runs on which object, what each
class and flow does, how they connect. The next ticket on the same org starts from there, after
asking the org whether anything changed since. The memory stays in the `knowledge` folder on
your computer.

## Good to know

- **Where things are**: each ticket has a folder in `tickets/` with the report, the comment, the
  fix and the saved evidence. Nothing leaves your computer except what Claude Code itself sends
  to Claude to do the work.
- **What an investigation started from the page can do**: read from the org and write in the
  ticket's folder. It is not given the tools that change an org, a shell, or web access.
- **Deploying a fix to a sandbox** is done from the terminal, where you approve each step:
  in a terminal in this folder, `start.bat --claude` (Windows) or `sh start.command --claude` (macOS). See *The two modes* in
  `README.md`.
- **What it costs**: the *Claude usage* box (sidebar, or a pill at the bottom on a phone) shows
  the running investigation's tokens and estimated price as it works, today's total, and how much
  of your plan's limits is used.
- **Cheaper runs**: tick **Use fewer tokens**, and set **Stop if it costs more than $…** if you
  want a hard limit. Or choose *Sonnet* next to the Investigate button.
- **Have a Developer Edition org?** `docs/LIVE-ORG.md` tries everything on it, safely.
- **No browser where the investigator runs** (a remote computer, a cloud session)? *Connect an
  org* can take an authorization pasted from a computer that has one. See `docs/GUIDE.md`.
- **Stop**: close the small window, or press Ctrl+C in it.
- **Update**: unzip the new version over the old one and start again. Your `tickets` and
  `knowledge` folders are kept.

## If something does not work

| What you see | What to do |
|---|---|
| "Python 3.11 or newer was not found" | Install Python from python.org, then start again. On Windows tick *Add python.exe to PATH*. |
| "The installation did not finish" | No internet, or a company network blocking `pypi.org`. Fix that and start again. |
| The page opens but **Claude Code not found** | Install it (table above), then press **Check again**. You can still paste tickets; the page gives you a prompt to run in Claude Code yourself. |
| **No org is connected** | **Log in with your browser** on **Get started**, or run the `sf org login web …` line the page shows, then **I have connected it**. |
| "The sf-investigator tools did not start" | Close the window and start again with the start file, not with `python -m webapp` from another Python. |
| The investigation stops with a sign-in message | Run `claude` once in a terminal and sign in. |
| You started it twice | Nothing breaks: the second start opens the copy that is already running. To switch between demo and your own orgs, close the first window. |

More detail: `README.md`, then the guides in `docs/`: `GUIDE.md` (using it), `SAFETY.md`,
`REFERENCE.md` (every tool) and `VERIFICATION.md` (what has and has not been verified).
