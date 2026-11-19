"""What changed, and when.

Two questions that shorten most investigations:

* *What changed in the org around the time the problem started?* — ``recent_org_changes``
  reads the setup audit trail and the last-modified dates of code and automation.
* *What happened to this record?* — ``record_history`` reads field history tracking.

Both are read-only.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import InputValidationError
from .logs import _soql_datetime
from .query import PERSONAL_FIELD_PATTERN, REDACTED, run_raw_query, soql_literal, strip_attributes, validate_api_name
from .sf_cli import SfCli, validate_target_org

_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(\.[\w\-]+)+")
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")

#: History objects that do not follow the "<Object>History" naming.
HISTORY_OBJECTS = {
    "Opportunity": ("OpportunityFieldHistory", "OpportunityId"),
}


#: History fields that point at a person (a user, a queue, a contact or a lead) when they are not Ids.
_PERSON_LOOKUP_FIELDS = frozenset({"owner", "contact", "who", "lead", "user", "manager", "assignee", "requester",
                                   "delegatedapprover", "createdby", "lastmodifiedby"})
#: Setup audit trail sections whose descriptions name people ("Changed profile for user Jane Doe…").
_USER_SECTIONS_RE = re.compile(r"user|contact|partner|portal|communit", re.IGNORECASE)


def _is_id(value: Any) -> bool:
    """A Salesforce record Id: 15 or 18 letters and digits, at least one digit."""
    return isinstance(value, str) and bool(_RECORD_ID_RE.match(value)) and any(c.isdigit() for c in value)


def _mask_emails(text: Any) -> Any:
    return _EMAIL_RE.sub("[email]", text) if isinstance(text, str) else text


def recent_org_changes(
    cli: SfCli,
    org: str,
    since: str,
    *,
    until: str | None = None,
    contains: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Setup changes and modified code/automation between ``since`` and ``until`` (ISO 8601)."""
    validate_target_org(org)
    start = _soql_datetime(since)
    end = _soql_datetime(until) if until else None
    limit = max(1, min(int(limit), 200))
    window = f"CreatedDate >= {start}" + (f" AND CreatedDate <= {end}" if end else "")
    modified = f"LastModifiedDate >= {start}" + (f" AND LastModifiedDate <= {end}" if end else "")
    needle = contains.lower() if contains else None
    if contains and (len(contains) > 80):
        raise InputValidationError("contains is too long.")
    out: dict[str, Any] = {"since": start, "until": end}

    def attempt(key: str, query: str, *, tooling: bool) -> list[dict[str, Any]]:
        try:
            rows = strip_attributes(run_raw_query(cli, org, query, tooling=tooling))
        except Exception as exc:  # noqa: BLE001 - report and continue with the other sources
            out[key] = {"error": getattr(exc, "message", str(exc))}
            return []
        return rows

    trail = attempt(
        "setup_audit_trail",
        "SELECT Action, Section, Display, CreatedDate, CreatedById, DelegateUser FROM SetupAuditTrail "
        f"WHERE {window} ORDER BY CreatedDate DESC LIMIT {limit}",
        tooling=False,
    )
    if "setup_audit_trail" not in out:
        entries = []
        for row in trail:
            display = _mask_emails(row.get("Display"))
            if needle and needle not in f"{display} {row.get('Section')} {row.get('Action')}".lower():
                continue
            if _USER_SECTIONS_RE.search(str(row.get("Section") or "")):
                # These descriptions name the people concerned; the action code says what was done.
                display = f"[hidden: a change to {row.get('Section')} that names a person; action {row.get('Action')}]"
            entries.append({
                "when": row.get("CreatedDate"),
                "section": row.get("Section"),
                "action": row.get("Action"),
                "display": display,
                "by_user_id": row.get("CreatedById"),
                "delegated": bool(row.get("DelegateUser")),
            })
        out["setup_audit_trail"] = entries
        if len(trail) >= limit:
            out["setup_audit_trail_truncated"] = True

    sources = {
        "apex_classes": ("SELECT Id, Name, LastModifiedDate, LastModifiedById FROM ApexClass "
                         f"WHERE NamespacePrefix = null AND {modified} ORDER BY LastModifiedDate DESC LIMIT {limit}"),
        "apex_triggers": ("SELECT Id, Name, TableEnumOrId, Status, LastModifiedDate, LastModifiedById FROM ApexTrigger "
                          f"WHERE NamespacePrefix = null AND {modified} ORDER BY LastModifiedDate DESC LIMIT {limit}"),
        "flow_versions": ("SELECT Id, Definition.DeveloperName, VersionNumber, Status, LastModifiedDate, "
                          f"LastModifiedById FROM Flow WHERE {modified} ORDER BY LastModifiedDate DESC LIMIT {limit}"),
        "validation_rules": ("SELECT Id, ValidationName, EntityDefinition.QualifiedApiName, Active, LastModifiedDate, "
                             f"LastModifiedById FROM ValidationRule WHERE {modified} "
                             f"ORDER BY LastModifiedDate DESC LIMIT {limit}"),
    }
    for key, query in sources.items():
        rows = attempt(key, query, tooling=True)
        if key in out:
            continue
        if needle:
            rows = [r for r in rows if needle in str(r).lower()]
        out[key] = rows

    counts = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    failed = sorted(k for k, v in out.items() if isinstance(v, dict) and "error" in v)
    out["counts"] = counts
    out["how_to_use"] = (
        "Compare these timestamps with when the ticket says the problem started. A change shortly before "
        "the first failure is a strong lead, but it is still a hypothesis until the component itself is read."
    )
    if failed and not counts:
        out["note"] = (
            "Every source failed, so nothing is known about changes in this window: this is not an answer of "
            "'no changes'. Read the errors above."
        )
    elif failed:
        out["note"] = (
            f"{len(failed)} source(s) failed ({', '.join(failed)}): changes there are unknown, not absent. "
            + ("The other sources recorded no change in this window." if not any(counts.values()) else "")
        ).strip()
    elif not any(counts.values()):
        out["note"] = (
            "No changes recorded in this window. The setup audit trail keeps 180 days; data loads and "
            "permission changes made through the API may not appear here."
        )
    return out


def record_history(
    cli: SfCli,
    org: str,
    sobject: str,
    record_id: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Field history of one record, newest first. Needs field history tracking on the object."""
    validate_target_org(org)
    validate_api_name(sobject, "object API name")
    if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
        raise InputValidationError(f"Invalid record Id: {record_id!r}.")
    limit = max(1, min(int(limit), 200))

    if sobject in HISTORY_OBJECTS:
        history, parent = HISTORY_OBJECTS[sobject]
    elif sobject.endswith("__c"):
        history, parent = sobject[:-1] + "History", "ParentId"
    else:
        history, parent = f"{sobject}History", f"{sobject}Id"

    rows = strip_attributes(run_raw_query(
        cli,
        org,
        f"SELECT Field, OldValue, NewValue, CreatedById, CreatedDate FROM {history} "
        f"WHERE {parent} = {soql_literal(record_id)} ORDER BY CreatedDate DESC, Id DESC LIMIT {limit}",
    ))
    # A change to a lookup (Owner, Contact…) is recorded twice: once with Ids, once with display names, which
    # are people's names for users, queues and contacts. The Ids are kept; the names are not.
    with_ids = {(row.get("Field"), row.get("CreatedDate")) for row in rows
                if _is_id(row.get("OldValue")) or _is_id(row.get("NewValue"))}
    redacted: set[str] = set()
    entries = []
    for row in rows:
        field = str(row.get("Field") or "")
        names_of_a_lookup = (field.lower() in _PERSON_LOOKUP_FIELDS or (row.get("Field"), row.get("CreatedDate")) in with_ids) \
            and not (_is_id(row.get("OldValue")) or _is_id(row.get("NewValue")))
        hide = bool(PERSONAL_FIELD_PATTERN.search(field)) or names_of_a_lookup
        if hide:
            redacted.add(field)
        entries.append({
            "when": row.get("CreatedDate"),
            "field": field,
            "old_value": REDACTED if hide and row.get("OldValue") is not None else row.get("OldValue"),
            "new_value": REDACTED if hide and row.get("NewValue") is not None else row.get("NewValue"),
            "by_user_id": row.get("CreatedById"),
        })
    out: dict[str, Any] = {
        "sobject": sobject,
        "record_id": record_id,
        "history_object": history,
        "count": len(entries),
        "redacted_fields": sorted(redacted),
        "changes": entries,
    }
    if not entries:
        out["note"] = (
            f"No history rows. Either nothing tracked has changed, or field history tracking is not enabled "
            f"for the relevant fields on {sobject}. Absence of history is not evidence that nothing changed."
        )
    return out
