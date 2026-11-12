"""SQLite schema."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_files (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,  -- idempotency key: file content hash
    kind TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id TEXT,
    path TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    rows_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    events_total INTEGER NOT NULL DEFAULT 0,      -- event files: non-empty lines read
    events_malformed INTEGER NOT NULL DEFAULT 0,
    events_duplicate INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL,   -- keyed HMAC of the canonical phone, used for matching
    phone_last4 TEXT NOT NULL,  -- clear on purpose: reports need it, 4 digits alone identify nobody
    external_ref TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    attributes TEXT NOT NULL,   -- JSON of non-PII schema fields (normalised, nulls included)
    pii TEXT NOT NULL,          -- JSON of PII field name -> Fernet token or null
    UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS lead_rejections (
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,       -- never contains PII
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- Disposition catalogue, reloaded from dispositions.csv on every ingest.
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code TEXT NOT NULL,
    is_terminal INTEGER NOT NULL,
    is_success INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, code)
);

-- The append-only interaction ledger. Events are stored as they arrive; linking them
-- to leads is not stored, it is computed by the event_links view at query time.
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES ingested_files(id),
    event_type TEXT NOT NULL,
    workflow_id TEXT,
    call_id TEXT,
    occurred_at TEXT NOT NULL,  -- IST ISO-8601 ("+05:30"), so text order is time order
    phone_hash TEXT,            -- keyed HMAC of the canonical phone; the raw phone is never stored
    disposition_code TEXT,      -- copied from the payload of disposition.* events
    payload TEXT NOT NULL       -- JSON
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;

CREATE INDEX IF NOT EXISTS events_by_phone ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS events_by_call ON events (workflow_id, call_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
