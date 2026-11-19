"""Org memory: what earlier investigations learned about an org, kept for the next one.

Every investigation reads parts of an org. This module keeps a map of what was seen, so the
next ticket on the same org starts from knowledge instead of from zero:

* **Components**: every retrieved class, trigger and flow with a content fingerprint and a
  static outline (methods, objects queried, fields written, classes called, a flow's trigger
  and the fields it tests or writes).
* **Objects**: which triggers, flows, validation rules and workflow rules run on an object.
* **Links**: which component uses which.
* **Notes**: facts the investigator chose to remember, with topics and the evidence they came
  from ("CaseEscalationService decides case priority from the account's support tier").

Memory is a **lead, never evidence**. Orgs change, so nothing here may be cited in a report: it
tells the investigator where to look, and the component is then read again from the org. To
make that safe, recalled knowledge is double-checked:

* at recall time, the org is asked for the last-modified date of each remembered component,
  and anything that changed since it was learned is flagged, together with the notes about it;
* at retrieve time, the fresh source is compared with the remembered fingerprint.

The store is local (``knowledge/<org id>.json``), is never committed, is keyed by org Id rather
than alias (an alias can be re-pointed), and holds metadata facts only: no record data. Notes
are refused when they contain an email address.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .errors import InputValidationError, SfInvestigatorError, WorkspaceError
from .flow_summary import summarize_flow_xml
from .query import run_raw_query, soql_literal, strip_attributes
from .sf_cli import SfCli

KNOWLEDGE_DIRNAME = "knowledge"
MAX_NOTE_CHARS = 1200
MAX_NOTES_PER_ORG = 2000
MAX_TOPICS = 12
SCHEMA_VERSION = 1
#: How long a writer waits for another writer, and when a lock left by a crashed process is taken over.
LOCK_WAIT_SECONDS = 15.0
LOCK_STALE_SECONDS = 60.0

#: Component types whose last-modified date can be read cheaply through the Tooling API.
CHECKABLE_TYPES = ("ApexClass", "ApexTrigger", "Flow")

FRESH = "unchanged"
CHANGED = "changed"
MISSING = "not_found_in_org"
UNKNOWN = "not_checked"

_ORG_ID_RE = re.compile(r"^00D[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_SPEC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*:[A-Za-z0-9_.\-]+$")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_STOP_WORDS = frozenset(
    "the a an and or of to in on for is are was were be not with by at as it this that from when "
    "does do did has have had but if then than so no yes can cannot our their they them some".split()
)

_APEX_METHOD_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|private|protected|global|static|virtual|override|abstract|"
    r"testMethod|webservice)\s+)+[\w.<>,\[\]\s]+?\s+(\w+)\s*\(",
)
_APEX_FROM_RE = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)
_APEX_ASSIGN_RE = re.compile(r"\b[a-z]\w*\.(\w+)\s*=(?!=)")
_APEX_CALL_RE = re.compile(r"\b([A-Z]\w+)\.(\w+)\s*\(")
_APEX_DML_RE = re.compile(r"^\s*(insert|update|upsert|delete|undelete|merge)\s+\w|\bDatabase\.(insert|update|upsert|delete)\s*\(",
                          re.IGNORECASE)
_TRIGGER_RE = re.compile(r"\btrigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
_PLATFORM_CLASSES = frozenset(
    "System Database Test Trigger Math String Integer Decimal Double Long Boolean Date Datetime Time Limits "
    "JSON Schema UserInfo Label Type Id List Set Map Blob Crypto EncodingUtil Url Http Messaging Assert "
    "Approval Site Network Pattern Matcher Savepoint SObjectType".split()
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def spec_of(component_type: Any, name: Any) -> str:
    return f"{component_type}:{name}"


def _tokens(text: str) -> list[str]:
    """Lower-case search tokens; camelCase and snake_case names are also split into their words."""
    out: list[str] = []
    for word in _WORD_RE.findall(text or ""):
        lowered = word.lower()
        parts = [p.lower() for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", word.replace("_", " "))]
        for token in [lowered, *parts]:
            if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
                token = token[:-1]  # "cases" and "case" are the same word for searching
            if len(token) > 1 and token not in _STOP_WORDS and token not in out:
                out.append(token)
    return out


# --- Static outlines ----------------------------------------------------------------------


def outline_apex(source: str, *, is_trigger: bool = False) -> dict[str, Any]:
    """Facts that can be read from Apex source without running it. Line numbers are 1-based."""
    from .apex_scan import strip_comments_and_strings  # local import: keep module load light

    clean = strip_comments_and_strings(source)
    methods: list[dict[str, Any]] = []
    objects: list[str] = []
    fields: list[str] = []
    calls: list[str] = []
    dml_lines: list[int] = []
    for number, line in enumerate(clean.splitlines(), start=1):
        match = _APEX_METHOD_RE.match(line)
        if match and match.group(1) not in ("if", "for", "while", "catch", "switch", "class", "new"):
            methods.append({"name": match.group(1), "line": number})
        for found in _APEX_FROM_RE.findall(line):
            if found not in objects:
                objects.append(found)
        for found in _APEX_ASSIGN_RE.findall(line):
            if found not in fields:
                fields.append(found)
        for class_name, method in _APEX_CALL_RE.findall(line):
            if class_name not in _PLATFORM_CLASSES:
                call = f"{class_name}.{method}"
                if call not in calls:
                    calls.append(call)
        if _APEX_DML_RE.search(line):
            dml_lines.append(number)
    out: dict[str, Any] = {
        "kind": "trigger" if is_trigger else "class",
        "lines": len(source.splitlines()),
        "is_test": bool(re.search(r"@isTest\b", source, re.IGNORECASE)),
        "methods": methods[:80],
        "objects_queried": objects[:40],
        "fields_assigned": fields[:60],
        "calls": calls[:80],
        "dml_lines": dml_lines[:40],
    }
    if is_trigger:
        trigger = _TRIGGER_RE.search(clean)
        if trigger:
            out["object"] = trigger.group(2)
            out["events"] = [e.strip() for e in trigger.group(3).split(",") if e.strip()]
    return out


def outline_flow(xml_text: str) -> dict[str, Any]:
    """The parts of a flow that say when it runs and what it reads and writes."""
    try:
        summary = summarize_flow_xml(xml_text)
    except SfInvestigatorError:
        return {"kind": "flow", "unreadable": True}
    start = summary.get("start") or {}
    reads: list[str] = []
    writes: list[str] = []
    actions: list[str] = []
    for element in summary.get("elements", []):
        for outcome in element.get("outcomes", []):
            for condition in outcome.get("conditions", []):
                reads.append(condition.split(" ", 1)[0])
        if element.get("kind") in ("update_records", "create_records", "delete_records"):
            target = element.get("object") or element.get("record_variable") or "?"
            for assignment in element.get("field_values", []):
                writes.append(f"{target}.{assignment.split(' ', 1)[0]}")
            if not element.get("field_values"):
                writes.append(str(target))
        if element.get("kind") == "action":
            actions.append(str(element.get("action_name")))
        if element.get("kind") == "subflow":
            actions.append(f"subflow:{element.get('flow_name')}")
    return {
        "kind": "flow",
        "label": summary.get("label"),
        "status": summary.get("status"),
        "object": start.get("object"),
        "trigger_type": start.get("trigger_type"),
        "record_trigger_type": start.get("record_trigger_type"),
        "entry_conditions": start.get("entry_conditions") or [],
        "reads": sorted(set(reads))[:40],
        "writes": sorted(set(writes))[:40],
        "actions": sorted(set(actions))[:40],
        "elements": len(summary.get("elements", [])),
    }


def outline_file(path: Path) -> dict[str, Any] | None:
    """Outline for a retrieved file, or ``None`` for files that carry no behaviour of their own."""
    name = path.name
    try:
        if name.endswith(".cls"):
            return outline_apex(path.read_text(encoding="utf-8", errors="replace"))
        if name.endswith(".trigger"):
            return outline_apex(path.read_text(encoding="utf-8", errors="replace"), is_trigger=True)
        if name.endswith(".flow-meta.xml"):
            return outline_flow(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return None


# --- Store --------------------------------------------------------------------------------


class OrgKnowledge:
    """Reads and writes the memory of every org under ``<root>/knowledge/``."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.dir = self.root / KNOWLEDGE_DIRNAME

    # -- files -----------------------------------------------------------------------------

    def _path(self, org_id: str) -> Path:
        if not isinstance(org_id, str) or not _ORG_ID_RE.match(org_id):
            raise InputValidationError(f"Invalid org Id for the org memory: {org_id!r}.")
        return self.dir / f"{org_id[:15]}.json"

    @contextmanager
    def _writing(self, org_id: str) -> Iterator[None]:
        """One writer at a time per org, across processes.

        The MCP server and the web page are separate programs and both write this file. Without
        this, the one that read first and saved last would undo what the other had learned in
        between. Every change is therefore: take the lock, read, change, save, release.
        """
        lock = self._path(org_id).with_suffix(".lock")
        self.dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    abandoned = time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS
                except OSError:
                    continue  # released between the two calls
                if abandoned:  # its writer died before releasing it
                    try:
                        lock.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise WorkspaceError(
                        "The org memory is being updated by another program and did not become free.",
                        hint="Try again in a moment. If it persists, delete the .lock file in the knowledge folder.",
                    ) from None
                time.sleep(0.03)
            except OSError:
                break  # a folder that cannot hold a lock file (read-only, odd file system): go on without one
            else:
                os.write(handle, str(os.getpid()).encode())
                os.close(handle)
                break
        try:
            yield
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def load(self, org_id: str) -> dict[str, Any]:
        path = self._path(org_id)
        empty: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "org_id": org_id[:15], "aliases": [], "created_at": None, "updated_at": None,
            "components": {}, "objects": {}, "edges": [], "notes": [],
        }
        if not path.is_file():
            return empty
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A damaged memory file must never stop an investigation: start again and keep the old file.
            try:
                path.replace(path.with_suffix(".damaged.json"))
            except OSError:
                pass
            return empty
        if not isinstance(data, dict):
            return empty
        for key, value in empty.items():
            data.setdefault(key, value)
        return data

    def save(self, data: dict[str, Any]) -> None:
        path = self._path(data["org_id"])
        self.dir.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        data["created_at"] = data.get("created_at") or now
        data["updated_at"] = now
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)

    def known_orgs(self) -> list[dict[str, Any]]:
        out = []
        if self.dir.is_dir():
            for path in sorted(self.dir.glob("00D*.json")):
                if path.name.endswith(".damaged.json"):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                out.append({
                    "org_id": data.get("org_id"), "aliases": data.get("aliases", []),
                    "components": len(data.get("components", {})), "notes": len(data.get("notes", [])),
                    "objects": len(data.get("objects", {})), "updated_at": data.get("updated_at"),
                })
        return out

    @staticmethod
    def _touch_alias(data: dict[str, Any], alias: str | None) -> None:
        if alias and alias not in data["aliases"]:
            data["aliases"].append(alias)

    # -- learning from tool calls ------------------------------------------------------------

    def record_retrieve(
        self,
        org_id: str,
        alias: str | None,
        repo_path: Path,
        files: Sequence[dict[str, Any]],
        *,
        ticket_key: str | None = None,
        last_modified: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Remember retrieved components. Returns what was new and what changed since last seen."""
        with self._writing(org_id):
            data = self.load(org_id)
            self._touch_alias(data, alias)
            now = utc_now()
            last_modified = last_modified or {}
            by_spec: dict[str, list[dict[str, Any]]] = {}
            for entry in files:
                if entry.get("state") == "Failed" or not entry.get("path") or not entry.get("type") or not entry.get("name"):
                    continue
                by_spec.setdefault(spec_of(entry["type"], entry["name"]), []).append(entry)

            new, changed, unchanged = [], [], []
            for spec, entries in sorted(by_spec.items()):
                digest = hashlib.sha256()
                outline = None
                main_path = None
                for entry in sorted(entries, key=lambda e: str(e["path"])):
                    path = Path(repo_path) / str(entry["path"])
                    try:
                        content = path.read_bytes()
                    except OSError:
                        continue
                    digest.update(str(entry["path"]).encode())
                    digest.update(b"\0")
                    digest.update(content)
                    found = outline_file(path)
                    if found is not None:
                        outline, main_path = found, str(entry["path"])
                    elif main_path is None:
                        main_path = str(entry["path"])
                fingerprint = digest.hexdigest()
                previous = data["components"].get(spec)
                previous_tickets = list((previous or {}).get("seen_in_tickets", []))
                component: dict[str, Any] = dict(previous or {})
                component["seen_in_tickets"] = list(previous_tickets)
                component.update({
                    "type": spec.split(":", 1)[0], "name": spec.split(":", 1)[1],
                    "path": f"metadata/{main_path}" if main_path else None,
                    "sha256": fingerprint, "last_seen": now, "last_verified": now,
                })
                component.setdefault("first_seen", now)
                if outline is not None:
                    component["outline"] = outline
                if spec in last_modified:
                    component["org_last_modified"] = last_modified[spec]
                tickets = component.setdefault("seen_in_tickets", [])
                if ticket_key and ticket_key not in tickets:
                    tickets.append(ticket_key)
                if previous is None:
                    new.append(spec)
                elif previous.get("sha256") != fingerprint:
                    component.setdefault("history", []).append(
                        {"at": now, "before": previous.get("sha256"), "after": fingerprint, "ticket_key": ticket_key})
                    component["history"] = component["history"][-20:]
                    changed.append({"component": spec, "last_seen": previous.get("last_seen"),
                                    "seen_in_tickets": previous_tickets})
                else:
                    unchanged.append(spec)
                data["components"][spec] = component
                self._link_from_outline(data, spec, outline)

            stale_notes = self._refresh_note_staleness(data)
            self.save(data)
        out: dict[str, Any] = {"new": new, "changed_since_last_seen": changed, "unchanged": len(unchanged)}
        if changed:
            affected = [n["id"] for n in data["notes"] if n.get("stale") and
                        set(n.get("components", [])) & {c["component"] for c in changed}]
            out["notes_now_flagged_stale"] = affected
            out["note"] = (
                "These components differ from what was remembered for this org. Anything remembered about "
                "them is flagged stale: read them again before relying on it."
            )
        elif stale_notes:
            out["stale_notes"] = stale_notes
        return out

    @staticmethod
    def _link_from_outline(data: dict[str, Any], spec: str, outline: dict[str, Any] | None) -> None:
        if not outline:
            return
        known_classes = {c["name"] for c in data["components"].values() if c.get("type") == "ApexClass"}
        targets = set()
        for call in outline.get("calls", []):
            class_name = call.split(".", 1)[0]
            if class_name in known_classes and f"ApexClass:{class_name}" != spec:
                targets.add(f"ApexClass:{class_name}")
        OrgKnowledge._replace_edges(data, spec, "calls", "source", targets)
        if outline.get("object"):
            entry = data["objects"].setdefault(outline["object"], {})
            bucket = "flows" if outline.get("kind") == "flow" else "triggers" if outline.get("kind") == "trigger" else None
            if bucket:
                names = entry.setdefault(bucket, [])
                name = spec.split(":", 1)[1]
                if name not in names:
                    names.append(name)

    @staticmethod
    def _replace_edges(data: dict[str, Any], source: str, kind: str, origin: str, targets: Iterable[str]) -> None:
        data["edges"] = [e for e in data["edges"]
                         if not (e.get("from") == source and e.get("kind") == kind and e.get("origin") == origin)]
        for target in sorted(set(targets)):
            data["edges"].append({"from": source, "to": target, "kind": kind, "origin": origin})

    def record_automation(self, org_id: str, alias: str | None, sobject: str, automation: dict[str, Any]) -> None:
        """Remember which triggers, flows and rules run on an object (from ``describe_object``)."""
        with self._writing(org_id):
            data = self.load(org_id)
            self._touch_alias(data, alias)
            entry = data["objects"].setdefault(sobject, {})

            def names(key: str, field: str) -> list[str] | None:
                rows = automation.get(key)
                if not isinstance(rows, list):
                    return None  # that section failed: keep what was known
                return sorted({str(r.get(field)) for r in rows if isinstance(r, dict) and r.get(field)})

            for bucket, key, field in (("triggers", "triggers", "Name"), ("flows", "record_triggered_flows", "ApiName"),
                                       ("validation_rules", "validation_rules", "ValidationName"),
                                       ("workflow_rules", "workflow_rules", "Name")):
                found = names(key, field)
                if found is not None:
                    entry[bucket] = found
            entry["automation_checked_at"] = utc_now()
            self.save(data)

    def record_dependencies(self, org_id: str, alias: str | None, name: str, component_type: str | None,
                            result: dict[str, Any]) -> None:
        """Remember "uses" links reported by the dependency API."""
        with self._writing(org_id):
            data = self.load(org_id)
            self._touch_alias(data, alias)
            subject_type = component_type
            if not subject_type:
                subject_type = next((c["type"] for c in data["components"].values() if c.get("name") == name), None)
            if not subject_type:
                return
            subject = spec_of(subject_type, name)
            if "uses" in result:
                self._replace_edges(data, subject, "uses", "dependency_api",
                                    [spec_of(d.get("type"), d.get("name")) for d in result["uses"] if d.get("name")])
            for user in result.get("used_by", []):
                if user.get("name") and user.get("type"):
                    source = spec_of(user["type"], user["name"])
                    edge = {"from": source, "to": subject, "kind": "uses", "origin": "dependency_api"}
                    if edge not in data["edges"]:
                        data["edges"].append(edge)
            self.save(data)

    # -- notes -------------------------------------------------------------------------------

    def add_note(
        self,
        org_id: str,
        alias: str | None,
        text: str,
        *,
        topics: Sequence[str] = (),
        components: Sequence[str] = (),
        evidence: Sequence[str] = (),
        ticket_key: str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Remember one fact about the org. Returns the stored note."""
        if not isinstance(text, str) or len(text.strip()) < 15:
            raise InputValidationError("A note needs at least one full sentence about the org.")
        text = re.sub(r"\s+", " ", text.strip())
        if len(text) > MAX_NOTE_CHARS:
            raise InputValidationError(f"A note is at most {MAX_NOTE_CHARS} characters. Split it into separate facts.")
        if _EMAIL_RE.search(text) or any(_EMAIL_RE.search(str(t)) for t in topics):
            raise InputValidationError(
                "The note contains an email address. The org memory holds facts about metadata, never personal data.",
                hint="Refer to users and records by Id.",
            )
        clean_topics: list[str] = []
        for topic in topics or ():
            topic = re.sub(r"\s+", " ", str(topic).strip().lower())
            if topic and len(topic) <= 60 and topic not in clean_topics:
                clean_topics.append(topic)
        if len(clean_topics) > MAX_TOPICS:
            raise InputValidationError(f"At most {MAX_TOPICS} topics per note.")
        specs: list[str] = []
        for spec in components or ():
            if not isinstance(spec, str) or not _SPEC_RE.match(spec):
                raise InputValidationError(f"Invalid component reference: {spec!r}.", hint="Use Type:Name.")
            if spec not in specs:
                specs.append(spec)
        clean_evidence = [str(e).strip("` ")[:300] for e in (evidence or ()) if str(e).strip("` ")][:20]

        with self._writing(org_id):
            data = self.load(org_id)
            self._touch_alias(data, alias)
            if len(data["notes"]) >= MAX_NOTES_PER_ORG:
                raise InputValidationError("The org memory is full. Remove notes that no longer hold first.")
            if supersedes:
                old = next((n for n in data["notes"] if n.get("id") == supersedes), None)
                if old is None:
                    raise InputValidationError(f"No note with id {supersedes!r} for this org.")
                old["superseded_by"] = "pending"
            note = {
                "id": "N-" + uuid.uuid4().hex[:8],
                "text": text,
                "topics": clean_topics,
                "components": specs,
                "evidence": clean_evidence,
                "ticket_key": ticket_key,
                "created_at": utc_now(),
                # What each component looked like when this was learned, to detect later change.
                "fingerprints": {s: data["components"].get(s, {}).get("sha256") for s in specs},
                "stale": False,
            }
            if supersedes:
                for old in data["notes"]:
                    if old.get("superseded_by") == "pending":
                        old["superseded_by"] = note["id"]
            data["notes"].append(note)
            self.save(data)
        unknown = [s for s in specs if s not in data["components"]]
        out = dict(note)
        if unknown:
            out["warning"] = (
                "Not retrieved for this org yet, so changes to them cannot be detected: " + ", ".join(unknown)
            )
        return out

    def remove_note(self, org_id: str, note_id: str) -> dict[str, Any]:
        with self._writing(org_id):
            data = self.load(org_id)
            before = len(data["notes"])
            data["notes"] = [n for n in data["notes"] if n.get("id") != note_id]
            if len(data["notes"]) == before:
                raise InputValidationError(f"No note with id {note_id!r} for this org.")
            self.save(data)
        return {"removed": note_id, "notes_left": len(data["notes"])}

    @staticmethod
    def _refresh_note_staleness(data: dict[str, Any], org_changed: Iterable[str] = ()) -> list[str]:
        """Flag notes whose components no longer match what they looked like when the note was written."""
        org_changed = set(org_changed)
        stale: list[str] = []
        for note in data["notes"]:
            reasons = []
            for spec, fingerprint in (note.get("fingerprints") or {}).items():
                current = data["components"].get(spec, {}).get("sha256")
                if fingerprint and current and fingerprint != current:
                    reasons.append(f"{spec} was retrieved again and differs from when this was learned")
                if spec in org_changed:
                    reasons.append(f"{spec} was modified in the org after this was learned")
            note["stale"] = bool(reasons)
            if reasons:
                note["stale_because"] = sorted(set(reasons))
                stale.append(note["id"])
            else:
                note.pop("stale_because", None)
        return stale

    # -- double check against the org --------------------------------------------------------

    def verify(self, cli: SfCli, org: str, org_id: str, *, components: Sequence[str] | None = None) -> dict[str, Any]:
        """Ask the org whether remembered components changed since they were learned.

        Compares the last-modified date stored when a component was last retrieved with the
        org's current one. Read-only: a few Tooling API queries.
        """
        known = self.load(org_id)["components"]
        asked = [s for s in (components or sorted(known)) if s in known]
        results: dict[str, dict[str, Any]] = {}
        by_type: dict[str, list[str]] = {}
        for spec in asked:
            by_type.setdefault(spec.split(":", 1)[0], []).append(spec.split(":", 1)[1])
        current: dict[str, str | None] = {}
        errors: dict[str, str] = {}
        for component_type, names in by_type.items():
            if component_type not in CHECKABLE_TYPES:
                continue
            try:
                current.update(fetch_last_modified(cli, org, component_type, names))
            except SfInvestigatorError as exc:
                errors[component_type] = exc.message

        # The org was asked without holding the lock (it takes seconds). The file is read again
        # now, so that what another program learned in the meantime is kept.
        now = utc_now()
        changed_specs: list[str] = []
        with self._writing(org_id):
            data = self.load(org_id)
            wanted = [s for s in asked if s in data["components"]]
            stale_notes = self._apply_check(data, wanted, current, errors, now, results, changed_specs)
            self.save(data)
        counts: dict[str, int] = {}
        for entry in results.values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return {"checked_at": now, "counts": counts, "components": results, "stale_notes": stale_notes}

    def _apply_check(self, data: dict[str, Any], wanted: Sequence[str], current: dict[str, str | None],
                     errors: dict[str, str], now: str, results: dict[str, dict[str, Any]],
                     changed_specs: list[str]) -> list[str]:
        """Write the outcome of a check into ``data``; returns the notes it flags."""
        for spec in wanted:
            component = data["components"][spec]
            component_type = spec.split(":", 1)[0]
            remembered = component.get("org_last_modified")
            entry: dict[str, Any] = {"remembered_last_modified": remembered, "last_seen": component.get("last_seen")}
            if component_type not in CHECKABLE_TYPES:
                entry.update(status=UNKNOWN, reason="This type has no cheap change check; retrieve it to compare.")
            elif component_type in errors:
                entry.update(status=UNKNOWN, reason=f"The org could not be asked: {errors[component_type]}")
            elif spec not in current:
                entry.update(status=MISSING, reason="The org no longer has a component with this name.")
                changed_specs.append(spec)
            elif current[spec] is None and component_type == "Flow":
                if remembered:
                    entry.update(status=CHANGED, active=False,
                                 reason="The flow is still in the org but no version of it is active any more: it "
                                 "was deactivated after it was learned. Retrieve and read it again.")
                    changed_specs.append(spec)
                else:
                    entry.update(status=UNKNOWN, active=False, reason="The flow is in the org but has no active version.")
            elif not remembered:
                entry.update(status=UNKNOWN, org_last_modified=current[spec],
                             reason="No last-modified date was stored when it was learned; retrieve it to compare.")
            elif _same_moment(remembered, current[spec]):
                entry.update(status=FRESH, org_last_modified=current[spec])
                component["last_verified"] = now
            else:
                entry.update(status=CHANGED, org_last_modified=current[spec],
                             reason="Modified in the org since it was learned. Retrieve and read it again.")
                changed_specs.append(spec)
            component["last_check"] = {"at": now, "status": entry["status"]}
            results[spec] = entry

        return self._refresh_note_staleness(data, changed_specs)

    # -- recall ------------------------------------------------------------------------------

    def search(self, org_id: str, query: str, *, limit: int = 10) -> dict[str, Any]:
        """Rank remembered notes, components and objects against the words of ``query``."""
        if not isinstance(query, str) or not query.strip():
            raise InputValidationError("Say what you are looking for, for example 'case priority premium'.")
        data = self.load(org_id)
        wanted = _tokens(query)
        limit = max(1, min(int(limit), 50))

        def score(text: str, weight: float = 1.0) -> float:
            haystack = set(_tokens(text))
            return weight * sum(1 for token in wanted if token in haystack)

        notes = []
        for note in data["notes"]:
            if note.get("superseded_by"):
                continue
            value = score(note["text"]) + score(" ".join(note.get("topics", [])), 2.0) + \
                score(" ".join(note.get("components", [])), 1.5)
            if value > 0:
                notes.append((value, note))
        notes.sort(key=lambda pair: (-pair[0], pair[1].get("created_at") or ""))
        # A component that a matching note is about is more relevant than one that only shares a word.
        boosted: dict[str, float] = {}
        for value, note in notes:
            for spec in note.get("components", []):
                boosted[spec] = boosted.get(spec, 0.0) + value

        components = []
        for spec, component in data["components"].items():
            outline = component.get("outline") or {}
            text = " ".join([
                spec, str(outline.get("object") or ""), str(outline.get("label") or ""),
                " ".join(outline.get("objects_queried", [])), " ".join(outline.get("fields_assigned", [])),
                " ".join(outline.get("calls", [])), " ".join(m["name"] for m in outline.get("methods", [])),
                " ".join(outline.get("reads", [])), " ".join(outline.get("writes", [])),
                " ".join(outline.get("actions", [])),
            ])
            value = score(spec, 2.0) + score(text) + boosted.get(spec, 0.0)
            if value > 0:
                components.append((value, spec, component))
        components.sort(key=lambda item: (-item[0], item[1]))

        objects = []
        for name, entry in data["objects"].items():
            text = " ".join([name, *entry.get("triggers", []), *entry.get("flows", []),
                             *entry.get("validation_rules", []), *entry.get("workflow_rules", [])])
            value = score(name, 2.0) + score(text)
            if value > 0:
                objects.append((value, name, entry))
        objects.sort(key=lambda item: (-item[0], item[1]))

        return {
            "query_words": wanted,
            "notes": [note for _, note in notes[:limit]],
            "components": [{"component": spec, **_component_view(component)} for _, spec, component in components[:limit]],
            "objects": [{"object": name, **entry} for _, name, entry in objects[:limit]],
            "totals": {"notes": len(data["notes"]), "components": len(data["components"]),
                       "objects": len(data["objects"])},
        }

    def org_map(self, org_id: str) -> dict[str, Any]:
        """Everything remembered about one org, arranged for reading."""
        data = self.load(org_id)
        by_type: dict[str, list[dict[str, Any]]] = {}
        for spec, component in sorted(data["components"].items()):
            by_type.setdefault(component.get("type") or spec.split(":", 1)[0], []).append(
                {"component": spec, **_component_view(component)})
        topics: dict[str, int] = {}
        for note in data["notes"]:
            if note.get("superseded_by"):
                continue
            for topic in note.get("topics", []):
                topics[topic] = topics.get(topic, 0) + 1
        tickets = sorted({t for c in data["components"].values() for t in c.get("seen_in_tickets", [])} |
                         {n["ticket_key"] for n in data["notes"] if n.get("ticket_key")})
        return {
            "org_id": data["org_id"],
            "aliases": data["aliases"],
            "updated_at": data.get("updated_at"),
            "counts": {"components": len(data["components"]), "objects": len(data["objects"]),
                       "links": len(data["edges"]),
                       "notes": sum(1 for n in data["notes"] if not n.get("superseded_by")),
                       "stale_notes": sum(1 for n in data["notes"] if n.get("stale") and not n.get("superseded_by"))},
            "objects": {name: entry for name, entry in sorted(data["objects"].items())},
            "components_by_type": by_type,
            "links": sorted(data["edges"], key=lambda e: (e.get("from", ""), e.get("to", ""))),
            "topics": dict(sorted(topics.items(), key=lambda kv: (-kv[1], kv[0]))),
            "notes": [n for n in data["notes"] if not n.get("superseded_by")],
            "tickets": tickets,
        }


def _component_view(component: dict[str, Any]) -> dict[str, Any]:
    outline = component.get("outline") or {}
    view: dict[str, Any] = {
        "path": component.get("path"),
        "first_seen": component.get("first_seen"),
        "last_seen": component.get("last_seen"),
        "last_verified": component.get("last_verified"),
        "org_last_modified": component.get("org_last_modified"),
        "seen_in_tickets": component.get("seen_in_tickets", []),
        "times_changed": len(component.get("history", [])),
    }
    if component.get("last_check"):
        view["last_check"] = component["last_check"]
    if outline:
        view["outline"] = outline
    return view


def _same_moment(a: str | None, b: str | None) -> bool:
    """Compare two Salesforce timestamps to the second, whatever their fractional or zone format."""
    if not a or not b:
        return False
    return a[:19] == b[:19]


def fetch_last_modified(cli: SfCli, org: str, component_type: str, names: Sequence[str]) -> dict[str, str | None]:
    """Last-modified dates of components of one type, keyed by ``Type:Name``. Read-only."""
    names = [n for n in dict.fromkeys(names) if re.match(r"^[A-Za-z0-9_]{1,80}$", n)]
    out: dict[str, str | None] = {}
    if not names:
        return out
    for start in range(0, len(names), 100):
        chunk = names[start:start + 100]
        listed = ", ".join(soql_literal(n) for n in chunk)
        if component_type in ("ApexClass", "ApexTrigger"):
            # NamespacePrefix = null: a managed package's class of the same name is another component.
            rows = strip_attributes(run_raw_query(
                cli, org, f"SELECT Name, LastModifiedDate FROM {component_type} WHERE Name IN ({listed}) "
                          "AND NamespacePrefix = null LIMIT 200", tooling=True))
            for row in rows:
                out[spec_of(component_type, row.get("Name"))] = row.get("LastModifiedDate")
        elif component_type == "Flow":
            # Asked by name: an org can have far more active flows than one page of an unfiltered query.
            definitions = strip_attributes(run_raw_query(
                cli, org, f"SELECT ApiName, ActiveVersionId FROM FlowDefinitionView WHERE ApiName IN ({listed}) "
                          "AND NamespacePrefix = null LIMIT 200"))
            active = {str(r["ActiveVersionId"]): str(r.get("ApiName")) for r in definitions if r.get("ActiveVersionId")}
            dates: dict[str, str | None] = {}
            if active:
                versions = strip_attributes(run_raw_query(
                    cli, org, "SELECT Id, LastModifiedDate FROM Flow WHERE Id IN "
                              f"({', '.join(soql_literal(i) for i in active)}) LIMIT 200", tooling=True))
                for row in versions:
                    name = active.get(str(row.get("Id"))) or active.get(next(
                        (i for i in active if i[:15] == str(row.get("Id"))[:15]), ""))
                    if name:
                        dates[name] = row.get("LastModifiedDate")
            for row in definitions:
                # None: the flow is in the org but has no active version.
                out[spec_of("Flow", row.get("ApiName"))] = dates.get(str(row.get("ApiName")))
    return out
