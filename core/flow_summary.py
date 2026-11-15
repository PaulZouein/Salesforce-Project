"""Readable outline of a Flow definition (``*.flow-meta.xml``).

Flow XML is hard to read. This produces the parts that matter for a root-cause
investigation: what starts the flow and under which entry conditions, each decision with
its conditions, what records are read, created, updated or deleted, which Apex actions and
subflows are called, and how the elements connect. Each element is returned with the line
number of its ``<name>`` tag so it can be cited.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .errors import InputValidationError

NS = "{http://soap.sforce.com/2006/04/metadata}"

ELEMENT_KINDS = {
    "decisions": "decision",
    "assignments": "assignment",
    "recordLookups": "get_records",
    "recordCreates": "create_records",
    "recordUpdates": "update_records",
    "recordDeletes": "delete_records",
    "actionCalls": "action",
    "subflows": "subflow",
    "loops": "loop",
    "screens": "screen",
    "waits": "wait",
    "collectionProcessors": "collection_processor",
    "transforms": "transform",
    "customErrors": "custom_error",
    "recordRollbacks": "rollback",
}


def _tag(element: ET.Element) -> str:
    return element.tag.replace(NS, "")


def _text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    child = element.find(f"{NS}{name}")
    return child.text.strip() if child is not None and child.text else None


def _value(element: ET.Element | None) -> Any:
    """Render a Flow value element (stringValue, booleanValue, elementReference, …)."""
    if element is None:
        return None
    for child in element:
        text = (child.text or "").strip()
        name = _tag(child)
        if name == "elementReference":
            return f"{{!{text}}}"
        if name == "booleanValue":
            return text.lower() == "true"
        if name == "numberValue":
            try:
                return float(text) if "." in text else int(text)
            except ValueError:
                return text
        return text
    return None


def _condition(element: ET.Element) -> str:
    left = _text(element, "leftValueReference") or _text(element, "field") or "?"
    operator = _text(element, "operator") or "?"
    right = _value(element.find(f"{NS}rightValue"))
    if right is None:
        right = _value(element.find(f"{NS}value"))
    return f"{left} {operator} {right!r}"


def _connector(element: ET.Element | None, name: str = "connector") -> str | None:
    if element is None:
        return None
    connector = element.find(f"{NS}{name}")
    return _text(connector, "targetReference") if connector is not None else None


def _assignments(element: ET.Element, tag: str) -> list[str]:
    out = []
    for item in element.findall(f"{NS}{tag}"):
        target = _text(item, "assignToReference") or _text(item, "field") or "?"
        operator = _text(item, "operator")
        value = _value(item.find(f"{NS}value"))
        out.append(f"{target} {operator or '='} {value!r}")
    return out


def summarize_flow_xml(text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise InputValidationError(f"The file is not valid XML: {exc}.") from None
    if _tag(root) != "Flow":
        raise InputValidationError("The file is not a Flow definition (root element is not <Flow>).")

    lines = text.splitlines()

    def line_of(name: str | None) -> int | None:
        if not name:
            return None
        needle = f"<name>{name}</name>"
        for number, line in enumerate(lines, start=1):
            if needle in line:
                return number
        return None

    out: dict[str, Any] = {
        "label": _text(root, "label"),
        "process_type": _text(root, "processType"),
        "status": _text(root, "status"),
        "api_version": _text(root, "apiVersion"),
        "description": _text(root, "description"),
    }

    start = root.find(f"{NS}start")
    if start is not None:
        out["start"] = {
            "object": _text(start, "object"),
            "trigger_type": _text(start, "triggerType"),
            "record_trigger_type": _text(start, "recordTriggerType"),
            "entry_filter_logic": _text(start, "filterLogic"),
            "entry_conditions": [_condition(f) for f in start.findall(f"{NS}filters")],
            "entry_formula": _text(start, "filterFormula"),
            "run_only_when_changed_to_meet_criteria": _text(start, "doesRequireRecordChangedToMeetCriteria"),
            "first_element": _connector(start),
            "scheduled_paths": [
                {"name": _text(p, "name"), "offset": _text(p, "offsetNumber"), "unit": _text(p, "offsetUnit"),
                 "first_element": _connector(p)}
                for p in start.findall(f"{NS}scheduledPaths")
            ],
        }

    elements: list[dict[str, Any]] = []
    for tag, kind in ELEMENT_KINDS.items():
        for element in root.findall(f"{NS}{tag}"):
            name = _text(element, "name")
            entry: dict[str, Any] = {"name": name, "kind": kind, "label": _text(element, "label"),
                                     "line": line_of(name)}
            if kind == "decision":
                entry["outcomes"] = [
                    {
                        "name": _text(rule, "name"),
                        "label": _text(rule, "label"),
                        "logic": _text(rule, "conditionLogic"),
                        "conditions": [_condition(c) for c in rule.findall(f"{NS}conditions")],
                        "next": _connector(rule),
                        "line": line_of(_text(rule, "name")),
                    }
                    for rule in element.findall(f"{NS}rules")
                ]
                entry["default_outcome"] = {
                    "label": _text(element, "defaultConnectorLabel"),
                    "next": _connector(element, "defaultConnector"),
                }
            elif kind == "assignment":
                entry["assignments"] = _assignments(element, "assignmentItems")
                entry["next"] = _connector(element)
            elif kind in ("get_records", "update_records", "delete_records", "create_records"):
                entry["object"] = _text(element, "object")
                entry["record_variable"] = _text(element, "inputReference") or _text(element, "outputReference")
                entry["filter_logic"] = _text(element, "filterLogic")
                entry["filters"] = [_condition(f) for f in element.findall(f"{NS}filters")]
                entry["field_values"] = _assignments(element, "inputAssignments")
                entry["next"] = _connector(element)
                entry["fault_path"] = _connector(element, "faultConnector")
                if kind == "get_records":
                    entry["first_record_only"] = _text(element, "getFirstRecordOnly")
            elif kind == "action":
                entry["action_name"] = _text(element, "actionName")
                entry["action_type"] = _text(element, "actionType")
                entry["inputs"] = [
                    f"{_text(p, 'name')} = {_value(p.find(f'{NS}value'))!r}"
                    for p in element.findall(f"{NS}inputParameters")
                ]
                entry["next"] = _connector(element)
                entry["fault_path"] = _connector(element, "faultConnector")
            elif kind == "subflow":
                entry["flow_name"] = _text(element, "flowName")
                entry["inputs"] = _assignments(element, "inputAssignments")
                entry["next"] = _connector(element)
            elif kind == "loop":
                entry["collection"] = _text(element, "collectionReference")
                entry["for_each"] = _connector(element, "nextValueConnector")
                entry["after_last"] = _connector(element, "noMoreValuesConnector")
            elif kind == "custom_error":
                entry["messages"] = [
                    (_text(m, "errorMessage") or "")[:300] for m in element.findall(f"{NS}customErrorMessages")
                ]
                entry["next"] = _connector(element)
            else:
                entry["next"] = _connector(element)
            elements.append({k: v for k, v in entry.items() if v not in (None, [], "")})

    by_name = {e["name"]: e for e in elements if e.get("name")}
    order: list[str] = []
    queue = [out.get("start", {}).get("first_element")] + [
        p.get("first_element") for p in out.get("start", {}).get("scheduled_paths", [])
    ]
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if not name or name in seen or name not in by_name:
            continue
        seen.add(name)
        order.append(name)
        element = by_name[name]
        nexts = [element.get("next"), element.get("for_each"), element.get("after_last"), element.get("fault_path")]
        nexts += [o.get("next") for o in element.get("outcomes", [])]
        nexts.append((element.get("default_outcome") or {}).get("next"))
        queue.extend(n for n in nexts if n)
    out["execution_order"] = order
    unreachable = [e["name"] for e in elements if e.get("name") and e["name"] not in seen]
    if unreachable:
        out["unreachable_elements"] = unreachable
    out["elements"] = sorted(elements, key=lambda e: order.index(e["name"]) if e.get("name") in seen else 10_000)

    out["formulas"] = [
        {"name": _text(f, "name"), "data_type": _text(f, "dataType"), "expression": (_text(f, "expression") or "")[:600],
         "line": line_of(_text(f, "name"))}
        for f in root.findall(f"{NS}formulas")
    ]
    out["variables"] = [
        {"name": _text(v, "name"), "data_type": _text(v, "dataType"), "object": _text(v, "objectType"),
         "is_collection": _text(v, "isCollection"), "is_input": _text(v, "isInput"), "is_output": _text(v, "isOutput")}
        for v in root.findall(f"{NS}variables")
    ]

    notes = []
    if out.get("status") and out["status"] != "Active":
        notes.append(f"This flow version has status {out['status']!r}; it does not run unless it is Active.")
    writes = [e for e in elements if e["kind"] in ("update_records", "create_records", "delete_records")]
    no_fault = [e["name"] for e in writes if not e.get("fault_path")]
    if no_fault:
        notes.append("Data elements without a fault path (an error here fails the whole transaction): "
                     + ", ".join(no_fault))
    out["notes"] = notes
    return out


def summarize_flow_file(path: Path) -> dict[str, Any]:
    if not re.search(r"\.flow(-meta\.xml)?$", path.name):
        raise InputValidationError(f"{path.name} is not a flow file (*.flow-meta.xml).")
    return summarize_flow_xml(path.read_text(encoding="utf-8", errors="replace"))
