import pytest

from ledger.parse import (
    normalize_header,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_phone,
    parse_timestamp,
)


@pytest.mark.parametrize("raw, expected", [
    ("  Mobile_No ", "mobile no"),
    ("Customer__Name", "customer name"),
    ("Policy \t No", "policy no"),
])
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


@pytest.mark.parametrize("raw", [
    "9876543210",
    "+91 98765 43210",
    "91-98765-43210",
    "098765 43210",
    "(+91) 98765-43210",
    "919876543210",
])
def test_phone_valid(raw):
    assert parse_phone(raw) == "+919876543210"


def test_phone_ten_digits_starting_with_91_is_kept():
    assert parse_phone("9123456789") == "+919123456789"


@pytest.mark.parametrize("raw", [
    "5876543210", "98765", "9198765432101", "00919876543210", "05876543210", "", "abc",
])
def test_phone_invalid(raw):
    assert parse_phone(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),
    ("29/02/2028", "2028-02-29"),
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "29/02/2026", "12/31/2026", "2026/12/01", "abc", "",
])
def test_date_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("₹1,234.50", "1234.50"),
    ("Rs. 999", "999.00"),
    ("rs500", "500.00"),
    ("INR 1,00,000", "100000.00"),
    ("Rs 12.345", "12.35"),
    ("12.344", "12.34"),
    ("0", "0.00"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["-5", "abc", "1e5", "Rs.", ""])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum():
    assert parse_enum(" HI ", ["en", "hi"]) == "hi"
    assert parse_enum("fr", ["en", "hi"]) is None
    assert parse_enum("", ["en", "hi"]) is None


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-05T13:20:21Z", "2026-10-05T18:50:21+05:30"),
    ("2026-10-05T20:00:00Z", "2026-10-06T01:30:00+05:30"),
    ("2026-10-03T10:01:00+05:30", "2026-10-03T10:01:00+05:30"),
    ("2026-10-03 10:01:00", "2026-10-03T10:01:00+05:30"),
])
def test_timestamp_valid(raw, expected):
    assert parse_timestamp(raw).isoformat() == expected


def test_timestamp_crosses_midnight_in_ist():
    assert parse_timestamp("2026-10-05T20:00:00Z").date().isoformat() == "2026-10-06"


@pytest.mark.parametrize("raw", ["garbage", ""])
def test_timestamp_invalid(raw):
    assert parse_timestamp(raw) is None
