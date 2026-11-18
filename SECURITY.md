# Security policy

The Salesforce Ticket Investigator reads customer Salesforce orgs. Its safety rules are enforced
in code, not left to the assistant's judgement:

- **Production is read-only.** Every write and every test run is classified as such by the
  Salesforce CLI wrapper (`core/sf_cli.py`) and needs a permit that only `core/org_guard.py`
  issues, after asking the org itself whether it is production. There is no setting, flag or
  instruction that changes this.
- **Every write needs the user's approval**, every time: the write tools of the MCP server ask.
- **Nothing is posted anywhere.** Reports and ticket comments are drafts the user posts.
- **No personal data in reports or comments.** Query results are redacted before they are saved.
- **The local page answers only on the computer it runs on** (`127.0.0.1`, a host check, and a
  token for anything that changes state). An authorization pasted on it goes to the Salesforce
  CLI on standard input and is never written, logged or sent back.

[`docs/SAFETY.md`](docs/SAFETY.md) explains each layer and what it does not guarantee.

## Reporting a vulnerability

Please report it privately, not in a public issue: on GitHub, open the **Security** tab of this
repository and choose **Report a vulnerability**. Include what you did, what happened, and the
commit you tested.

Especially welcome:

- a way to change a production org, or to run a write or a test without a permit;
- a write that does not ask the user first;
- anything posted to an external system;
- personal data reaching a report or a comment, or a secret reaching a log, a file or an answer;
- the local page answering a request from another website or another computer.

Out of scope: the guard hook's documented false positive ([`docs/SAFETY.md`](docs/SAFETY.md), *The hook, and
what it does not guarantee*), and vulnerabilities in the Salesforce CLI or Claude Code themselves (report those to
Salesforce and Anthropic).
