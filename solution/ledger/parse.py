"""Pure value parsers. Each returns the normalised value, or None when invalid or empty."""

import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y")
_CURRENCY_PREFIX = re.compile(r"^(₹|rs\.?|inr)\s*", re.IGNORECASE)
_DECIMAL = re.compile(r"\d+(\.\d+)?", re.ASCII)


def normalize_header(s: str) -> str:
    return re.sub(r"[_\s]+", " ", s.strip().lower())


def parse_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw, flags=re.ASCII)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_decimal(raw: str) -> str | None:
    text = _CURRENCY_PREFIX.sub("", raw.strip()).replace(",", "")
    if not _DECIMAL.fullmatch(text):
        return None
    return str(Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_enum(raw: str, values: list[str]) -> str | None:
    value = raw.strip().lower()
    return value if value in {v.lower() for v in values} else None


def parse_timestamp(raw: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return None
    if ts.tzinfo is None:  # naive means IST
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(IST)
