"""Event ingestion: append-only storage with dedupe and malformed line handling.

Events are stored as they arrive. Linking to leads, orphans and unknown dispositions
are decided at query time by the views in db.py, never here.
"""

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .leads import SCHEMAS_DIR
from .parse import parse_phone, parse_timestamp
from .pii import Pii


def load_dispositions(conn: sqlite3.Connection, csv_path: Path = SCHEMAS_DIR / "dispositions.csv"):
    """Replace the catalogue with the current dispositions.csv. Caller commits."""
    conn.execute("DELETE FROM dispositions")
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            conn.execute(
                "INSERT INTO dispositions (workflow_id, code, is_terminal, is_success)"
                " VALUES (?, ?, ?, ?)",
                (r["workflow_id"], r["code"], r["is_terminal"] == "true", r["is_success"] == "true"),
            )


def _str_or_none(v) -> str | None:
    return v if isinstance(v, str) else None


def _parse_event(line: str, pii: Pii) -> tuple | None:
    """Return the event's column values (without file_id), or None when malformed."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if not isinstance(ev, dict):
        return None
    event_id, event_type = ev.get("event_id"), ev.get("event_type")
    if not (isinstance(event_id, str) and event_id) or not isinstance(event_type, str):
        return None
    raw_ts = ev.get("occurred_at")
    ts = parse_timestamp(raw_ts) if isinstance(raw_ts, str) else None
    if ts is None:
        return None
    payload = ev.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    raw_phone = ev.get("phone")
    phone = parse_phone(raw_phone) if isinstance(raw_phone, str) else None
    code = _str_or_none(payload.get("disposition_code")) if event_type.startswith("disposition.") else None
    return (
        event_id, event_type, _str_or_none(ev.get("workflow_id")), _str_or_none(ev.get("call_id")),
        ts.isoformat(), pii.phone_hash(phone) if phone else None, code, json.dumps(payload),
    )


def _ingest_file(conn, pii: Pii, path: Path, file_id: int) -> tuple[int, int, int]:
    total = malformed = duplicate = 0
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            total += 1
            row = _parse_event(line, pii)
            if row is None:
                malformed += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id,"
                " occurred_at, phone_hash, disposition_code, payload, file_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*row, file_id),
            )
            duplicate += cur.rowcount == 0
    return total, malformed, duplicate


def event_quality(conn: sqlite3.Connection) -> dict:
    """Event counts for data_quality.json, reflecting everything ingested so far."""
    totals = conn.execute(
        "SELECT COALESCE(SUM(events_total), 0), COALESCE(SUM(events_malformed), 0),"
        " COALESCE(SUM(events_duplicate), 0) FROM ingested_files"
    ).fetchone()
    orphans = conn.execute("SELECT COUNT(*) FROM event_links WHERE lead_id IS NULL").fetchone()[0]
    # orphans are counted only as orphans, so unknown codes are counted among linked events
    unknown = conn.execute(
        "SELECT COUNT(*) FROM events e JOIN event_links k ON k.event_id = e.event_id"
        " WHERE k.lead_id IS NOT NULL AND e.event_type LIKE 'disposition.%'"
        " AND NOT EXISTS (SELECT 1 FROM dispositions d"
        "                 WHERE d.workflow_id = e.workflow_id AND d.code = e.disposition_code)"
    ).fetchone()[0]
    return {
        "events_total": totals[0],
        "malformed_events": totals[1],
        "duplicate_events_dropped": totals[2],
        "orphan_events": orphans,
        "unknown_disposition_events": unknown,
    }


def ingest_events(conn: sqlite3.Connection, pii: Pii, drop: Path):
    """Ingest every event file of one drop. Caller owns the transaction. Yields summary lines."""
    events_dir = Path(drop) / "events"
    if not events_dir.is_dir():
        return
    for path in sorted(events_dir.glob("*.jsonl")):
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if conn.execute("SELECT 1 FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone():
            yield f"{path.name}: skipped (already ingested)"
            continue
        # the file row comes first because events reference it; counters are filled in after
        file_id = conn.execute(
            "INSERT INTO ingested_files (sha256, kind, path, ingested_at) VALUES (?, 'events', ?, ?)",
            (sha, str(path), datetime.now(timezone.utc).isoformat()),
        ).lastrowid
        total, malformed, duplicate = _ingest_file(conn, pii, path, file_id)
        conn.execute(
            "UPDATE ingested_files SET events_total = ?, events_malformed = ?, events_duplicate = ?"
            " WHERE id = ?",
            (total, malformed, duplicate, file_id),
        )
        yield (f"{path.name}: lines={total} malformed={malformed} duplicates={duplicate}"
               f" stored={total - malformed - duplicate}")
