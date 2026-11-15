"""A stand-in for the ``sf`` executable, backed by files instead of a Salesforce org.

It exists so that the tests and the offline demo exercise the real code path (subprocess,
JSON envelopes, exit codes) without an org. It implements only the commands and flags the
investigator uses, and returns the same JSON shapes as ``@salesforce/cli`` 2.158.

It does **not** execute Apex. Apex test outcomes are decided by simple rules in
``tests.json`` (for example "this method fails while the service still contains this
pattern"), which is enough to show a test failing before a fix and passing after it.

State layout (``SFTI_SIM_STATE``)::

    orgs/<alias>/org.json       org identity: orgId, username, isSandbox, organizationType…
    orgs/<alias>/source/        the org's metadata in source format (main/default/…)
    orgs/<alias>/records.json   {"<Object>": [records…]} for SOQL and Tooling queries
    orgs/<alias>/tests.json     rules for simulated Apex test outcomes
    orgs/<alias>/logs/<id>.log  debug logs
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

VERSION = "@salesforce/cli/2.158.7-simulated"

TYPE_DIRS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ApexClass": ("classes", (".cls", ".cls-meta.xml")),
    "ApexTrigger": ("triggers", (".trigger", ".trigger-meta.xml")),
    "Flow": ("flows", (".flow-meta.xml",)),
}
CHILD_TYPES: dict[str, tuple[str, str]] = {
    "CustomField": ("fields", ".field-meta.xml"),
    "ValidationRule": ("validationRules", ".validationRule-meta.xml"),
}


class CliError(Exception):
    def __init__(self, name: str, message: str, exit_code: int = 1, data: Any = None):
        super().__init__(message)
        self.name, self.message, self.exit_code, self.data = name, message, exit_code, data


# --- State ------------------------------------------------------------------------------


def state_dir() -> Path:
    env = os.environ.get("SFTI_SIM_STATE")
    return Path(env) if env else Path(__file__).resolve().parent / "state"


def all_orgs() -> list[tuple[Path, dict[str, Any]]]:
    root = state_dir() / "orgs"
    out = []
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            org_file = folder / "org.json"
            if org_file.is_file():
                out.append((folder, json.loads(org_file.read_text(encoding="utf-8"))))
    return out


def find_org(target: str | None) -> tuple[Path, dict[str, Any]]:
    if not target:
        raise CliError("NoDefaultEnvError", "No default environment found. Use -o or --target-org to specify an environment.", 1)
    for folder, org in all_orgs():
        if target in (org.get("alias"), org.get("username")):
            return folder, org
    raise CliError(
        "NamedOrgNotFoundError",
        f"Parsing --target-org \n\tNo authorization information found for {target}.\nSee more help with --help",
        2,
    )


def load_records(folder: Path) -> dict[str, list[dict[str, Any]]]:
    path = folder / "records.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_records(folder: Path, records: dict[str, list[dict[str, Any]]]) -> None:
    (folder / "records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


# --- Arguments --------------------------------------------------------------------------

MULTI_FLAGS = {"--metadata", "--class-names", "--tests", "--header"}
BOOL_FLAGS = {
    "--json", "--use-tooling-api", "--all-rows", "--ignore-conflicts", "--dry-run", "--code-coverage",
    "--synchronous", "--no-prompt", "--skip-connection-status", "--all", "--verbose",
}


def parse_args(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    positional: list[str] = []
    flags: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name, _, inline = arg.partition("=")
            if name in BOOL_FLAGS:
                flags[name] = True
            else:
                if inline:
                    value = inline
                else:
                    i += 1
                    if i >= len(argv):
                        raise CliError("Error", f"Flag {name} expects a value", 2)
                    value = argv[i]
                if name in MULTI_FLAGS:
                    flags.setdefault(name, []).append(value)
                else:
                    flags[name] = value
        else:
            positional.append(arg)
        i += 1
    return positional, flags


# --- SOQL subset ------------------------------------------------------------------------

_QUERY_RE = re.compile(
    r"^\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<object>\w+)"
    r"(?:\s+WHERE\s+(?P<where>.+?))?"
    r"(?:\s+ORDER\s+BY\s+(?P<order>.+?))?"
    r"(?:\s+LIMIT\s+(?P<limit>\d+))?"
    r"(?:\s+OFFSET\s+\d+)?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CONDITION_RE = re.compile(
    r"^\s*(?P<field>[\w.]+)\s*(?P<op>NOT\s+IN|IN|!=|<>|>=|<=|=|>|<|LIKE)\s*(?P<value>.+?)\s*$", re.IGNORECASE | re.DOTALL
)


def _literal(text: str) -> Any:
    text = text.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    lowered = text.lower()
    if lowered == "null":
        return None
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return float(text) if "." in text and not re.match(r"^\d{4}-", text) else int(text)
    except ValueError:
        return text  # date and datetime literals


def _split_and(where: str) -> list[str]:
    parts, depth, quote, current = [], 0, False, []
    tokens = re.split(r"(\s+AND\s+)", where, flags=re.IGNORECASE)
    for token in tokens:
        if re.fullmatch(r"\s+AND\s+", token, flags=re.IGNORECASE) and depth == 0 and not quote:
            parts.append("".join(current))
            current = []
            continue
        for ch in token:
            if ch == "'":
                quote = not quote
            elif not quote and ch == "(":
                depth += 1
            elif not quote and ch == ")":
                depth -= 1
        current.append(token)
    parts.append("".join(current))
    return [p for p in parts if p.strip()]


def _resolve(record: dict[str, Any], path: str, records: dict[str, list[dict[str, Any]]]) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        key = next((k for k in current if k.lower() == part.lower()), None)
        if key is not None:
            current = current[key]
            continue
        # Relationship: follow <Name>Id to another record.
        id_key = next((k for k in current if k.lower() == part.lower() + "id"), None)
        if id_key is None or current[id_key] is None:
            return None
        target = None
        for rows in records.values():
            target = next((r for r in rows if r.get("Id") == current[id_key]), None)
            if target:
                break
        current = target
    return current


def _norm(value: Any) -> Any:
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:19].replace(" ", "T") if "T" in value else value[:10]
    return value


def _matches(record: dict[str, Any], condition: str, records: dict[str, list[dict[str, Any]]]) -> bool:
    match = _CONDITION_RE.match(condition)
    if not match:
        raise CliError("MALFORMED_QUERY", f"The simulator cannot evaluate this condition: {condition.strip()}", 1)
    actual = _resolve(record, match.group("field"), records)
    op = re.sub(r"\s+", " ", match.group("op").upper())
    raw = match.group("value")
    if op in ("IN", "NOT IN"):
        values = [_literal(v) for v in re.findall(r"'(?:[^'\\]|\\.)*'|[^,()\s]+", raw.strip().strip("()"))]
        found = any(str(actual)[:15] == str(v)[:15] if _is_id(actual) else actual == v for v in values)
        return found if op == "IN" else not found
    expected = _literal(raw)
    if op == "LIKE":
        pattern = re.escape(str(expected)).replace("%", ".*").replace("_", ".")
        return actual is not None and re.fullmatch(pattern, str(actual), flags=re.IGNORECASE | re.DOTALL) is not None
    if _is_id(actual) and isinstance(expected, str):
        left, right = str(actual)[:15], expected[:15]
    else:
        left, right = _norm(actual), _norm(expected)
        if isinstance(left, str) and isinstance(right, str):
            if len(left) != len(right) and re.match(r"^\d{4}-", left) and re.match(r"^\d{4}-", right):
                size = min(len(left), len(right))
                left, right = left[:size], right[:size]
            elif not re.match(r"^\d{4}-", left):
                left, right = left.lower(), right.lower()  # SOQL string comparison ignores case
    if op == "=":
        return left == right
    if op in ("!=", "<>"):
        return left != right
    if left is None or right is None:
        return False
    try:
        return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[op]
    except TypeError:
        return False


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{15}|[A-Za-z0-9]{18}", value) is not None \
        and re.match(r"^[0-9a-zA-Z]{3}", value) is not None and any(ch.isdigit() for ch in value[:3])


def _project(record: dict[str, Any], fields: list[str], object_name: str,
             records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {"attributes": {"type": object_name, "url": f"/services/data/v62.0/sobjects/{object_name}/{record.get('Id', '')}"}}
    for field in fields:
        if "." not in field:
            key = next((k for k in record if k.lower() == field.lower()), field)
            out[key] = record.get(key)
            continue
        parts = field.split(".")
        cursor = out
        source: Any = record
        for depth, part in enumerate(parts[:-1]):
            source = _resolve(source, part, records) if isinstance(source, dict) else None
            if source is None:
                cursor[part] = None
                break
            if not isinstance(cursor.get(part), dict):
                cursor[part] = {"attributes": {"type": _type_of(source, records) or part}}
            cursor = cursor[part]
        else:
            cursor[parts[-1]] = _resolve(source, parts[-1], records) if isinstance(source, dict) else None
    return out


def _type_of(record: dict[str, Any], records: dict[str, list[dict[str, Any]]]) -> str | None:
    for name, rows in records.items():
        if any(r is record or (r.get("Id") and r.get("Id") == record.get("Id")) for r in rows):
            return name
    return None


def run_query(query: str, records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    match = _QUERY_RE.match(query.strip().rstrip(";"))
    if not match:
        raise CliError("MALFORMED_QUERY", f"The simulator cannot parse this query: {query.strip()[:200]}", 1)
    object_name = next((k for k in records if k.lower() == match.group("object").lower()), None)
    if object_name is None:
        raise CliError("INVALID_TYPE", f"sObject type '{match.group('object')}' is not supported.", 1)
    rows = list(records[object_name])
    if match.group("where"):
        for condition in _split_and(match.group("where")):
            rows = [r for r in rows if _matches(r, condition, records)]
    if match.group("order"):
        for key in reversed([k.strip() for k in match.group("order").split(",")]):
            tokens = key.split()
            descending = len(tokens) > 1 and tokens[1].upper() == "DESC"
            rows.sort(key=lambda r, f=tokens[0]: (_resolve(r, f, records) is None, str(_resolve(r, f, records) or "")),
                      reverse=descending)
    select = match.group("select").strip()
    if re.fullmatch(r"COUNT\(\s*\)", select, flags=re.IGNORECASE):
        return {"records": [], "totalSize": len(rows), "done": True}
    total = len(rows)
    if match.group("limit"):
        rows = rows[: int(match.group("limit"))]
    fields = [f.strip() for f in select.split(",") if f.strip()]
    for field in fields:
        if not re.fullmatch(r"[\w.]+", field):
            raise CliError("MALFORMED_QUERY", f"The simulator does not support this select item: {field}", 1)
    projected = [_project(r, fields, object_name, records) for r in rows]
    return {"records": projected, "totalSize": min(total, len(projected)) if match.group("limit") else total, "done": True}


# --- Metadata ---------------------------------------------------------------------------


def component_files(root: Path, spec: str) -> list[tuple[str, str, Path]]:
    """Return ``(type, fullName, path)`` for every file of the components matching ``spec``."""
    type_name, _, name = spec.partition(":")
    name = name or "*"
    out: list[tuple[str, str, Path]] = []
    if type_name in TYPE_DIRS:
        folder, suffixes = TYPE_DIRS[type_name]
        base = root / folder
        if base.is_dir():
            names = sorted({p.name[: -len(suffixes[0])] for p in base.iterdir() if p.name.endswith(suffixes[0])})
            for candidate in names:
                if fnmatch.fnmatchcase(candidate, name):
                    for suffix in suffixes:
                        path = base / f"{candidate}{suffix}"
                        if path.is_file():
                            out.append((type_name, candidate, path))
    elif type_name in CHILD_TYPES:
        folder, suffix = CHILD_TYPES[type_name]
        object_pattern, _, child_pattern = name.partition(".")
        child_pattern = child_pattern or "*"
        objects = root / "objects"
        if objects.is_dir():
            for obj in sorted(p for p in objects.iterdir() if p.is_dir()):
                if not fnmatch.fnmatchcase(obj.name, object_pattern):
                    continue
                for path in sorted((obj / folder).glob(f"*{suffix}")) if (obj / folder).is_dir() else []:
                    child = path.name[: -len(suffix)]
                    if fnmatch.fnmatchcase(child, child_pattern):
                        out.append((type_name, f"{obj.name}.{child}", path))
    elif type_name == "CustomObject":
        objects = root / "objects"
        if objects.is_dir():
            for obj in sorted(p for p in objects.iterdir() if p.is_dir()):
                if fnmatch.fnmatchcase(obj.name, name):
                    for path in sorted(p for p in obj.rglob("*") if p.is_file()):
                        child_type = next((t for t, (f, s) in CHILD_TYPES.items() if path.parent.name == f), "CustomObject")
                        full = obj.name if child_type == "CustomObject" else f"{obj.name}.{path.name.split('.')[0]}"
                        out.append((child_type, full, path))
    else:
        raise CliError("RegistryError", f"Missing metadata type definition in registry for id '{type_name.lower()}'.", 1)
    return out


def project_root(cwd: Path) -> Path:
    if not (cwd / "sfdx-project.json").is_file():
        raise CliError("InvalidProjectWorkspaceError", f"{cwd} does not contain a valid Salesforce DX project.", 1)
    return cwd / "force-app" / "main" / "default"


def cmd_retrieve(flags: dict[str, Any]) -> dict[str, Any]:
    folder, _ = find_org(flags.get("--target-org"))
    local = project_root(Path.cwd())
    files, messages = [], []
    for spec in flags.get("--metadata") or []:
        matched = component_files(folder / "source", spec)
        if not matched:
            type_name, _, name = spec.partition(":")
            messages.append({"fileName": "unpackaged/package.xml",
                             "problem": f"Entity of type '{type_name}' named '{name or '*'}' cannot be found"})
        for type_name, full_name, path in matched:
            relative = path.relative_to(folder / "source")
            target = local / relative
            new_text = path.read_bytes()
            if not target.exists():
                state = "Created"
            else:
                state = "Unchanged" if target.read_bytes() == new_text else "Changed"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new_text)
            files.append({"fullName": full_name, "type": type_name, "state": state, "filePath": str(target)})
    return {
        "status": 0,
        "result": {"done": True, "status": "Succeeded", "success": True, "id": "09S" + uuid.uuid4().hex[:15],
                   "files": files, "messages": messages, "fileProperties": []},
        "warnings": [m["problem"] for m in messages],
    }


def _braces_balanced(text: str) -> int | None:
    """Return the line where braces first go wrong, or ``None`` when they balance."""
    cleaned = re.sub(r"//[^\n]*|/\*.*?\*/|'(?:[^'\\\n]|\\.)*'", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text,
                     flags=re.DOTALL)
    depth = 0
    for number, line in enumerate(cleaned.splitlines(), start=1):
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return number
    return None if depth == 0 else len(cleaned.splitlines())


def evaluate_tests(folder: Path, source_root: Path, class_names: list[str]) -> list[dict[str, Any]]:
    rules_path = folder / "tests.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8")) if rules_path.is_file() else {}
    results = []
    for class_name in class_names:
        path = source_root / "classes" / f"{class_name}.cls"
        if not path.is_file():
            raise CliError("InvalidTestClass", f"No test class named {class_name} was found in the org.", 1)
        text = path.read_text(encoding="utf-8")
        methods = re.findall(r"@isTest\s+(?:static\s+)?(?:void\s+)?(?:static\s+void\s+)?(\w+)\s*\(", text, flags=re.I)
        methods = [m for m in methods if m.lower() not in ("static", "void")] or \
            re.findall(r"static\s+testMethod\s+void\s+(\w+)\s*\(", text, flags=re.I)
        for method in methods:
            rule = (rules.get(class_name) or {}).get(method) or {}
            outcome, message, stack = "Pass", None, None
            condition = rule.get("fails_while")
            if condition:
                target = source_root / condition["file"]
                body = target.read_text(encoding="utf-8") if target.is_file() else ""
                if re.search(condition["regex"], body, flags=re.DOTALL):
                    outcome, message, stack = "Fail", rule.get("message"), rule.get("stack_trace")
            results.append({"class": class_name, "method": method, "outcome": outcome, "message": message, "stack": stack})
    return results


def cmd_deploy(flags: dict[str, Any]) -> dict[str, Any]:
    folder, _ = find_org(flags.get("--target-org"))
    local = project_root(Path.cwd())
    specs = flags.get("--metadata") or []
    files, failures, staged = [], [], []
    for spec in specs:
        matched = component_files(local, spec)
        if not matched:
            raise CliError("NoSourceError", f"No source-backed components present in the package for {spec}.", 1)
        for type_name, full_name, path in matched:
            relative = path.relative_to(local)
            target = folder / "source" / relative
            text = path.read_bytes()
            state = "Created" if not target.exists() else ("Unchanged" if target.read_bytes() == text else "Changed")
            if path.suffix in (".cls", ".trigger"):
                bad = _braces_balanced(path.read_text(encoding="utf-8"))
                if bad is not None:
                    failures.append({"componentType": type_name, "fullName": full_name, "fileName": str(relative),
                                     "lineNumber": bad, "columnNumber": 1, "problemType": "Error", "success": False,
                                     "problem": "Unexpected token '}'." if bad else "Missing '}'."})
                    state = "Failed"
            files.append({"fullName": full_name, "type": type_name, "state": state, "filePath": str(path)})
            staged.append((path, target))

    deploy_id = "0Af" + uuid.uuid4().hex[:15]
    test_failures: list[dict[str, Any]] = []
    tests_run = 0
    if not failures and flags.get("--test-level") == "RunSpecifiedTests":
        # Evaluate the tests against what the org would contain after this deploy.
        preview = state_dir() / "tmp" / deploy_id
        shutil.copytree(folder / "source", preview)
        for path, target in staged:
            dest = preview / target.relative_to(folder / "source")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        try:
            outcomes = evaluate_tests(folder, preview, flags.get("--tests") or [])
        finally:
            shutil.rmtree(preview, ignore_errors=True)
        tests_run = len(outcomes)
        test_failures = [
            {"name": o["class"], "methodName": o["method"], "message": o["message"], "stackTrace": o["stack"]}
            for o in outcomes if o["outcome"] != "Pass"
        ]

    success = not failures and not test_failures
    if success and not flags.get("--dry-run"):
        for path, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    result = {
        "id": deploy_id,
        "status": "Succeeded" if success else "Failed",
        "success": success,
        "done": True,
        "checkOnly": bool(flags.get("--dry-run")),
        "numberComponentErrors": len(failures),
        "numberComponentsDeployed": 0 if failures else len({(f["type"], f["fullName"]) for f in files}),
        "numberTestErrors": len(test_failures),
        "numberTestsCompleted": tests_run,
        "files": files,
        "details": {
            "componentFailures": failures,
            "runTestResult": {"numTestsRun": tests_run, "numFailures": len(test_failures), "failures": test_failures},
        },
    }
    return {"status": 0 if success else 1, "result": result, "warnings": []}


def cmd_delete_source(flags: dict[str, Any]) -> dict[str, Any]:
    folder, _ = find_org(flags.get("--target-org"))
    local = project_root(Path.cwd())
    files = []
    for spec in flags.get("--metadata") or []:
        for type_name, full_name, path in component_files(folder / "source", spec):
            relative = path.relative_to(folder / "source")
            path.unlink()
            if (local / relative).exists():
                (local / relative).unlink()
            files.append({"fullName": full_name, "type": type_name, "state": "Deleted", "filePath": str(local / relative)})
    return {"status": 0, "result": {"status": "Succeeded", "success": True, "done": True, "deletedSource": files,
                                     "deletes": files}, "warnings": []}


# --- Commands ---------------------------------------------------------------------------


def cmd_org_list() -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"other": [], "sandboxes": [], "nonScratchOrgs": [], "devHubs": [], "scratchOrgs": []}
    for _, org in all_orgs():
        entry = {
            "alias": org.get("alias"), "username": org.get("username"), "orgId": org.get("orgId"),
            "instanceUrl": org.get("instanceUrl"), "accessToken": "00D-SIMULATED-TOKEN-THAT-MUST-NEVER-BE-SHOWN",
            "loginUrl": org.get("instanceUrl"), "clientId": "PlatformCLI", "isDevHub": False,
            "instanceApiVersion": org.get("apiVersion"), "connectedStatus": "Connected",
            "isDefaultUsername": False, "isDefaultDevHubUsername": False, "isSandbox": bool(org.get("isSandbox")),
            "isScratch": bool(org.get("isScratch")),
        }
        if org.get("isScratch"):
            groups["scratchOrgs"].append(entry)
        else:
            groups["nonScratchOrgs"].append(entry)
            if org.get("isSandbox"):
                groups["sandboxes"].append(entry)
            else:
                groups["other"].append(entry)
    return {"status": 0, "result": groups, "warnings": []}


def cmd_org_display(flags: dict[str, Any]) -> dict[str, Any]:
    _, org = find_org(flags.get("--target-org"))
    result = {
        "id": org.get("orgId"), "apiVersion": org.get("apiVersion"),
        "accessToken": "00D-SIMULATED-TOKEN-THAT-MUST-NEVER-BE-SHOWN",
        "instanceUrl": org.get("instanceUrl"), "username": org.get("username"), "clientId": "PlatformCLI",
        "connectedStatus": "Connected", "alias": org.get("alias"),
    }
    if org.get("isScratch"):
        result.update({"devHubId": "hub@example.com", "expirationDate": "2099-01-01", "status": "Active"})
    return {"status": 0, "result": result, "warnings": []}


def org_records(folder: Path, org: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    records = load_records(folder)
    if org.get("organizationRecordMissing"):
        records["Organization"] = []
    else:
        records["Organization"] = [{
            "Id": org.get("orgId"), "Name": org.get("name"), "IsSandbox": org.get("isSandbox"),
            "OrganizationType": org.get("organizationType"), "InstanceName": org.get("instanceName", "SIM1"),
            "TrialExpirationDate": None,
        }]
    return records


def cmd_data_query(flags: dict[str, Any]) -> dict[str, Any]:
    folder, org = find_org(flags.get("--target-org"))
    if flags.get("--file"):
        query = Path(flags["--file"]).read_text(encoding="utf-8")
    elif flags.get("--query"):
        query = flags["--query"]
    else:
        raise CliError("Error", "Exactly one of the following must be provided: --query, --file", 2)
    return {"status": 0, "result": run_query(query, org_records(folder, org)), "warnings": []}


def cmd_describe(flags: dict[str, Any]) -> dict[str, Any]:
    folder, _ = find_org(flags.get("--target-org"))
    name = flags.get("--sobject", "")
    path = folder / "describe" / f"{name}.json"
    if not path.is_file():
        raise CliError("NOT_FOUND", f"The requested resource does not exist: sObject type '{name}'", 1)
    return {"status": 0, "result": json.loads(path.read_text(encoding="utf-8")), "warnings": []}


def cmd_run_tests(flags: dict[str, Any]) -> dict[str, Any]:
    folder, org = find_org(flags.get("--target-org"))
    class_names = flags.get("--class-names") or []
    outcomes = evaluate_tests(folder, folder / "source", class_names)
    failing = [o for o in outcomes if o["outcome"] != "Pass"]
    tests = [
        {
            "Id": "07M" + uuid.uuid4().hex[:15], "QueueItemId": "709" + uuid.uuid4().hex[:15],
            "StackTrace": o["stack"], "Message": o["message"], "AsyncApexJobId": "707SIM000000001AAA",
            "MethodName": o["method"], "Outcome": o["outcome"],
            "ApexClass": {"Id": "01pSIM" + o["class"][:9].ljust(9, "0"), "Name": o["class"], "NamespacePrefix": None},
            "RunTime": 42, "FullName": f"{o['class']}.{o['method']}",
        }
        for o in outcomes
    ]
    result: dict[str, Any] = {
        "summary": {
            "outcome": "Failed" if failing else "Passed", "testsRan": len(outcomes),
            "passing": len(outcomes) - len(failing), "failing": len(failing), "skipped": 0,
            "passRate": f"{round(100 * (len(outcomes) - len(failing)) / max(1, len(outcomes)))}%",
            "failRate": f"{round(100 * len(failing) / max(1, len(outcomes)))}%",
            "testRunId": "707SIM000000001AAA", "testExecutionTime": f"{42 * len(outcomes)} ms",
            "orgId": org.get("orgId"), "username": org.get("username"),
        },
        "tests": tests,
    }
    if flags.get("--code-coverage"):
        covered = []
        classes_dir = folder / "source" / "classes"
        test_text = " ".join((classes_dir / f"{c}.cls").read_text(encoding="utf-8") for c in class_names)
        for path in sorted(classes_dir.glob("*.cls")):
            name = path.stem
            if name in class_names or "@isTest" in path.read_text(encoding="utf-8"):
                continue
            if re.search(rf"\b{re.escape(name)}\b", test_text) or name.replace("Service", "") in test_text:
                total = len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip().endswith((";", "{"))])
                covered.append({"id": "01pSIM" + name[:9].ljust(9, "0"), "name": name, "totalLines": total,
                                "lines": {}, "totalCovered": total, "coveredPercent": 100})
        result["coverage"] = {"coverage": covered, "records": [], "summary": {}}
        result["summary"]["testRunCoverage"] = "100%" if covered else "0%"
    return {"status": 100 if failing else 0, "result": result, "warnings": []}


def cmd_get_log(flags: dict[str, Any]) -> dict[str, Any]:
    folder, _ = find_org(flags.get("--target-org"))
    log_id = flags.get("--log-id", "")
    source = folder / "logs" / f"{log_id}.log"
    if not source.is_file():
        return {"status": 0, "result": [], "warnings": []}
    if flags.get("--output-dir"):
        target_dir = Path(flags["--output-dir"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{log_id}.log"
        shutil.copyfile(source, target)
        return {"status": 0, "result": [str(target)], "warnings": []}
    return {"status": 0, "result": [{"log": source.read_text(encoding="utf-8")}], "warnings": []}


def cmd_apex_run(flags: dict[str, Any]) -> dict[str, Any]:
    find_org(flags.get("--target-org"))
    path = Path(flags.get("--file", ""))
    if not path.is_file():
        raise CliError("Error", f"No file found at {path}", 2)
    code = path.read_text(encoding="utf-8")
    log = "62.0 APEX_CODE,DEBUG;APEX_PROFILING,INFO\nExecute Anonymous: " + code.splitlines()[0][:80] + "\n"
    bad = _braces_balanced(code)
    if bad is not None or "SIMULATE_COMPILE_ERROR" in code:
        result = {"success": False, "compiled": False, "compileProblem": "Unexpected token '}'.",
                  "exceptionMessage": "", "exceptionStackTrace": "", "line": bad or 1, "column": 1, "logs": log}
        raise CliError("executeCompileFailure", "Compilation failed: Unexpected token '}'.", 1, result)
    for number, line in enumerate(code.splitlines(), start=1):
        debug = re.search(r"System\.debug\(\s*'([^']*)'\s*\)", line)
        if debug:
            log += f"00:00:00.1 (1000000)|USER_DEBUG|[{number}]|DEBUG|{debug.group(1)}\n"
    return {"status": 0, "result": {"success": True, "compiled": True, "compileProblem": "", "exceptionMessage": "",
                                     "exceptionStackTrace": "", "line": -1, "column": -1, "logs": log}, "warnings": []}


def cmd_rest(positional: list[str], flags: dict[str, Any]) -> dict[str, Any]:
    folder, _ = find_org(flags.get("--target-org"))
    url = positional[3] if len(positional) > 3 else ""
    method = flags.get("--method", "GET")
    match = re.match(r"^/?services/data/v[\d.]+/(tooling/)?sobjects/(\w+)(?:/(\w+))?$", url)
    if not match:
        return {"status": 0, "result": {"statusCode": 404, "headers": {}, "body": [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]}, "warnings": [], "_exit": 1}
    object_name, record_id = match.group(2), match.group(3)
    body: dict[str, Any] = {}
    raw = flags.get("--body")
    if raw:
        text = Path(raw[1:]).read_text(encoding="utf-8") if raw.startswith("@") else raw
        body = json.loads(text) if text.strip() else {}
    records = load_records(folder)
    rows = records.setdefault(object_name, [])
    if method == "POST" and not record_id:
        prefix = {"TraceFlag": "7tf", "DebugLevel": "7dl"}.get(object_name, "a00")
        new_id = prefix + uuid.uuid4().hex[:15].upper()
        rows.append({"Id": new_id, **body})
        save_records(folder, records)
        return {"status": 0, "result": {"statusCode": 201, "headers": {}, "body": {"id": new_id, "success": True, "errors": []}}, "warnings": []}
    if method == "PATCH" and record_id:
        row = next((r for r in rows if str(r.get("Id"))[:15] == record_id[:15]), None)
        if row is None:
            return {"status": 0, "result": {"statusCode": 404, "headers": {}, "body": [{"errorCode": "NOT_FOUND", "message": "Provided external ID field does not exist or is not accessible: " + record_id}]}, "warnings": [], "_exit": 1}
        for key in body:
            if key not in row and not key.endswith("__c") and object_name not in ("TraceFlag", "DebugLevel"):
                return {"status": 0, "result": {"statusCode": 400, "headers": {}, "body": [{"errorCode": "INVALID_FIELD", "message": f"No such column '{key}' on sobject of type {object_name}"}]}, "warnings": [], "_exit": 1}
        row.update(body)
        save_records(folder, records)
        return {"status": 0, "result": {"statusCode": 204, "headers": {}, "body": ""}, "warnings": []}
    return {"status": 0, "result": {"statusCode": 405, "headers": {}, "body": [{"errorCode": "METHOD_NOT_ALLOWED", "message": f"HTTP Method '{method}' not allowed"}]}, "warnings": [], "_exit": 1}


def dispatch(argv: list[str]) -> dict[str, Any]:
    positional, flags = parse_args(argv)
    command = tuple(positional[:3])
    if command[:2] == ("org", "list"):
        return cmd_org_list()
    if command[:2] == ("org", "display"):
        return cmd_org_display(flags)
    if command[:2] == ("data", "query"):
        return cmd_data_query(flags)
    if command[:2] == ("sobject", "describe"):
        return cmd_describe(flags)
    if command == ("project", "retrieve", "start"):
        return cmd_retrieve(flags)
    if command == ("project", "deploy", "start"):
        return cmd_deploy(flags)
    if command == ("project", "delete", "source"):
        return cmd_delete_source(flags)
    if command == ("apex", "run", "test"):
        return cmd_run_tests(flags)
    if command == ("apex", "get", "log"):
        return cmd_get_log(flags)
    if command[:2] == ("apex", "run"):
        return cmd_apex_run(flags)
    if command == ("api", "request", "rest"):
        return cmd_rest(positional, flags)
    raise CliError("Error", f"command {' '.join(positional[:3])} not found (not implemented by the simulator)", 127)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["--version"], ["version"]):
        print(VERSION)
        return 0
    delay = os.environ.get("SFTI_SIM_DELAY")
    if delay:
        import time

        time.sleep(float(delay))
    try:
        envelope = dispatch(argv)
    except CliError as exc:
        envelope = {"name": exc.name, "message": exc.message, "exitCode": exc.exit_code, "status": exc.exit_code,
                    "warnings": [], "code": exc.name}
        if exc.data is not None:
            envelope["data"] = exc.data
        print(json.dumps(envelope, indent=2))
        return exc.exit_code
    exit_code = envelope.pop("_exit", envelope.get("status", 0))
    print(json.dumps(envelope, indent=2))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
