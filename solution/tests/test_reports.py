import csv
import json

import pytest
from cryptography.fernet import Fernet

from ledger import db
from ledger.events import ingest_events, load_dispositions
from ledger.leads import ingest_leads
from ledger.pii import Pii
from ledger.reports import write_reports

WF = "motor_renewal"


def ev(event_id, event_type, call_id, ts, phone=None, **payload):
    d = {"event_id": event_id, "event_type": event_type, "workflow_id": WF, "call_id": call_id,
         "occurred_at": ts, "payload": payload}
    if phone:
        d["phone"] = "+91" + phone
    return json.dumps(d)


def call(n, phone, init, connected=None, failed=None, sets=()):
    """Events of one call; `sets` is a list of (time, code)."""
    c = f"call_{n}"
    out = [ev(f"i{n}", "call.initiated", c, init, phone)]
    if connected:
        out.append(ev(f"c{n}", "call.connected", c, connected, phone))
    if failed:
        out.append(ev(f"f{n}", "call.telephony_failed", c, failed, phone, reason="x"))
    for k, (ts, code) in enumerate(sets):
        out.append(ev(f"s{n}_{k}", "disposition.set", c, ts, phone, disposition_code=code))
    return out


def override(event_id, call_id, ts, code):
    return ev(event_id, "disposition.override", call_id, ts, disposition_code=code,
              overridden_by="sup", reason="fix")


# phone suffix 000N belongs to policy PN
LEADS = (
    "customer_name,phone,policy_number\n"
    + "".join(f"Lead {n},900000000{n},P{n}\n" for n in range(1, 8))
    + ",9000000008,P8\n"        # missing_required:customer_name (listed first on purpose)
    + "Bad,12345,P9\n"          # invalid_phone
    + "Lead 1 again,9000000001,P1\n"  # merged duplicate
)

EVENTS = [
    # P1: its only call failed
    *call(1, "9000000001", "2026-10-05T10:00:00+05:30", failed="2026-10-05T10:00:05+05:30"),
    # P2: one failed call and one answered call (naive timestamp = IST)
    *call(2, "9000000002", "2026-10-05T10:10:00+05:30", failed="2026-10-05T10:10:05+05:30"),
    *call(3, "9000000002", "2026-10-05 10:20:00", connected="2026-10-05T10:20:10+05:30"),
    # P3: 20:00Z is 01:30 IST the next day; terminal set; a later call has no disposition
    *call(4, "9000000003", "2026-10-05T20:00:00Z", connected="2026-10-05T20:00:10Z",
          sets=[("2026-10-05T20:05:00Z", "not_interested")]),
    *call(5, "9000000003", "2026-10-06T12:00:00+05:30"),
    # P4: terminal and successful set, then a supervisor override to a non-terminal code
    *call(6, "9000000004", "2026-10-06T09:00:00+05:30", connected="2026-10-06T09:00:10+05:30",
          sets=[("2026-10-06T09:05:00+05:30", "renewed")]),
    override("o6", "call_6", "2026-10-06T09:30:00+05:30", "callback_requested"),
    # P5: two overrides; the later one is listed first in the file
    *call(7, "9000000005", "2026-10-06T10:00:00+05:30",
          sets=[("2026-10-06T10:05:00+05:30", "callback_requested")]),
    override("o7b", "call_7", "2026-10-06T12:00:00+05:30", "payment_link_sent"),
    override("o7a", "call_7", "2026-10-06T11:00:00+05:30", "renewed"),
    # P6: two successful calls on the same date
    *call(8, "9000000006", "2026-10-06T11:00:00+05:30", connected="2026-10-06T11:00:10+05:30",
          sets=[("2026-10-06T11:05:00+05:30", "renewed")]),
    *call(9, "9000000006", "2026-10-06T13:00:00+05:30",
          sets=[("2026-10-06T13:05:00+05:30", "renewed")]),
    # P7 has no call.initiated: these events must not count anywhere
    ev("x1", "call.connected", "call_10", "2026-10-06T14:00:00+05:30", "9000000007"),
    ev("x2", "disposition.set", "call_10", "2026-10-06T14:05:00+05:30", "9000000007",
       disposition_code="renewed"),
    override("x3", "call_10", "2026-10-06T14:10:00+05:30", "renewed"),
]


@pytest.fixture
def reports(tmp_path):
    drop = tmp_path / "drop"
    (drop / "leads" / WF).mkdir(parents=True)
    (drop / "leads" / WF / "batch_001.csv").write_text(LEADS, encoding="utf-8")
    (drop / "events").mkdir()
    (drop / "events" / "events_1.jsonl").write_text("\n".join(EVENTS) + "\n", encoding="utf-8")
    conn = db.connect(str(tmp_path / "t.sqlite"))
    pii = Pii(Fernet.generate_key())
    load_dispositions(conn)
    list(ingest_leads(conn, pii, drop))
    list(ingest_events(conn, pii, drop))
    conn.commit()
    out = tmp_path / "out"
    write_reports(conn, out)

    class R:
        pass

    r = R()
    r.conn, r.out = conn, out
    yield r
    conn.close()


def rows(r, name):
    with open(r.out / name, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def lead_rows(r):
    return {x["external_ref"]: x for x in rows(r, "lead_report.csv")}


def test_all_four_buckets(reports):
    got = {ref: x["bucket"] for ref, x in lead_rows(reports).items()}
    assert got == {
        "P1": "in_progress_telephony_failed",  # only call failed
        "P2": "in_progress_dialed",            # one failed call, one not
        "P3": "completed",                     # terminal effective disposition
        "P4": "in_progress_dialed",            # terminal set overridden to non-terminal
        "P5": "in_progress_dialed",
        "P6": "completed",
        "P7": "yet_to_reach_out",              # only events of a call with no call.initiated
    }
    [b] = rows(reports, "batch_summary.csv")
    assert b == {"workflow_id": WF, "batch_id": "batch_001", "total_leads": "7",
                 "yet_to_reach_out": "1", "in_progress_telephony_failed": "1",
                 "in_progress_dialed": "3", "completed": "2"}


def test_override_beats_set_and_latest_override_wins(reports):
    leads = lead_rows(reports)
    assert leads["P4"]["current_disposition"] == "callback_requested"
    assert leads["P4"]["ever_success"] == "false"
    # o7b is first in the file but later by occurred_at; both beat the set
    assert leads["P5"]["current_disposition"] == "payment_link_sent"
    assert leads["P5"]["ever_success"] == "false"


def test_override_leaves_original_event_in_ledger(reports):
    conn = reports.conn
    assert conn.execute("SELECT disposition_code FROM events WHERE event_id = 's6_0'").fetchone() == ("renewed",)
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'disposition.override'").fetchone() == (4,)


def test_events_of_call_without_initiated_are_ignored(reports):
    p7 = lead_rows(reports)["P7"]
    assert (p7["total_calls"], p7["connected_calls"], p7["last_call_at"], p7["current_disposition"],
            p7["ever_success"]) == ("0", "0", "", "", "false")
    # 3 calls on 10-05 and 6 on 10-06: call_10 is in neither
    assert [(d["date"], d["total_calls"]) for d in rows(reports, "daily_overview.csv")] == [
        ("2026-10-05", "3"), ("2026-10-06", "6")]


def test_utc_call_lands_on_ist_date(reports):
    assert "2026-10-06" in {d["date"] for d in rows(reports, "daily_overview.csv")}
    assert lead_rows(reports)["P3"]["total_calls"] == "2"


def test_daily_overview_rate_and_distinct_success_leads(reports):
    d5, d6 = rows(reports, "daily_overview.csv")
    assert d5 == {"workflow_id": WF, "date": "2026-10-05", "total_calls": "3",
                  "connected_calls": "1", "connectivity_rate": "0.3333", "success_leads": "0"}
    # P6 has two successful calls on 10-06 and counts once; P4's success was overridden away
    assert d6["connected_calls"] == "3" and d6["connectivity_rate"] == "0.5000"
    assert d6["success_leads"] == "1"


def test_lead_report_fields(reports):
    leads = lead_rows(reports)
    assert list(leads) == ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]
    p3 = leads["P3"]
    assert p3["phone_masked"] == "+91******0003"
    assert p3["batch_id"] == "batch_001"
    # the later call has no disposition and must not blank the earlier one
    assert (p3["total_calls"], p3["connected_calls"], p3["last_call_at"], p3["current_disposition"],
            p3["ever_success"]) == ("2", "1", "2026-10-06T12:00:00+05:30", "not_interested", "false")
    assert leads["P2"]["last_call_at"] == "2026-10-05T10:20:00+05:30"
    p6 = leads["P6"]
    assert (p6["current_disposition"], p6["ever_success"], p6["connected_calls"]) == ("renewed", "true", "1")


def test_rejections_csv(reports):
    assert rows(reports, "rejections.csv") == [
        {"workflow_id": WF, "batch_id": "batch_001", "row_number": "8", "reason": "missing_required:customer_name"},
        {"workflow_id": WF, "batch_id": "batch_001", "row_number": "9", "reason": "invalid_phone"},
    ]


def test_data_quality_key_order_and_sorted_reasons(reports):
    text = (reports.out / "data_quality.json").read_text(encoding="utf-8")
    assert text.endswith("}\n")
    dq = json.loads(text)
    assert list(dq) == [
        "lead_rows_total", "lead_rows_rejected", "rejections_by_reason", "duplicate_lead_rows_merged",
        "field_warnings", "events_total", "malformed_events", "duplicate_events_dropped",
        "orphan_events", "unknown_disposition_events"]
    assert list(dq["rejections_by_reason"]) == ["invalid_phone", "missing_required:customer_name"]
    assert dq["lead_rows_total"] == 10 and dq["lead_rows_rejected"] == 2
    assert dq["duplicate_lead_rows_merged"] == 1
    assert dq["events_total"] == len(EVENTS) and dq["orphan_events"] == 0
