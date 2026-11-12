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

-- Which lead each event belongs to; lead_id NULL means orphan. Computed at query time, so
-- an event whose lead arrives in a later drop stops being an orphan with no rewrite.
CREATE VIEW IF NOT EXISTS event_links AS
WITH by_phone AS (  -- every event except overrides links by (workflow, phone)
    SELECT e.event_id, e.workflow_id, e.call_id, l.id AS lead_id
    FROM events e
    LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
    WHERE e.event_type != 'disposition.override'
),
call_lead AS (  -- a call's lead, from its phone-linked events (MIN if they ever disagree)
    SELECT workflow_id, call_id, MIN(lead_id) AS lead_id
    FROM by_phone
    WHERE lead_id IS NOT NULL
    GROUP BY workflow_id, call_id
)
SELECT event_id, lead_id FROM by_phone
UNION ALL  -- overrides carry no phone: link through their call
SELECT e.event_id, c.lead_id
FROM events e
LEFT JOIN call_lead c ON c.workflow_id = e.workflow_id AND c.call_id = e.call_id
WHERE e.event_type = 'disposition.override';

-- The linked, usable ledger: orphans and unknown disposition codes are left out.
-- Every report is built on this view.
CREATE VIEW IF NOT EXISTS ledger AS
SELECT e.event_id, k.lead_id, e.event_type, e.call_id, e.occurred_at, e.disposition_code
FROM events e
JOIN event_links k ON k.event_id = e.event_id
WHERE k.lead_id IS NOT NULL
  AND (e.event_type NOT LIKE 'disposition.%'
       OR EXISTS (SELECT 1 FROM dispositions d
                  WHERE d.workflow_id = e.workflow_id AND d.code = e.disposition_code));
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
