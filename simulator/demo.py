"""The DEMO-101 investigation as a fixed sequence of read-only tool calls.

Two users:

* the end-to-end tests, which replay the investigation and check the stored report against it;
* the web interface's **recorded sample**, which shows a complete result without the AI agent.
  The evidence is gathered live from the simulated org by the real tools; the written analysis
  (``ticket.md``, ``report.md``, ``comment.md``) is the stored sample from
  ``samples/DEMO-101/expected/``. The interface says so.

Nothing here writes to an org: the sequence is the advise-mode investigation.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable

KEY = "DEMO-101"
PROD, SANDBOX = "demo-prod", "demo-sandbox"
LOG_ID = "07LWs00000L0001MAA"
SERVICE = "metadata/force-app/main/default/classes/CaseEscalationService.cls"
SERVICE_TEST = "metadata/force-app/main/default/classes/CaseEscalationServiceTest.cls"
FLOW = "metadata/force-app/main/default/flows/Case_Premium_Notification.flow-meta.xml"
WRONG_CASES = ["500Ws00000C0003IAA", "500Ws00000C0004IAA", "500Ws00000C0006IAA"]
COMPONENTS = [
    "ApexTrigger:CaseTrigger", "ApexClass:CaseTriggerHandler", "ApexClass:CaseEscalationService",
    "ApexClass:CaseEscalationServiceTest", "Flow:Case_Premium_Notification",
]

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "DEMO-101"
EXPECTED = SAMPLES / "expected"
TICKET_TEXT = SAMPLES / "ticket.txt"

CASES_QUERY = (
    "SELECT Id, CaseNumber, Priority, Origin, AccountId, Account.Support_Tier__c, CreatedById, CreatedDate "
    "FROM Case WHERE CreatedDate >= 2026-11-09T00:00:00Z ORDER BY CreatedDate, CaseNumber"
)
NOT_HIGH_QUERY = (
    "SELECT Id, CaseNumber, Priority, CreatedDate FROM Case WHERE Priority != 'High' "
    "AND Account.Support_Tier__c = 'Premium' ORDER BY CaseNumber"
)

MIXED_BATCH_TEST = '''
    @isTest
    static void mixedBatchEscalatesEveryPremiumCase() {
        Account standard = createAccount('Standard Test Account', 'Standard');
        Account premium = createAccount('Premium Test Account', 'Premium');

        Test.startTest();
        List<Case> batch = new List<Case>{
            new Case(Subject = 'Standard first', AccountId = standard.Id, Priority = 'Medium'),
            new Case(Subject = 'Premium second', AccountId = premium.Id, Priority = 'Medium')
        };
        insert batch;
        Test.stopTest();

        Map<Id, Case> saved = new Map<Id, Case>([SELECT Priority FROM Case WHERE Id IN :batch]);
        System.assertEquals('Medium', saved.get(batch[0].Id).Priority, 'A Standard case keeps its priority');
        System.assertEquals('High', saved.get(batch[1].Id).Priority,
            'Every Premium case in a mixed batch should be High');
    }
}
'''

ORG_FACTS = [
    {
        "text": "CaseEscalationService.applyPremiumPriority decides case priority: it sets Priority to High for "
                "cases whose account has Support_Tier__c = Premium. It is the only code that writes Case.Priority.",
        "topics": ["case priority", "premium", "support tier", "escalation"],
        "components": ["ApexClass:CaseEscalationService"],
        "evidence": [f"{SERVICE}:26-33"],
    },
    {
        "text": "CaseTrigger hands the whole batch to CaseTriggerHandler, which calls the escalation service on "
                "before insert, and on before update only for cases whose AccountId changed.",
        "topics": ["case trigger", "bulk insert", "order of execution"],
        "components": ["ApexTrigger:CaseTrigger", "ApexClass:CaseTriggerHandler"],
        "evidence": ["metadata/force-app/main/default/classes/CaseTriggerHandler.cls:6-20"],
    },
    {
        "text": "The Case_Premium_Notification flow runs after save on create, notifies the support lead when "
                "Priority is High, and never writes to the case: it depends on the trigger having set the priority.",
        "topics": ["case notification", "flow", "case priority"],
        "components": ["Flow:Case_Premium_Notification"],
        "evidence": [f"{FLOW}:31-41"],
    },
    {
        "text": "Cases are also created in bulk by a nightly import through the REST composite API, as one "
                "integration user, in batches of up to 200. Bulk behaviour matters for anything on Case insert.",
        "topics": ["nightly import", "integration", "bulk insert", "case"],
        "components": ["ApexTrigger:CaseTrigger"],
        "evidence": ["logs/07LWs00000L0001MAA.log:4"],
    },
]


def apply_planted_bug_fix(ticket_dir: Path) -> None:
    """Make the DEMO-101 fix in a ticket workspace: ``return;`` -> ``continue;`` plus the new test."""
    service = Path(ticket_dir) / SERVICE
    lines = service.read_text(encoding="utf-8").split("\n")
    assert lines[29].strip() == "return;", lines[29]
    lines[29] = lines[29].replace("return;", "continue;")
    service.write_text("\n".join(lines), encoding="utf-8")

    test_class = Path(ticket_dir) / SERVICE_TEST
    text = test_class.read_text(encoding="utf-8").rstrip()
    assert text.endswith("}")
    test_class.write_text(text[:-1].rstrip() + "\n" + MIXED_BATCH_TEST, encoding="utf-8")


def gather_evidence(inv: Any, ticket_dir: Path, on_step: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """Run the investigation's tool calls. ``on_step(tool, what)`` is called before each one."""
    out: dict[str, Any] = {}

    def step(tool: str, what: str) -> None:
        if on_step is not None:
            on_step(tool, what)

    step("get_config", "Reading the mode and limits")
    out["config"] = inv.get_config()
    out["init"] = inv.init_ticket(KEY)
    step("org_info", f"Classifying {PROD} and {SANDBOX}")
    out["prod"] = inv.org_info(PROD)
    out["sandbox"] = inv.org_info(SANDBOX)
    step("search_tickets", "Looking for earlier tickets on the same subject")
    out["earlier_tickets"] = inv.search_tickets("Premium")
    step("recall_org_knowledge", "Asking the org memory what is already known")
    out["recall"] = inv.recall_org_knowledge(PROD, "premium customer case priority high import notification flow")

    step("soql_query", "Querying the cases created since 9 November with their account tier")
    out["cases"] = inv.soql_query(PROD, CASES_QUERY, ticket_key=KEY, save_as="cases-since-9-nov")
    step("describe_object", "Listing the automation that runs on Case")
    out["automation"] = inv.describe_object(PROD, "Case", include=["automation"])["automation"]
    step("retrieve_components", "Retrieving the trigger, handler, service, test class and flow")
    out["retrieve"] = inv.retrieve_components(PROD, KEY, COMPONENTS)
    step("scan_apex", "Scanning the Apex for bug-prone patterns (leads only)")
    out["scan"] = inv.scan_apex(KEY)
    step("read_file", "Reading CaseEscalationService around the lead")
    out["service"] = inv.read_file(KEY, SERVICE, start_line=26, end_line=33)
    step("summarize_flow", "Outlining the Case Premium Notification flow")
    out["flow"] = inv.summarize_flow(KEY, FLOW)
    step("recent_org_changes", "Checking what changed in the org before the first failure")
    out["changes"] = inv.recent_org_changes(PROD, "2026-11-03", until="2026-11-12T23:59:59Z")
    out["audit_trail"] = inv.soql_query(
        PROD, "SELECT Action, Section, Display, CreatedDate, CreatedById FROM SetupAuditTrail "
              "WHERE CreatedDate >= 2026-11-03T00:00:00Z ORDER BY CreatedDate",
        ticket_key=KEY, save_as="setup-audit-trail")
    out["class_dates"] = inv.soql_query(
        PROD, "SELECT Name, LastModifiedDate, LastModifiedById FROM ApexClass WHERE Name LIKE 'Case%' ORDER BY Name",
        tooling=True, ticket_key=KEY, save_as="apex-class-last-modified")
    out["flow_versions"] = inv.soql_query(
        PROD, "SELECT Id, VersionNumber, Status, LastModifiedDate FROM Flow ORDER BY LastModifiedDate",
        tooling=True, ticket_key=KEY, save_as="flow-versions")
    step("get_debug_log", "Downloading the debug log of the 11 November import")
    out["logs"] = inv.list_debug_logs(PROD)
    out["log"] = inv.get_debug_log(PROD, LOG_ID, KEY, grep=r"STATEMENT_EXECUTE\|\[(2[6-9]|3[0-3])\]|FLOW_RULE_DETAIL")
    step("analyze_debug_log", "Reading which statements ran and how the flow decided")
    out["log_analysis"] = inv.analyze_debug_log(KEY, out["log"]["saved_to"])
    step("find_dependencies", "Finding what else uses the service")
    out["dependencies"] = inv.find_dependencies(PROD, "CaseEscalationService")
    out["callers"] = inv.search_metadata(KEY, r"applyPremiumPriority\(", glob="*.cls")
    step("run_apex_tests", f"Running the existing tests on {SANDBOX}")
    out["tests"] = inv.run_apex_tests(SANDBOX, ["CaseEscalationServiceTest"], ticket_key=KEY)
    out["sandbox_class_dates"] = inv.soql_query(
        SANDBOX, "SELECT Name, LastModifiedDate FROM ApexClass WHERE Name LIKE 'Case%' ORDER BY Name",
        tooling=True, ticket_key=KEY, save_as="sandbox-apex-class-last-modified")
    out["mixed_batch_test_before_fix"] = inv.search_metadata(KEY, "mixedBatch|List<Case>\\{", glob="*Test.cls")
    step("soql_query", "Counting the Premium cases that are not High")
    out["not_high"] = inv.soql_query(PROD, NOT_HIGH_QUERY, ticket_key=KEY, save_as="premium-cases-not-high")

    step("workspace_diff", "Drafting the fix locally and producing the diff")
    apply_planted_bug_fix(ticket_dir)
    out["diff"] = inv.workspace_diff(KEY)
    out["evidence"] = inv.evidence_index(KEY)
    return out


def with_replayed_citations(text: str, ticket_dir: Path) -> str:
    """Point the stored report's saved-query citations at the files of this run (new timestamps)."""
    by_slug = {re.sub(r"^\d{8}-\d{6}-", "", p.name): p.name for p in (Path(ticket_dir) / "queries").glob("*.json")}

    def swap(match: re.Match) -> str:
        name = match.group(1)
        if name not in by_slug:
            raise KeyError(f"this run did not save {name}")
        return "queries/" + by_slug[name]

    return re.sub(r"queries/\d{8}-\d{6}-([A-Za-z0-9\-]+\.json)", swap, text)


def install_expected(ticket_dir: Path) -> None:
    """Copy the stored ticket, report and comment into a ticket folder whose evidence was just gathered."""
    ticket_dir = Path(ticket_dir)
    for name in ("ticket.md", "comment.md"):
        shutil.copyfile(EXPECTED / name, ticket_dir / name)
    report = with_replayed_citations((EXPECTED / "report.md").read_text(encoding="utf-8"), ticket_dir)
    (ticket_dir / "report.md").write_text(report, encoding="utf-8")


def remember_org_facts(inv: Any) -> list[str]:
    """Store what the investigation learned about the org, once. Returns the ids of new notes."""
    existing = {n["text"] for n in inv.org_map(PROD)["notes"]}
    added = []
    for fact in ORG_FACTS:
        if fact["text"] in existing:
            continue
        note = inv.remember_org_fact(PROD, fact["text"], topics=fact["topics"], components=fact["components"],
                                     evidence=fact["evidence"], ticket_key=KEY)["remembered"]
        added.append(note["id"])
    return added
