"""The five report files. Aggregation is SQL over the `ledger` view; Python only formats and writes.

A call exists only when the ledger holds a linked call.initiated for it. Events of any other
call (and dispositions of such calls) are ignored.
"""

import csv
import json
import sqlite3
from pathlib import Path

from .events import event_quality

# The fact tables are built once per run as TEMP tables, so no report query re-evaluates the
# ledger view. call_facts: one row per call (a linked call.initiated exists), with its flags and
# effective disposition. lead_stats: one row per lead with its bucket and call stats.
_CALL_FACTS = """
CREATE TEMP TABLE call_facts AS
WITH call_ev AS (
    SELECT lead_id, call_id,
           MIN(CASE WHEN event_type = 'call.initiated' THEN occurred_at END) AS initiated_at,
           MAX(event_type = 'call.connected') AS connected,
           MAX(event_type = 'call.telephony_failed') AS failed
    FROM ledger
    WHERE call_id IS NOT NULL
    GROUP BY lead_id, call_id
),
eff AS (  -- effective disposition: the latest override, else the latest set (ties: event_id)
    SELECT lead_id, call_id, disposition_code AS code FROM (
        SELECT lead_id, call_id, disposition_code,
               ROW_NUMBER() OVER (
                   PARTITION BY lead_id, call_id
                   ORDER BY (event_type = 'disposition.override') DESC, occurred_at DESC, event_id DESC
               ) AS rn
        FROM ledger
        WHERE event_type LIKE 'disposition.%' AND call_id IS NOT NULL
    ) WHERE rn = 1
)
SELECT ev.lead_id, l.workflow_id, ev.call_id, ev.initiated_at,
       substr(ev.initiated_at, 1, 10) AS call_date,
       ev.connected, ev.failed, e.code, d.is_terminal, d.is_success
FROM call_ev ev
JOIN leads l ON l.id = ev.lead_id
LEFT JOIN eff e ON e.lead_id = ev.lead_id AND e.call_id = ev.call_id
LEFT JOIN dispositions d ON d.workflow_id = l.workflow_id AND d.code = e.code
WHERE ev.initiated_at IS NOT NULL
"""

# Per-lead pieces are keyed tables so the final join to `leads` is a primary key lookup
# (joining the call_facts CTE directly made SQLite scan it once per lead).
_LEAD_CALLS = """
CREATE TEMP TABLE lead_calls (lead_id INTEGER PRIMARY KEY, total_calls, connected_calls,
                              last_call_at, any_terminal, all_failed, ever_success);
INSERT INTO lead_calls
SELECT lead_id, COUNT(*), SUM(connected), MAX(initiated_at), MAX(COALESCE(is_terminal, 0)),
       MIN(failed), MAX(COALESCE(is_success, 0))
FROM call_facts GROUP BY lead_id;
CREATE TEMP TABLE latest_disp (lead_id INTEGER PRIMARY KEY, code);  -- most recent call with a disposition
INSERT INTO latest_disp
SELECT lead_id, code FROM (
    SELECT lead_id, code,
           ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY initiated_at DESC, call_id DESC) AS rn
    FROM call_facts WHERE code IS NOT NULL
) WHERE rn = 1;
CREATE TEMP TABLE lead_stats AS
SELECT l.id, l.workflow_id, l.external_ref, l.batch_id, l.phone_last4,
       CASE WHEN lc.lead_id IS NULL THEN 'yet_to_reach_out'
            WHEN lc.any_terminal = 1 THEN 'completed'
            WHEN lc.all_failed = 1 THEN 'in_progress_telephony_failed'
            ELSE 'in_progress_dialed' END AS bucket,
       COALESCE(lc.total_calls, 0) AS total_calls,
       COALESCE(lc.connected_calls, 0) AS connected_calls,
       lc.last_call_at,
       ld.code AS current_disposition,
       COALESCE(lc.ever_success, 0) AS ever_success
FROM leads l
LEFT JOIN lead_calls lc ON lc.lead_id = l.id
LEFT JOIN latest_disp ld ON ld.lead_id = l.id;
"""

_BATCH_SUMMARY = """
SELECT workflow_id, batch_id, COUNT(*),
       SUM(bucket = 'yet_to_reach_out'), SUM(bucket = 'in_progress_telephony_failed'),
       SUM(bucket = 'in_progress_dialed'), SUM(bucket = 'completed')
FROM lead_stats GROUP BY workflow_id, batch_id ORDER BY workflow_id, batch_id
"""

_DAILY = """
SELECT workflow_id, call_date, COUNT(*), SUM(connected),
       COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_id END)
FROM call_facts GROUP BY workflow_id, call_date ORDER BY workflow_id, call_date
"""

_LEADS = """
SELECT workflow_id, external_ref, batch_id, phone_last4, bucket, total_calls, connected_calls,
       last_call_at, current_disposition, ever_success
FROM lead_stats ORDER BY workflow_id, external_ref, id
"""

_REJECTIONS = (
    "SELECT workflow_id, batch_id, row_number, reason FROM lead_rejections"
    " ORDER BY workflow_id, batch_id, row_number"
)


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _data_quality(conn: sqlite3.Connection) -> dict:
    total, rejected, merged, warnings = conn.execute(
        "SELECT COALESCE(SUM(rows_total), 0), COALESCE(SUM(rows_rejected), 0),"
        " COALESCE(SUM(rows_merged), 0), COALESCE(SUM(field_warnings), 0)"
        " FROM ingested_files WHERE kind = 'leads'"
    ).fetchone()
    reasons = dict(conn.execute(
        "SELECT reason, COUNT(*) FROM lead_rejections GROUP BY reason ORDER BY reason"
    ))
    return {
        "lead_rows_total": total,
        "lead_rows_rejected": rejected,
        "rejections_by_reason": reasons,
        "duplicate_lead_rows_merged": merged,
        "field_warnings": warnings,
        **event_quality(conn),
    }


def write_reports(conn: sqlite3.Connection, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for table in ("call_facts", "lead_calls", "latest_disp", "lead_stats"):
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")
    conn.execute(_CALL_FACTS)
    conn.execute("CREATE INDEX temp.call_facts_lead ON call_facts (lead_id)")
    conn.executescript(_LEAD_CALLS)

    _write_csv(
        out_dir / "batch_summary.csv",
        ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
         "in_progress_telephony_failed", "in_progress_dialed", "completed"],
        conn.execute(_BATCH_SUMMARY),
    )
    _write_csv(
        out_dir / "daily_overview.csv",
        ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
        ((wf, day, total, conn_calls, f"{conn_calls / total:.4f}", success)
         for wf, day, total, conn_calls, success in conn.execute(_DAILY)),
    )
    _write_csv(
        out_dir / "lead_report.csv",
        ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
         "connected_calls", "last_call_at", "current_disposition", "ever_success"],
        ((wf, ref, batch, "+91******" + last4, bucket, total, connected, last_at or "", disp or "",
          "true" if success else "false")
         for wf, ref, batch, last4, bucket, total, connected, last_at, disp, success
         in conn.execute(_LEADS)),
    )
    _write_csv(
        out_dir / "rejections.csv",
        ["workflow_id", "batch_id", "row_number", "reason"],
        conn.execute(_REJECTIONS),
    )
    with open(out_dir / "data_quality.json", "w", encoding="utf-8", newline="") as fh:
        json.dump(_data_quality(conn), fh, indent=2)
        fh.write("\n")
