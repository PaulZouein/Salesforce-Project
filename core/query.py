"""SOQL and Tooling API queries with enforced row limits and redaction.

* Only ``SELECT`` statements run.
* A top-level ``LIMIT`` is required: it is added when missing and rejected when it exceeds
  the configured maximum.
* Values of fields that look like personal data are replaced with ``[REDACTED]``. Copies
  written to the ticket folder are always redacted, whatever the caller asked to reveal.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, load_settings
from .errors import InputValidationError, QueryValidationError, RowLimitError
from .sf_cli import SfCli, validate_target_org
from .workspace import Workspace, slugify

REDACTED = "[REDACTED]"

#: Field-name fragments that indicate personal data (matched case-insensitively).
PERSONAL_FIELD_PATTERN = re.compile(
    r"(e_?mail|phone|mobile|fax|birth|ssn|social_?security|passport|national_?id|tax_?id|iban|"
    r"credit_?card|card_?number|street|postal_?code|zip|latitude|longitude|geocode|"
    r"first_?name|last_?name|middle_?name|maiden|salutation|nick_?name|supplied_?name|address)",
    re.IGNORECASE,
)

#: Objects whose ``Name`` is a person's name.
PERSON_OBJECTS = frozenset({"contact", "lead", "user", "personaccount", "individual", "employee"})

_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")
_FORBIDDEN_CLAUSE = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_TAIL_CLAUSE_RE = re.compile(r"\b(OFFSET\s+\d+|FOR\s+(VIEW|REFERENCE)|ALL\s+ROWS)\b", re.IGNORECASE)


def soql_literal(value: str) -> str:
    """Quote a string for use in a SOQL ``WHERE`` clause."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _mask_top_level(query: str) -> str:
    """Blank out string literals and anything inside parentheses, keeping positions."""
    out: list[str] = []
    depth = 0
    in_string = False
    i = 0
    while i < len(query):
        ch = query[i]
        if in_string:
            if ch == "\\" and i + 1 < len(query):
                out.append("  ")
                i += 2
                continue
            if ch == "'":
                in_string = False
            out.append(" ")
        elif ch == "'":
            in_string = True
            out.append(" ")
        elif ch == "(":
            depth += 1
            out.append(" ")
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise QueryValidationError("Unbalanced parentheses in the query.")
            out.append(" ")
        else:
            out.append(ch if depth == 0 else " ")
        i += 1
    if in_string:
        raise QueryValidationError("Unterminated string literal in the query.")
    if depth != 0:
        raise QueryValidationError("Unbalanced parentheses in the query.")
    return "".join(out)


def enforce_limit(query: str, *, default_limit: int, max_limit: int, requested_limit: int | None = None) -> tuple[str, int]:
    """Validate ``query`` and return it with a top-level ``LIMIT`` no greater than ``max_limit``."""
    if not isinstance(query, str) or not query.strip():
        raise QueryValidationError("The query is empty.")
    text = query.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    masked = _mask_top_level(text)
    if ";" in masked:
        raise QueryValidationError("Only one statement is allowed.")
    if not re.match(r"^\s*SELECT\b", masked, re.IGNORECASE):
        raise QueryValidationError("Only SELECT queries are allowed.", hint="This tool reads data; it never changes it.")
    if _FORBIDDEN_CLAUSE.search(masked):
        raise QueryValidationError("FOR UPDATE is not allowed: it locks records.")
    if requested_limit is not None:
        if isinstance(requested_limit, bool) or not isinstance(requested_limit, int) or requested_limit < 1:
            raise RowLimitError("limit must be a positive integer.")
        if requested_limit > max_limit:
            raise RowLimitError(
                f"limit {requested_limit} is above the maximum of {max_limit} rows.",
                hint="Narrow the WHERE clause, or page with ORDER BY and a filter on the last value seen.",
            )

    # Subquery limits are checked too: a child subquery can multiply the rows returned.
    without_strings = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    for nested in _LIMIT_RE.finditer(without_strings):
        if int(nested.group(1)) > max_limit:
            raise RowLimitError(
                f"LIMIT {nested.group(1)} is above the maximum of {max_limit} rows.",
                hint="Narrow the WHERE clause, or page with ORDER BY and a filter on the last value seen.",
            )

    limits = list(_LIMIT_RE.finditer(masked))
    if len(limits) > 1:
        raise QueryValidationError("More than one top-level LIMIT clause.")
    if limits:
        found = int(limits[0].group(1))
        if found > max_limit:
            raise RowLimitError(
                f"LIMIT {found} is above the maximum of {max_limit} rows.",
                hint="Narrow the WHERE clause, or page with ORDER BY and a filter on the last value seen.",
            )
        if requested_limit is not None and requested_limit < found:
            start, end = limits[0].span(1)
            text = text[:start] + str(requested_limit) + text[end:]
            return text, requested_limit
        return text, found

    applied = requested_limit if requested_limit is not None else default_limit
    applied = min(applied, max_limit)
    tail = _TAIL_CLAUSE_RE.search(masked)
    if tail:
        text = f"{text[:tail.start()].rstrip()} LIMIT {applied} {text[tail.start():]}"
    else:
        text = f"{text} LIMIT {applied}"
    return text, applied


# --- Redaction --------------------------------------------------------------------------


class Redactor:
    """Replaces the values of personal-data fields in query results."""

    def __init__(self, extra_fields: Iterable[str] = (), reveal_fields: Iterable[str] = ()):
        self.extra = {f.lower() for f in extra_fields}
        self.reveal = {f.lower() for f in reveal_fields}
        self.redacted: set[str] = set()

    def _should_redact(self, field: str, path: str, object_type: str | None) -> bool:
        name, full = field.lower(), path.lower()
        if name in self.reveal or full in self.reveal:
            return False
        if name in self.extra or full in self.extra:
            return True
        if PERSONAL_FIELD_PATTERN.search(field):
            return True
        if name == "name" and object_type and object_type.lower() in PERSON_OBJECTS:
            return True
        if name in ("username", "communitynickname", "alias") and object_type and object_type.lower() == "user":
            return True
        return False

    def record(self, record: Any, prefix: str = "") -> Any:
        if isinstance(record, list):
            return [self.record(item, prefix) for item in record]
        if not isinstance(record, dict):
            return record
        attributes = record.get("attributes")
        object_type = attributes.get("type") if isinstance(attributes, dict) else None
        out: dict[str, Any] = {}
        for key, value in record.items():
            if key == "attributes":
                continue
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                if "records" in value and isinstance(value["records"], list):
                    # Child relationship subquery.
                    out[key] = {
                        "totalSize": value.get("totalSize"),
                        "records": self.record(value["records"], f"{path}."),
                    }
                elif self._should_redact(key, path, object_type):
                    # Compound fields such as addresses.
                    self.redacted.add(path)
                    out[key] = REDACTED
                else:
                    out[key] = self.record(value, f"{path}.")
            elif value is not None and self._should_redact(key, path, object_type):
                self.redacted.add(path)
                out[key] = REDACTED
            else:
                out[key] = value
        return out


def strip_attributes(record: Any) -> Any:
    if isinstance(record, list):
        return [strip_attributes(r) for r in record]
    if isinstance(record, dict):
        return {k: strip_attributes(v) for k, v in record.items() if k != "attributes"}
    return record


# --- Running ----------------------------------------------------------------------------


def run_raw_query(
    cli: SfCli,
    org: str,
    query: str,
    *,
    tooling: bool = False,
    timeout: float = 180.0,
) -> list[dict[str, Any]]:
    """Run a query that the core itself wrote and return the raw records.

    For internal use: no limit rewriting, no redaction. The query is passed through a file
    so that quoting is identical on every platform.
    """
    validate_target_org(org)
    with tempfile.TemporaryDirectory(prefix="sfti-query-") as tmp:
        (Path(tmp) / "query.soql").write_text(query, encoding="utf-8")
        args = ["data", "query", "--file", "query.soql", "--target-org", org]
        if tooling:
            args.append("--use-tooling-api")
        result = cli.run(args, cwd=tmp, timeout=timeout) or {}
    records = result.get("records") if isinstance(result, dict) else None
    return records if isinstance(records, list) else []


def run_query(
    cli: SfCli,
    org: str,
    query: str,
    *,
    tooling: bool = False,
    limit: int | None = None,
    redact_fields: Iterable[str] = (),
    reveal_fields: Iterable[str] = (),
    workspace: Workspace | None = None,
    ticket_key: str | None = None,
    save_as: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run a user-supplied query with limit enforcement and redaction."""
    settings = settings or load_settings()
    validate_target_org(org)
    final_query, applied_limit = enforce_limit(
        query,
        default_limit=settings.default_row_limit,
        max_limit=settings.max_row_limit,
        requested_limit=limit,
    )

    with tempfile.TemporaryDirectory(prefix="sfti-query-") as tmp:
        (Path(tmp) / "query.soql").write_text(final_query, encoding="utf-8")
        args = ["data", "query", "--file", "query.soql", "--target-org", org]
        if tooling:
            args.append("--use-tooling-api")
        result = cli.run(args, cwd=tmp, timeout=180) or {}

    raw_records = result.get("records") if isinstance(result, dict) else None
    raw_records = raw_records if isinstance(raw_records, list) else []
    total_size = result.get("totalSize") if isinstance(result, dict) else None

    shown = Redactor(redact_fields, reveal_fields)
    records = shown.record(raw_records)
    out: dict[str, Any] = {
        "query": final_query,
        "api": "tooling" if tooling else "data",
        "applied_limit": applied_limit,
        "total_size": total_size,
        "returned": len(records),
        "redacted_fields": sorted(shown.redacted),
        "records": records,
    }
    if isinstance(total_size, int) and total_size >= applied_limit:
        out["note"] = (
            f"The result reached the limit of {applied_limit} rows; more rows may exist. "
            "Narrow the WHERE clause rather than raising the limit."
        )

    if workspace is not None and ticket_key:
        stored = Redactor(redact_fields, ())  # reveal_fields never apply to files on disk
        stored_records = stored.record(raw_records)
        now = datetime.now(timezone.utc)
        name = f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify(save_as or ('tooling' if tooling else 'soql'), 'query')}.json"
        path = workspace.subdir(ticket_key, "queries") / name
        path.write_text(
            json.dumps(
                {
                    "query": final_query,
                    "api": out["api"],
                    "org": org,
                    "executed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "total_size": total_size,
                    "redacted_fields": sorted(stored.redacted),
                    "records": stored_records,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        out["saved_to"] = workspace.relative(ticket_key, path)
    return out


# --- Describe ---------------------------------------------------------------------------


def validate_api_name(name: str, what: str = "API name") -> str:
    if not isinstance(name, str) or not _API_NAME_RE.match(name) or len(name) > 120:
        raise InputValidationError(f"Invalid {what}: {name!r}.")
    return name


DESCRIBE_SECTIONS = ("fields", "record_types", "child_relationships", "automation")


def _field_summary(f: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": f.get("name"),
        "label": f.get("label"),
        "type": f.get("type"),
        "custom": f.get("custom"),
        "required": bool(f.get("createable")) and not f.get("nillable") and not f.get("defaultedOnCreate"),
        "updateable": f.get("updateable"),
    }
    if f.get("length"):
        out["length"] = f.get("length")
    if f.get("referenceTo"):
        out["reference_to"] = f.get("referenceTo")
        out["relationship_name"] = f.get("relationshipName")
    if f.get("calculated"):
        out["formula"] = f.get("calculatedFormula")
    if f.get("defaultValue") is not None:
        out["default_value"] = f.get("defaultValue")
    if f.get("unique"):
        out["unique"] = True
    if f.get("externalId"):
        out["external_id"] = True
    if f.get("controllerName"):
        out["controlling_field"] = f.get("controllerName")
    values = f.get("picklistValues") or []
    if values:
        active = [v.get("value") for v in values if v.get("active", True)]
        out["picklist_values"] = active[:60]
        if len(active) > 60:
            out["picklist_values_truncated"] = len(active)
    return out


def describe_object(
    cli: SfCli,
    org: str,
    sobject: str,
    *,
    fields: Iterable[str] | None = None,
    field_filter: str | None = None,
    include: Iterable[str] = ("fields", "record_types"),
) -> dict[str, Any]:
    """Condensed describe of one object, optionally with the automation that runs on it."""
    validate_target_org(org)
    validate_api_name(sobject, "object API name")
    sections = set(include)
    unknown = sections - set(DESCRIBE_SECTIONS)
    if unknown:
        raise InputValidationError(
            f"Unknown include value(s): {sorted(unknown)}.", hint=f"Choose from {list(DESCRIBE_SECTIONS)}."
        )

    raw = cli.run(["sobject", "describe", "--sobject", sobject, "--target-org", org], timeout=180) or {}
    out: dict[str, Any] = {
        "name": raw.get("name"),
        "label": raw.get("label"),
        "custom": raw.get("custom"),
        "key_prefix": raw.get("keyPrefix"),
        "queryable": raw.get("queryable"),
        "createable": raw.get("createable"),
        "updateable": raw.get("updateable"),
        "deletable": raw.get("deletable"),
        "field_count": len(raw.get("fields") or []),
    }

    if "fields" in sections:
        wanted = {f.lower() for f in fields} if fields else None
        needle = field_filter.lower() if field_filter else None
        selected = []
        for f in raw.get("fields") or []:
            name = str(f.get("name", ""))
            if wanted is not None and name.lower() not in wanted:
                continue
            if needle and needle not in name.lower() and needle not in str(f.get("label", "")).lower():
                continue
            selected.append(_field_summary(f))
        out["fields"] = selected
        if wanted:
            missing = sorted(wanted - {str(f["name"]).lower() for f in selected})
            if missing:
                out["fields_not_found"] = missing

    if "record_types" in sections:
        out["record_types"] = [
            {
                "name": r.get("name"),
                "developer_name": r.get("developerName"),
                "id": r.get("recordTypeId"),
                "active": r.get("active"),
                "default": r.get("defaultRecordTypeMapping"),
                "master": r.get("master"),
            }
            for r in raw.get("recordTypeInfos") or []
        ]

    if "child_relationships" in sections:
        children = [
            {
                "child_object": c.get("childSObject"),
                "field": c.get("field"),
                "relationship_name": c.get("relationshipName"),
                "cascade_delete": c.get("cascadeDelete"),
            }
            for c in raw.get("childRelationships") or []
            if c.get("relationshipName")
        ]
        out["child_relationships"] = children[:150]

    if "automation" in sections:
        out["automation"] = object_automation(cli, org, sobject)
    return out


def object_automation(cli: SfCli, org: str, sobject: str) -> dict[str, Any]:
    """Triggers, record-triggered flows, validation rules and workflow rules on an object.

    Each list is queried separately; a failure in one is reported in place and does not
    hide the others.
    """
    validate_api_name(sobject, "object API name")
    name = soql_literal(sobject)
    out: dict[str, Any] = {}

    def attempt(key: str, query: str, *, tooling: bool) -> list[dict[str, Any]] | None:
        try:
            records = strip_attributes(run_raw_query(cli, org, query, tooling=tooling))
        except Exception as exc:  # noqa: BLE001 - report and continue
            message = getattr(exc, "message", str(exc))
            out[key] = {"error": message}
            return None
        out[key] = records
        return records

    entity = attempt(
        "entity",
        f"SELECT DurableId, QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName = {name} LIMIT 1",
        tooling=True,
    )
    durable_id = entity[0].get("DurableId") if entity else None
    table_id = soql_literal(durable_id or sobject)

    attempt(
        "triggers",
        "SELECT Id, Name, Status, UsageBeforeInsert, UsageAfterInsert, UsageBeforeUpdate, UsageAfterUpdate, "
        "UsageBeforeDelete, UsageAfterDelete, UsageAfterUndelete FROM ApexTrigger "
        f"WHERE TableEnumOrId = {table_id} ORDER BY Name LIMIT 200",
        tooling=True,
    )
    attempt(
        "validation_rules",
        "SELECT Id, ValidationName, Active, ErrorDisplayField, ErrorMessage FROM ValidationRule "
        f"WHERE EntityDefinition.QualifiedApiName = {name} ORDER BY ValidationName LIMIT 200",
        tooling=True,
    )
    attempt(
        "record_triggered_flows",
        "SELECT ApiName, Label, ProcessType, TriggerType, RecordTriggerType, TriggerOrder, IsActive, "
        f"ActiveVersionId FROM FlowDefinitionView WHERE TriggerObjectOrEventId = {table_id} "
        "ORDER BY TriggerOrder NULLS LAST, ApiName LIMIT 200",
        tooling=False,
    )
    attempt(
        "workflow_rules",
        f"SELECT Id, Name FROM WorkflowRule WHERE TableEnumOrId = {table_id} ORDER BY Name LIMIT 200",
        tooling=True,
    )
    out["how_to_read_the_source"] = (
        "Retrieve with retrieve_components, for example ApexTrigger:<Name>, Flow:<ApiName>, "
        f"ValidationRule:{sobject}.<ValidationName>, then use read_file."
    )
    return out
