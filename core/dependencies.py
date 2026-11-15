"""Component dependency lookups through the Tooling API ``MetadataComponentDependency`` object.

Answers two questions about a component: *what uses it* and *what does it use*.

The underlying object does not track every kind of reference (dynamic Apex, references by
string, many standard fields, some declarative references), so an empty answer is not proof
that nothing depends on a component. The result always says so; pair it with a text search
of the retrieved metadata.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import InputValidationError
from .query import run_raw_query, soql_literal, strip_attributes
from .sf_cli import SfCli, validate_target_org

DIRECTIONS = ("used_by", "uses", "both")
MAX_DEPENDENCY_ROWS = 500

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,120}$")
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,60}$")

COVERAGE_NOTE = (
    "MetadataComponentDependency does not record every reference (for example dynamic Apex, "
    "names passed as strings, and many standard fields). Treat an empty list as 'none recorded', "
    "not 'none exist', and confirm with search_metadata on the retrieved source."
)


def _sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    # Sorted here rather than with ORDER BY: MetadataComponentDependency has restricted query support.
    return (str(entry.get("type") or ""), str(entry.get("name") or ""))


def find_dependencies(
    cli: SfCli,
    org: str,
    name: str,
    *,
    component_type: str | None = None,
    direction: str = "both",
    limit: int = 200,
) -> dict[str, Any]:
    """Return what uses ``name`` and/or what ``name`` uses.

    ``name`` is the component name as the dependency API stores it: the class, trigger or
    flow name, and for a custom field the field name without the object (``Tier__c`` becomes
    ``Tier``).
    """
    validate_target_org(org)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InputValidationError(f"Invalid component name: {name!r}.")
    if component_type is not None and not _TYPE_RE.match(str(component_type)):
        raise InputValidationError(f"Invalid component type: {component_type!r}.")
    if direction not in DIRECTIONS:
        raise InputValidationError(f"direction must be one of {DIRECTIONS}.")
    limit = max(1, min(int(limit), MAX_DEPENDENCY_ROWS))

    out: dict[str, Any] = {"component": name, "type": component_type, "direction": direction}

    if direction in ("used_by", "both"):
        where = f"RefMetadataComponentName = {soql_literal(name)}"
        if component_type:
            where += f" AND RefMetadataComponentType = {soql_literal(component_type)}"
        rows = strip_attributes(
            run_raw_query(
                cli,
                org,
                "SELECT MetadataComponentId, MetadataComponentName, MetadataComponentType, "
                "RefMetadataComponentType FROM MetadataComponentDependency "
                f"WHERE {where} LIMIT {limit}",
                tooling=True,
            )
        )
        out["used_by"] = [
            {
                "name": r.get("MetadataComponentName"),
                "type": r.get("MetadataComponentType"),
                "id": r.get("MetadataComponentId"),
            }
            for r in rows
        ]
        out["used_by"].sort(key=_sort_key)
        if len(rows) >= limit:
            out["used_by_truncated"] = True

    if direction in ("uses", "both"):
        where = f"MetadataComponentName = {soql_literal(name)}"
        if component_type:
            where += f" AND MetadataComponentType = {soql_literal(component_type)}"
        rows = strip_attributes(
            run_raw_query(
                cli,
                org,
                "SELECT RefMetadataComponentId, RefMetadataComponentName, RefMetadataComponentType, "
                "MetadataComponentType FROM MetadataComponentDependency "
                f"WHERE {where} LIMIT {limit}",
                tooling=True,
            )
        )
        out["uses"] = [
            {
                "name": r.get("RefMetadataComponentName"),
                "type": r.get("RefMetadataComponentType"),
                "id": r.get("RefMetadataComponentId"),
            }
            for r in rows
        ]
        out["uses"].sort(key=_sort_key)
        if len(rows) >= limit:
            out["uses_truncated"] = True

    out["coverage_note"] = COVERAGE_NOTE
    return out
