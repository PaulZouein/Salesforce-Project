"""SQLite audit log.

Two tables:

* ``audit_log``: one row per guarded attempt (allowed, blocked, succeeded or failed).
* ``changes``: one row per applied change (a deploy or a record update), holding what is
  needed to roll it back.

The database is local and is not committed to git. ``changes.detail`` can contain the
previous values of updated records, which is why it never leaves the machine.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL,
    org_target TEXT,
    org_id TEXT,
    classification TEXT,
    ticket_key TEXT,
    approved_by TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticket_key TEXT NOT NULL,
    org_target TEXT NOT NULL,
    org_id TEXT NOT NULL,
    status TEXT NOT NULL,
    pre_commit TEXT,
    proposed_commit TEXT,
    post_commit TEXT,
    deploy_id TEXT,
    reason TEXT,
    approved_by TEXT,
    detail TEXT
);
"""

OUTCOME_ALLOWED = "allowed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_IN_PROGRESS = "in_progress"  # a deploy still running in the org when the wait ran out


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_change_id() -> str:
    return "CHG-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # -- audit rows ----------------------------------------------------------------------

    def record(
        self,
        operation: str,
        outcome: str,
        *,
        org_target: str | None = None,
        org_id: str | None = None,
        classification: str | None = None,
        ticket_key: str | None = None,
        approved_by: str | None = None,
        detail: Any = None,
    ) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO audit_log (ts, operation, outcome, org_target, org_id, classification, "
                "ticket_key, approved_by, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utc_now(),
                    operation,
                    outcome,
                    org_target,
                    org_id,
                    classification,
                    ticket_key,
                    approved_by,
                    json.dumps(detail, default=str) if detail is not None else None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def entries(self, *, limit: int = 50, ticket_key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if ticket_key:
            sql += " WHERE ticket_key = ?"
            params.append(ticket_key)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row(r) for r in rows]

    # -- change rows ---------------------------------------------------------------------

    def save_change(
        self,
        *,
        change_id: str,
        kind: str,
        ticket_key: str,
        org_target: str,
        org_id: str,
        status: str,
        pre_commit: str | None = None,
        proposed_commit: str | None = None,
        post_commit: str | None = None,
        deploy_id: str | None = None,
        reason: str | None = None,
        approved_by: str | None = None,
        detail: Any = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO changes (change_id, ts, kind, ticket_key, org_target, org_id, status, "
                "pre_commit, proposed_commit, post_commit, deploy_id, reason, approved_by, detail) "
                "VALUES (?, COALESCE((SELECT ts FROM changes WHERE change_id = ?), ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    change_id,
                    change_id,
                    utc_now(),
                    kind,
                    ticket_key,
                    org_target,
                    org_id,
                    status,
                    pre_commit,
                    proposed_commit,
                    post_commit,
                    deploy_id,
                    reason,
                    approved_by,
                    json.dumps(detail, default=str) if detail is not None else None,
                ),
            )
            conn.commit()

    def set_change_status(self, change_id: str, status: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE changes SET status = ? WHERE change_id = ?", (status, change_id))
            conn.commit()

    def get_change(self, change_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM changes WHERE change_id = ?", (change_id,)).fetchone()
        return _row(row) if row else None

    def list_changes(self, *, ticket_key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM changes"
        params: list[Any] = []
        if ticket_key:
            sql += " WHERE ticket_key = ?"
            params.append(ticket_key)
        # rowid breaks ties between changes recorded within the same second (newest first).
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row(r) for r in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    if out.get("detail"):
        try:
            out["detail"] = json.loads(out["detail"])
        except ValueError:
            pass
    return out
