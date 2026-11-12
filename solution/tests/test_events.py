import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from ledger import db
from ledger.events import event_quality, ingest_events, load_dispositions
from ledger.leads import ingest_leads
from ledger.pii import Pii

WF = "motor_renewal"
PHONE = "98765 43210"
LEAD_CSV = "Name,Mobile,Policy No\nAsha,98765 43210,P1\n"


@pytest.fixture
def pii():
    return Pii(Fernet.generate_key())


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.sqlite"))
    cat = tmp_path / "dispositions.csv"
    cat.write_text(
        "workflow_id,code,label,is_terminal,is_success\n"
        f"{WF},renewed,Renewed,true,true\n{WF},callback_requested,Callback,false,false\n",
        encoding="utf-8",
    )
    load_dispositions(c, cat)
    c.commit()
    yield c
    c.close()


def ev(event_id, event_type="call.initiated", call_id="c1", ts="2026-10-05T10:00:00Z",
       phone=PHONE, wf=WF, **payload):
    d = {"event_id": event_id, "event_type": event_type, "workflow_id": wf, "call_id": call_id,
         "occurred_at": ts, "payload": payload}
    if phone:
        d["phone"] = phone
    return json.dumps(d)


def make_drop(root, name, lines=(), lead_csv=None):
    d = root / name
    if lines:
        (d / "events").mkdir(parents=True)
        (d / "events" / "events_1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if lead_csv:
        (d / "leads" / WF).mkdir(parents=True)
        (d / "leads" / WF / "batch_001.csv").write_text(lead_csv, encoding="utf-8")
    return d


def run(conn, pii, *drops):
    out = []
    for d in drops:
        out += list(ingest_leads(conn, pii, d)) + list(ingest_events(conn, pii, d))
        conn.commit()
    return out


def ledger(conn):
    return conn.execute("SELECT event_id, event_type FROM ledger ORDER BY event_id").fetchall()


def test_malformed_lines_counted_blank_lines_ignored(tmp_path, conn, pii):
    lines = [ev("e1"), "{not json", "", "   ", json.dumps({"event_type": "call.initiated"}),
             json.dumps([1, 2]), ev("e2", ts="garbage")]
    out = run(conn, pii, make_drop(tmp_path, "d", lines))
    assert out == ["events_1.jsonl: lines=5 malformed=4 duplicates=0 stored=1"]
    q = event_quality(conn)
    assert (q["events_total"], q["malformed_events"]) == (5, 4)


def test_duplicates_within_file_and_across_drops(tmp_path, conn, pii):
    run(conn, pii, make_drop(tmp_path, "d1", [ev("e1"), ev("e1"), ev("e2")]))
    run(conn, pii, make_drop(tmp_path, "d2", [ev("e2"), ev("e3")]))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    q = event_quality(conn)
    assert (q["events_total"], q["duplicate_events_dropped"]) == (5, 2)


def test_timestamps_stored_as_ist(tmp_path, conn, pii):
    lines = [ev("z", ts="2026-10-05T20:00:00Z"), ev("o", ts="2026-10-05T10:00:00+05:30"),
             ev("n", ts="2026-10-05 10:00:00")]
    run(conn, pii, make_drop(tmp_path, "d", lines))
    got = dict(conn.execute("SELECT event_id, occurred_at FROM events"))
    assert got["z"] == "2026-10-06T01:30:00+05:30"  # late UTC day is the next IST date
    assert got["o"] == got["n"] == "2026-10-05T10:00:00+05:30"


def test_override_links_through_call_id(tmp_path, conn, pii):
    lines = [ev("e1"), ev("e2", "disposition.override", phone=None, disposition_code="renewed")]
    run(conn, pii, make_drop(tmp_path, "d", lines, LEAD_CSV))
    assert ledger(conn) == [("e1", "call.initiated"), ("e2", "disposition.override")]
    assert event_quality(conn)["orphan_events"] == 0


def test_override_without_known_call_is_orphan(tmp_path, conn, pii):
    run(conn, pii, make_drop(tmp_path, "d", [ev("e2", "disposition.override", phone=None,
                                                 disposition_code="renewed")], LEAD_CSV))
    assert ledger(conn) == []
    assert event_quality(conn)["orphan_events"] == 1


def test_orphan_resolves_when_lead_arrives_later(tmp_path, conn, pii):
    run(conn, pii, make_drop(tmp_path, "a", [ev("e1"), ev("e2", "call.connected")]))
    assert event_quality(conn)["orphan_events"] == 2 and ledger(conn) == []
    run(conn, pii, make_drop(tmp_path, "b", lead_csv=LEAD_CSV))
    assert event_quality(conn)["orphan_events"] == 0
    assert len(ledger(conn)) == 2


def test_unknown_disposition_counted_and_left_out_of_ledger(tmp_path, conn, pii):
    lines = [ev("e1"), ev("e2", "disposition.set", disposition_code="nonsense"),
             ev("e3", "disposition.set", disposition_code="renewed"),
             ev("e4", "disposition.set", phone="90000 00000", disposition_code="nonsense")]
    run(conn, pii, make_drop(tmp_path, "d", lines, LEAD_CSV))
    assert [e for e, _ in ledger(conn)] == ["e1", "e3"]
    q = event_quality(conn)
    assert q["unknown_disposition_events"] == 1  # e4 is an orphan, counted only as that
    assert q["orphan_events"] == 1


@pytest.mark.parametrize("sql", ["UPDATE events SET call_id = 'x'", "DELETE FROM events"])
def test_events_are_append_only(tmp_path, conn, pii, sql):
    run(conn, pii, make_drop(tmp_path, "d", [ev("e1")]))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)


def test_reingest_changes_nothing(tmp_path, conn, pii):
    d = make_drop(tmp_path, "d", [ev("e1"), ev("e1"), "oops"], LEAD_CSV)
    run(conn, pii, d)
    before = (conn.execute("SELECT * FROM events").fetchall(), event_quality(conn))
    out = run(conn, pii, d)
    assert all("skipped" in line for line in out)
    assert (conn.execute("SELECT * FROM events").fetchall(), event_quality(conn)) == before


def test_raw_phone_never_reaches_the_database_file(tmp_path, pii):
    path = tmp_path / "p.sqlite"
    c = db.connect(str(path))
    run(c, pii, make_drop(tmp_path, "d", [ev("e1", phone="+91 98765 43210")], LEAD_CSV))
    c.close()
    data = path.read_bytes()
    assert b"9876543210" not in data and b"98765 43210" not in data
