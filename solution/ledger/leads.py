"""Lead ingestion: header mapping, validation, rejection and merging of duplicates."""

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .parse import normalize_header, parse_date, parse_decimal, parse_enum, parse_phone
from .pii import Pii

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"

_PARSERS = {"date": parse_date, "decimal": parse_decimal, "string": lambda raw: raw}


def _parse(field: dict, raw: str) -> str | None:
    if field["type"] == "phone":
        return parse_phone(raw)
    if field["type"] == "enum":
        return parse_enum(raw, field["values"])
    return _PARSERS[field["type"]](raw)


def _map_headers(header: list[str], fields: list[dict]) -> dict[str, int]:
    """field name -> column index. Unmatched columns are ignored; first column wins on a clash."""
    names = {}
    for f in fields:
        for n in [f["name"], *f.get("aliases", [])]:
            names.setdefault(normalize_header(n), f["name"])
    mapping: dict[str, int] = {}
    for i, cell in enumerate(header):
        name = names.get(normalize_header(cell))
        if name is not None:
            mapping.setdefault(name, i)
    return mapping


def _validate_row(row: list[str], fields: list[dict], cols: dict[str, int]):
    """Return (values, None, warnings) for a valid row or (None, reason, 0) for a rejected one."""
    values: dict[str, str | None] = {}
    warnings = 0
    for f in fields:
        i = cols.get(f["name"])
        raw = row[i].strip() if i is not None and i < len(row) else ""
        value = _parse(f, raw) if raw else None
        if value is None:
            if raw and f["type"] == "phone":
                return None, "invalid_phone", 0
            if f.get("required"):
                # also covers required non-phone values that are present but invalid
                return None, f"missing_required:{f['name']}", 0
            if raw:
                warnings += 1
        values[f["name"]] = value
    return values, None, warnings


def _is_blank(row: list[str]) -> bool:
    return all(not c.strip() for c in row)


def _merge(conn, pii: Pii, schema: dict, batch_id: str, values: dict) -> bool:
    """Insert a new lead or merge into the existing one. Returns True when merged."""
    wf = schema["workflow_id"]
    fields = schema["fields"]
    phone = values["phone"]
    phone_hash = pii.phone_hash(phone)
    row = conn.execute(
        "SELECT id, attributes, pii FROM leads WHERE workflow_id = ? AND phone_hash = ?",
        (wf, phone_hash),
    ).fetchone()
    attrs: dict = {}
    secrets: dict = {}
    for f in fields:
        (secrets if f.get("pii") else attrs)[f["name"]] = None
    if row:
        attrs.update(json.loads(row[1]))
        secrets.update(json.loads(row[2]))
    for f in fields:
        v = values[f["name"]]
        if v is None:
            continue
        if f.get("pii"):
            secrets[f["name"]] = pii.encrypt(v)
        else:
            attrs[f["name"]] = v
    # Defaults: a later row only ever overwrites with non-null values, so filling nulls
    # now is equivalent to filling them once after all merging is done.
    for f in fields:
        if "default" in f and not f.get("pii") and attrs[f["name"]] is None:
            attrs[f["name"]] = f["default"]
    ref_name = next(f["name"] for f in fields if f.get("reference"))
    external_ref = attrs[ref_name]
    if row:
        conn.execute(
            "UPDATE leads SET external_ref = ?, attributes = ?, pii = ? WHERE id = ?",
            (external_ref, json.dumps(attrs), json.dumps(secrets), row[0]),
        )
        return True
    conn.execute(
        "INSERT INTO leads (workflow_id, phone_hash, phone_last4, external_ref, batch_id,"
        " attributes, pii) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (wf, phone_hash, phone[-4:], external_ref, batch_id, json.dumps(attrs), json.dumps(secrets)),
    )
    return False


def _ingest_file(conn, pii: Pii, schema: dict, path: Path) -> tuple[int, int, int, int]:
    wf = schema["workflow_id"]
    batch_id = path.stem
    total = rejected = merged = warnings = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return 0, 0, 0, 0
        cols = _map_headers(header, schema["fields"])
        for row in reader:
            if _is_blank(row):
                continue
            total += 1
            values, reason, w = _validate_row(row, schema["fields"], cols)
            if reason:
                rejected += 1
                conn.execute(
                    "INSERT INTO lead_rejections (workflow_id, batch_id, row_number, reason)"
                    " VALUES (?, ?, ?, ?)",
                    (wf, batch_id, total, reason),
                )
                continue
            warnings += w
            merged += _merge(conn, pii, schema, batch_id, values)
    return total, rejected, merged, warnings


def ingest_leads(conn: sqlite3.Connection, pii: Pii, drop: Path, schemas_dir: Path = SCHEMAS_DIR):
    """Ingest every lead file of one drop. Caller owns the transaction. Yields summary lines."""
    leads_dir = Path(drop) / "leads"
    if not leads_dir.is_dir():
        return
    for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
        schema_path = schemas_dir / f"{wf_dir.name}.json"
        if not schema_path.exists():
            yield f"{wf_dir}: warning, unknown workflow, skipped"
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        for path in sorted(wf_dir.glob("*.csv")):
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            label = f"{wf_dir.name}/{path.name}"
            if conn.execute("SELECT 1 FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone():
                yield f"{label}: skipped (already ingested)"
                continue
            total, rejected, merged, warns = _ingest_file(conn, pii, schema, path)
            conn.execute(
                "INSERT INTO ingested_files (sha256, kind, workflow_id, path, rows_total,"
                " rows_rejected, rows_merged, field_warnings, ingested_at)"
                " VALUES (?, 'leads', ?, ?, ?, ?, ?, ?, ?)",
                (sha, schema["workflow_id"], str(path), total, rejected, merged, warns,
                 datetime.now(timezone.utc).isoformat()),
            )
            yield f"{label}: rows={total} rejected={rejected} merged={merged} warnings={warns}"
