# Changes waiting for the owner

The assistant may not edit `.claude/settings.json` or `.claude/hooks/`, so these changes are
prepared here for you to apply. Both answer findings of the independent safety review (decision
103 in [`docs/DECISIONS.md`](../../docs/DECISIONS.md)). Nothing here is active until you apply it.

## 1. Settings: pytest and the simulator ask first

`scripts/generate_settings.py` no longer allows `python -m pytest` and `python -m simulator`
without asking. A permission rule matches the start of a command and cannot stop what follows,
so `python -m pytest --doctest-glob=*.log tickets/T-1/logs` ran, unprompted, whatever a saved
debug log contained. The review showed a log line changing a record in production this way,
with nothing in the audit log.

## 2. Hook: `scripts/owner/guard.py` replaces `.claude/hooks/guard.py`

Compared with the installed hook it:

- blocks running anything under `tickets/` (Python, pytest, shells, node and others, also after
  `cd tickets/…`): those folders hold evidence from the org and the ticket, which is read, never
  executed;
- no longer excuses every command that contains the word `pytest`: only a command that actually
  starts pytest (`pytest …`, `python -m pytest …`) may name a write function;
- fixes the known false positive: `grep deploy_component core/changes.py` is no longer blocked;
- sees through `uv run`, `poetry run`, `pipenv run`, `pdm run`, `hatch run` and `conda run`.

It keeps everything else: the same protected files, the same CLI names, the same write functions.
`tests/test_proposed_guard.py` runs every command of the installed hook's tests through it, plus
the new cases.

## How to apply

In your own terminal, in this folder:

```
python scripts/generate_settings.py
cp scripts/owner/guard.py .claude/hooks/guard.py
python -m pytest
git add .claude/settings.json .claude/hooks/guard.py
git commit -m "Apply the hardened settings and hook"
```

On Windows use `copy scripts\owner\guard.py .claude\hooks\guard.py`. The two expected failures
the tests report today then pass. Afterwards you can delete `scripts/owner/` together with
`tests/test_proposed_guard.py`.

## Optional: `--protect-code`

`python scripts/generate_settings.py --protect-code` also denies edits to the code that enforces
the rules (`core/`, `mcp_server/`, `webapp/`, `scripts/`), `CLAUDE.md` and the skills. Use it in a
folder that only investigates tickets. It also stops the assistant from working on the tool itself,
so in the folder where you develop the tool, leave it out (or regenerate without it first).
Investigations started from the local page are already limited to writing under `tickets/`.
