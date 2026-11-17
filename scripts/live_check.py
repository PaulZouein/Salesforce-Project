"""Check the investigator's tools against a real org. Read-only. Writes a report you can share.

    python scripts/live_check.py --org my-dev
    python scripts/live_check.py --org my-dev --tests CaseEscalationServiceTest

Each check calls one of the investigator's read-only tools, exactly as Claude Code calls them,
and records whether it worked and the shape of what came back: counts, the names of the fields
returned, error codes. Record values, user names, email addresses, the org's Id and its web
address are left out, so the report can be sent to whoever helps you with the tool. Nothing in
the org is changed. ``--tests`` also runs those Apex test classes (allowed on sandboxes, scratch
orgs and Developer Edition orgs; refused on production).

What it saves:
    live-check/live-check-<date>-<time>.md    the report, to share
    live-check/live-check-<date>-<time>.json  the same, as data
    tickets/LIVE-CHECK/                       what the tools saved on the way (stays on this computer)

It never uses a tool that changes an org: it can only call the server's read-only tools.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TICKET = "LIVE-CHECK"
SAMPLE_CLASSES = ("CaseEscalationService", "CaseTriggerHandler", "CaseEscalationServiceTest")

_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_ORG_ID_RE = re.compile(r"\b00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s'\"\\/]+[\\/]){2,}[^\s'\"]*")


def scrub(text: Any, limit: int = 300) -> str:
    """Text safe to share: no email addresses, web addresses, org Ids or paths on this computer."""
    text = _EMAIL_RE.sub("[email]", str(text))
    text = _URL_RE.sub("[url]", text)
    text = _ORG_ID_RE.sub("00D[org]", text)
    text = _PATH_RE.sub("[path]", text)
    return text[:limit]


class Checker:
    """Calls read-only tools and keeps one line of result per check."""

    def __init__(self, org: str):
        from mcp_server import server

        self.org = org
        self.server = server
        self.results: list[dict[str, Any]] = []

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        if tool not in self.server.READ_ONLY_TOOLS or tool in self.server.WRITE_TOOLS:
            raise RuntimeError(f"{tool} is not a read-only tool; the live check never calls one")
        return json.loads(self.server.TOOLS[tool](**arguments))

    def check(self, name: str, tool: str, arguments: dict[str, Any],
              summarize: Callable[[dict[str, Any]], str] | None = None) -> dict[str, Any] | None:
        """Run one check. Returns the tool's answer when it worked."""
        started = time.monotonic()
        try:
            answer = self.call(tool, **arguments)
        except Exception as exc:  # noqa: BLE001 - a crash is a finding too
            self.results.append({"check": name, "tool": tool, "ok": False, "seconds": round(time.monotonic() - started, 1),
                                 "error": {"code": "crash", "message": scrub(f"{type(exc).__name__}: {exc}")}})
            return None
        entry: dict[str, Any] = {"check": name, "tool": tool, "ok": bool(answer.get("ok")),
                                 "seconds": round(time.monotonic() - started, 1)}
        if answer.get("ok"):
            entry["keys"] = sorted(k for k in answer if k != "ok")
            if summarize:
                try:
                    entry["summary"] = scrub(summarize(answer), 400)
                except Exception as exc:  # noqa: BLE001
                    entry["summary"] = scrub(f"(could not summarise: {type(exc).__name__}: {exc})")
        else:
            error = answer.get("error") or {}
            entry["error"] = {"code": error.get("code") or error.get("type"), "message": scrub(error.get("message")),
                              "hint": scrub(error.get("hint")) if error.get("hint") else None}
        self.results.append(entry)
        return answer if answer.get("ok") else None

    def skip(self, name: str, tool: str, why: str) -> None:
        self.results.append({"check": name, "tool": tool, "ok": None, "skipped": why})


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def run(org: str, tests: list[str] | None = None) -> list[dict[str, Any]]:
    c = Checker(org)
    c.check("Settings", "get_config", {}, lambda a: f"mode {a.get('mode')}, {len(a.get('warnings') or [])} warning(s)")
    orgs = c.check("Connected orgs", "list_orgs", {}, lambda a: f"{_count(a.get('orgs'))} org(s) connected")
    if orgs is not None:
        names = {str(o.get("alias") or "") for o in orgs.get("orgs") or []} | {str(o.get("username") or "") for o in orgs.get("orgs") or []}
        if org not in names:
            c.results[-1]["summary"] += f"; '{scrub(org)}' is not among them"
    info = c.check("What kind of org it is", "org_info", {"org": org},
                   lambda a: f"classification {a.get('classification')}, writes allowed {a.get('writes_allowed')}; "
                             f"reason: {a.get('classification_reason')}")
    if info is None:
        c.skip("Everything else", "-", "the org could not be reached or classified; fix that first")
        return c.results
    c.call("init_ticket", ticket_key=TICKET)

    case = c.check("Automation on Case", "describe_object",
                   {"org": org, "object_name": "Case", "include": ["automation", "fields"], "ticket_key": TICKET,
                    "save_as": "describe-case"},
                   lambda a: "; ".join(f"{k}: {_count(v) if not isinstance(v, dict) else 'error ' + str(v.get('error'))}"
                                       for k, v in (a.get("automation") or {}).items() if isinstance(v, (list, dict)))
                             + f"; {a.get('field_count')} fields")
    flows = [f.get("ApiName") for f in ((case or {}).get("automation") or {}).get("record_triggered_flows") or []
             if isinstance(f, dict) and f.get("ApiName")]
    c.check("A custom field on Account", "describe_object",
            {"org": org, "object_name": "Account", "fields": ["Support_Tier__c"]},
            lambda a: "Support_Tier__c " + ("found" if a.get("fields") else "not in this org (the sample is not deployed)"))
    cases = c.check("Records (SOQL)", "soql_query",
                    {"org": org, "query": "SELECT Id, CaseNumber, Priority, AccountId, CreatedDate FROM Case "
                                          "ORDER BY CreatedDate DESC LIMIT 5", "ticket_key": TICKET, "save_as": "cases"},
                    lambda a: f"{a.get('returned')} row(s) of {a.get('total_size')}; redacted: {a.get('redacted_fields')}; "
                              f"saved as evidence: {bool(a.get('saved_to'))}")
    c.check("Triggers (Tooling API)", "tooling_query",
            {"org": org, "query": "SELECT Id, Name, TableEnumOrId, Status, LastModifiedDate FROM ApexTrigger "
                                  "WHERE NamespacePrefix = null LIMIT 10"},
            lambda a: f"{a.get('returned')} trigger(s); fields: {sorted((a.get('records') or [{}])[0].keys()) if a.get('records') else []}")
    classes = c.check("Classes (Tooling API)", "tooling_query",
                      {"org": org, "query": "SELECT Id, Name, LastModifiedDate FROM ApexClass WHERE NamespacePrefix = null "
                                            "ORDER BY LastModifiedDate DESC LIMIT 20"},
                      lambda a: f"{a.get('returned')} class(es)")
    class_names = [r.get("Name") for r in (classes or {}).get("records") or [] if isinstance(r, dict) and r.get("Name")]
    sample_deployed = all(name in class_names for name in SAMPLE_CLASSES[:2])
    target_class = "CaseEscalationService" if "CaseEscalationService" in class_names else (class_names[0] if class_names else None)

    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    c.check("What changed in 30 days", "recent_org_changes", {"org": org, "since": since, "ticket_key": TICKET},
            lambda a: "; ".join(f"{k} {v}" for k, v in (a.get("counts") or {}).items())
                      + "".join(f"; {k}: error" for k, v in a.items() if isinstance(v, dict) and v.get("error")))
    if target_class:
        c.check("Who uses a class", "find_dependencies", {"org": org, "name": target_class, "ticket_key": TICKET},
                lambda a: f"used by {_count(a.get('used_by'))}, uses {_count(a.get('uses'))}"
                          + (f"; note: {a.get('note')}" if a.get("note") else ""))
        retrieved = c.check("Retrieve source", "retrieve_components",
                            {"org": org, "ticket_key": TICKET, "components": [f"ApexClass:{target_class}"]},
                            lambda a: f"{_count(a.get('files'))} file(s); org memory: "
                                      f"{json.dumps(a.get('org_memory'))[:160] if a.get('org_memory') else 'none'}")
        if retrieved:
            c.check("Scan retrieved Apex", "scan_apex", {"ticket_key": TICKET},
                    lambda a: f"{a.get('lead_count')} lead(s) in {a.get('files_scanned')} file(s)")
    else:
        c.skip("Who uses a class / Retrieve source", "find_dependencies", "the org has no Apex class of its own")
    c.check("Retrieve a component that does not exist", "retrieve_components",
            {"org": org, "ticket_key": TICKET, "components": ["ApexClass:Sfti_Live_Check_Does_Not_Exist"]},
            lambda a: f"{_count(a.get('files'))} file(s); "
                      + "; ".join(f"{k}: {v}" for k, v in a.items() if k in ("missing", "warnings", "note") and v))
    if flows and flows[0]:
        flow = c.check("Retrieve a flow", "retrieve_components",
                       {"org": org, "ticket_key": TICKET, "components": [f"Flow:{flows[0]}"]},
                       lambda a: f"{_count(a.get('files'))} file(s)")
        flow_files = [f for f in (flow or {}).get("files") or [] if str(f.get("path") if isinstance(f, dict) else f).endswith(".flow-meta.xml")]
        if flow_files:
            path = flow_files[0].get("path") if isinstance(flow_files[0], dict) else flow_files[0]
            c.check("Outline a flow", "summarize_flow", {"ticket_key": TICKET, "path": path},
                    lambda a: f"{(a.get('start') or {}).get('trigger_type')} on {(a.get('start') or {}).get('object')}, "
                              f"{_count(a.get('elements'))} element(s)")
    else:
        c.skip("Retrieve and outline a flow", "summarize_flow", "no record-triggered flow on Case")

    logs = c.check("Debug logs", "list_debug_logs", {"org": org, "limit": 5},
                   lambda a: f"{_count(a.get('logs'))} log(s); fields: "
                             f"{sorted((a.get('logs') or [{}])[0].keys()) if a.get('logs') else []}")
    newest = ((logs or {}).get("logs") or [None])[0]
    if isinstance(newest, dict) and (newest.get("id") or newest.get("Id")):
        log = c.check("Download a debug log", "get_debug_log",
                      {"org": org, "log_id": newest.get("id") or newest.get("Id"), "ticket_key": TICKET, "max_lines": 5},
                      lambda a: f"{a.get('total_lines') or a.get('lines')} line(s); saved: {bool(a.get('saved_to'))}")
        if log and log.get("saved_to"):
            c.check("Analyse a debug log", "analyze_debug_log", {"ticket_key": TICKET, "path": log["saved_to"]},
                    lambda a: f"{_count(a.get('code_units'))} code unit(s), {_count(a.get('exceptions'))} exception(s), "
                              f"{_count(a.get('soql'))} SOQL")
    else:
        c.skip("Download and analyse a debug log", "get_debug_log",
               "no debug log in the org (turn on a trace flag in Setup, then do something, to have one)")

    first_case = next((r.get("Id") for r in (cases or {}).get("records") or [] if isinstance(r, dict) and r.get("Id")), None)
    if first_case:
        c.check("Field history of a case", "record_history",
                {"org": org, "object_name": "Case", "record_id": first_case, "ticket_key": TICKET},
                lambda a: f"{a.get('count')} change(s)" + (f"; {a.get('note')}" if a.get("note") else ""))
    else:
        c.skip("Field history of a case", "record_history", "the org has no case")
    c.check("Org memory: check against the org", "verify_org_knowledge", {"org": org},
            lambda a: "; ".join(f"{k} {v}" for k, v in (a.get("counts") or {}).items()) or "nothing remembered yet")
    c.check("Org memory: recall", "recall_org_knowledge", {"org": org, "query": "case priority", "ticket_key": TICKET},
            lambda a: f"{_count(a.get('components'))} component(s), {_count(a.get('notes'))} note(s); "
                      f"checked against the org: {(a.get('freshness') or {}).get('checked')}")
    if tests:
        c.check("Run Apex tests", "run_apex_tests", {"org": org, "class_names": tests, "ticket_key": TICKET},
                lambda a: f"{a.get('outcome')}: {a.get('passing')} passing, {a.get('failing')} failing"
                          + (f"; coverage {a.get('coverage')}" if a.get("coverage") is not None else ""))
    else:
        c.skip("Run Apex tests", "run_apex_tests", "not asked for (add --tests CaseEscalationServiceTest)")
    if not sample_deployed:
        c.results.append({"check": "The sample (samples/planted-bug)", "tool": "-", "ok": None,
                          "skipped": "not deployed in this org; see docs/LIVE-ORG.md to try the sample ticket there"})
    return c.results


def report(results: list[dict[str, Any]], *, started: str, seconds: float) -> str:
    from webapp import __version__

    passed = sum(1 for r in results if r.get("ok") is True)
    failed = sum(1 for r in results if r.get("ok") is False)
    skipped = sum(1 for r in results if r.get("ok") is None)
    lines = [
        "# Live check of the Salesforce Ticket Investigator",
        "",
        f"Run {started}, {seconds:.0f} seconds. Version {__version__}. Python {sys.version.split()[0]} on {sys.platform}.",
        f"**{passed} worked, {failed} failed, {skipped} skipped.** Read-only: nothing was changed in the org.",
        "Record values, names, email addresses, the org's Id and web addresses are not in this report.",
        "",
        "| Check | Tool | Result | What came back |",
        "|---|---|---|---|",
    ]
    for r in results:
        if r.get("ok") is None:
            result, detail = "skipped", r.get("skipped", "")
        elif r.get("ok"):
            result, detail = f"worked ({r['seconds']} s)", r.get("summary") or ""
        else:
            error = r.get("error") or {}
            result = f"**failed** ({r.get('seconds')} s)"
            detail = f"{error.get('code')}: {error.get('message')}" + (f" Hint: {error.get('hint')}" if error.get("hint") else "")
        lines.append(f"| {r['check']} | `{r['tool']}` | {result} | {str(detail).replace('|', '/')} |")
    lines += ["", "Fields each answer had (to compare with what the code expects):", ""]
    for r in results:
        if r.get("keys"):
            lines.append(f"- {r['check']} (`{r['tool']}`): {', '.join(r['keys'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the investigator's read-only tools against a real org.")
    parser.add_argument("--org", required=True, help="alias of the org, as connected with the Salesforce CLI")
    parser.add_argument("--tests", nargs="*", default=None, help="Apex test classes to run as well (not on production)")
    parser.add_argument("--out", type=Path, default=ROOT / "live-check", help="folder for the report")
    args = parser.parse_args(argv)
    started = datetime.now().astimezone()
    clock = time.monotonic()
    print(f"Checking the investigator against '{args.org}' (read-only). This takes a minute or two.", flush=True)
    results = run(args.org, args.tests)
    text = report(results, started=started.strftime("%Y-%m-%d %H:%M %Z"), seconds=time.monotonic() - clock)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"live-check-{started.strftime('%Y%m%d-%H%M%S')}"
    stem.with_suffix(".md").write_text(text, encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = sum(1 for r in results if r.get("ok") is False)
    print(text)
    print(f"Report: {stem.with_suffix('.md')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
