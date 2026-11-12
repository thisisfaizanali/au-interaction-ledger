import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from ledger import db
from ledger.__main__ import main
from ledger.leads import ingest_leads
from ledger.pii import Pii

WF = "motor_renewal"
HEADER = "Name,Mobile,Policy No,Expiry,Premium,Language\n"


@pytest.fixture
def pii():
    return Pii(Fernet.generate_key())


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.sqlite"))
    yield c
    c.close()


def make_drop(root, name, files, wf=WF):
    d = root / name / "leads" / wf
    d.mkdir(parents=True)
    for fname, text in files.items():
        (d / fname).write_text(text, encoding="utf-8")
    return root / name


def run(conn, pii, *drops):
    out = []
    for d in drops:
        out += list(ingest_leads(conn, pii, d))
        conn.commit()
    return out


def lead(conn, pii, phone):
    row = conn.execute(
        "SELECT batch_id, external_ref, attributes, pii FROM leads WHERE phone_hash = ?",
        (pii.phone_hash(phone),),
    ).fetchone()
    return row[0], row[1], json.loads(row[2]), {k: v and pii.decrypt(v) for k, v in json.loads(row[3]).items()}


def test_header_aliases_spacing_and_extra_column(tmp_path, conn, pii):
    csv = "  customer__name ,MOBILE NO,policy_no,expiry,agent notes,Premium\nAsha,98765 43210,P1,01/12/2026,x,\"1,500\"\n"
    run(conn, pii, make_drop(tmp_path, "d", {"batch_001.csv": csv}))
    batch, ref, attrs, secrets = lead(conn, pii, "+919876543210")
    assert (batch, ref) == ("batch_001", "P1")
    assert attrs["policy_expiry"] == "2026-12-01"
    assert attrs["premium_amount"] == "1500.00"
    assert secrets == {"customer_name": "Asha", "phone": "+919876543210"}


def test_blank_rows_skipped_and_row_numbers_count_non_blank(tmp_path, conn, pii):
    csv = HEADER + "Asha,9876543210,P1,,,\n,,,,,\n  , ,,,,\nBob,123,P2,,,\n"
    out = run(conn, pii, make_drop(tmp_path, "d", {"batch_001.csv": csv}))
    assert "rows=2 rejected=1" in out[0]
    assert conn.execute("SELECT row_number, reason FROM lead_rejections").fetchall() == [(2, "invalid_phone")]


def test_rejection_order_follows_schema(tmp_path, conn, pii):
    csv = HEADER + ",12345,P1,,,\nAsha,12345,P2,,,\n"
    run(conn, pii, make_drop(tmp_path, "d", {"batch_001.csv": csv}))
    assert conn.execute("SELECT row_number, reason FROM lead_rejections ORDER BY 1").fetchall() == [
        (1, "missing_required:customer_name"), (2, "invalid_phone")]


def test_optional_invalid_warns_but_rejected_row_does_not(tmp_path, conn, pii):
    csv = HEADER + "Asha,9876543210,P1,31/02/2026,,\nBob,9876543211,P2,not a date,,\nCy,123,P3,bad,,\n"
    out = run(conn, pii, make_drop(tmp_path, "d", {"batch_001.csv": csv}))
    assert "warnings=2" in out[0]  # Asha and Bob; rejected Cy adds none
    assert lead(conn, pii, "+919876543210")[2]["policy_expiry"] is None


def test_duplicate_merge_across_files(tmp_path, conn, pii):
    a = HEADER + "Asha,9876543210,P1,01/12/2026,100,hi\n"
    b = HEADER + "Asha B,+91 98765 43210,P9,,200,\n"
    out = run(conn, pii, make_drop(tmp_path, "d", {"batch_001.csv": a, "batch_002.csv": b}))
    assert "merged=1" in out[1]
    batch, ref, attrs, secrets = lead(conn, pii, "+919876543210")
    assert batch == "batch_001"
    assert ref == "P9"
    assert attrs["policy_expiry"] == "2026-12-01"  # later null does not overwrite
    assert attrs["premium_amount"] == "200.00"  # later non-null does
    assert attrs["preferred_language"] == "hi"
    assert secrets["customer_name"] == "Asha B"
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_default_language_applied_and_overwritten(tmp_path, conn, pii):
    a = HEADER + "Asha,9876543210,P1,,,\nBob,9876543211,P2,,,Hindi\n"
    b = HEADER + "Asha,9876543210,P1,,,hi\n"
    run(conn, pii, make_drop(tmp_path, "d", {"batch_001.csv": a}))
    assert lead(conn, pii, "+919876543210")[2]["preferred_language"] == "en"
    assert lead(conn, pii, "+919876543211")[2]["preferred_language"] == "en"  # name is not a code
    run(conn, pii, make_drop(tmp_path, "e", {"batch_002.csv": b}))
    assert lead(conn, pii, "+919876543210")[2]["preferred_language"] == "hi"


def snapshot(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            for t in ("ingested_files", "leads", "lead_rejections")}


def test_reingest_changes_nothing(tmp_path, conn, pii):
    d = make_drop(tmp_path, "d", {"batch_001.csv": HEADER + "Asha,9876543210,P1,,,\nBob,1,P2,,,\n"})
    run(conn, pii, d)
    before = snapshot(conn)
    out = run(conn, pii, d)
    assert all("skipped (already ingested)" in line for line in out)
    assert snapshot(conn) == before


def leads_view(conn, pii):
    rows = conn.execute("SELECT workflow_id, phone_hash, batch_id, external_ref, attributes, pii"
                        " FROM leads ORDER BY 1, 2").fetchall()
    return [(*r[:4], json.loads(r[4]),
             {k: v and pii.decrypt(v) for k, v in json.loads(r[5]).items()}) for r in rows]


def test_separate_drops_equal_combined(tmp_path, pii):
    a = make_drop(tmp_path, "a", {"batch_001.csv": HEADER + "Asha,9876543210,P1,01/12/2026,,hi\nBob,9876543211,P2,,,\n"})
    b = make_drop(tmp_path, "b", {"batch_002.csv": HEADER + "Asha,9876543210,P1,,300,\nCy,9876543212,P3,,,\n"})
    views = []
    for name, groups in (("sep", [[a], [b]]), ("one", [[a, b]])):
        c = db.connect(str(tmp_path / f"{name}.sqlite"))
        for g in groups:
            run(c, pii, *g)
        views.append(leads_view(c, pii))
        c.close()
    assert views[0] == views[1]
    assert len(views[0]) == 3


def test_pii_not_in_database_bytes(tmp_path):
    path = tmp_path / "pii.sqlite"
    csv = HEADER + "Zaphod Beeblebrox,9876501234,P1,,,\n"
    make_drop(tmp_path, "d", {"batch_001.csv": csv})
    assert main(["--db", str(path), "ingest", str(tmp_path / "d")]) == 0
    raw = path.read_bytes()
    assert b"9876501234" not in raw
    assert b"Zaphod" not in raw
    key = path.with_name("pii.sqlite.key")
    assert key.exists()
    c = sqlite3.connect(path)
    assert c.execute("SELECT phone_last4 FROM leads").fetchone() == ("1234",)
    c.close()
