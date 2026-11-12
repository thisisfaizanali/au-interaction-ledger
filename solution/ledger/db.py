"""SQLite schema. Event tables arrive in a later milestone."""

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
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
