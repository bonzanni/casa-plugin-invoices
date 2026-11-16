"""Validation and normalization of company, customer and line items; locale
formatting of money and dates; dates and billing periods.

Every ValidationError message is shown to the operator: it names the field and
says what is wrong.
"""
from __future__ import annotations

import copy
import datetime
import re
import zoneinfo

import money


class ValidationError(ValueError):
    """Bad input from the operator; the message is user-facing."""


COMPANY_DEFAULTS = {
    "vat_label": "VAT ID",
    "registration_label": "Reg. no.",
    "tax_name": "VAT",
    "currency": "EUR",
    "locale": "nl-NL",
    "timezone": "Europe/Amsterdam",
    "payment_terms_days": 30,
    "default_vat_rate": "21",
    "numbering": {"pattern": "INV-{YYYY}-{seq:4}", "next_seq": 1, "seq_year": None},
}
SUPPORTED_LOCALES = ("nl-NL", "de-DE", "en-GB", "en-US")
MAX_LINE_ITEMS = 50
MAX_QTY = 10000
MAX_DESCRIPTION = 200          # characters; keeps one item row shorter than a PDF page
MAX_DETAIL = 1000
MAX_TAX_NAME = 30              # characters; the totals block must fit on one page
MAX_PAYMENT_TERMS_DAYS = 365

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_CUSTOMER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
_ITEM_ID = re.compile(r"[A-Za-z0-9-]{1,20}\Z")
_EMAIL = re.compile(r"[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\".]+\Z")
_QTY = re.compile(r"([0-9]+)(?:\.([0-9]{1,6}))?\Z")
_PERIOD = re.compile(r"([0-9]{4})-([0-9]{2})\Z")
_PERIOD_NAME = re.compile(r"([A-Za-z]+)\s+([0-9]{4})\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_ITEM_FIELDS = {"id", "description", "detail", "qty", "unit_price"}

# Text fields that end up on the PDF (checked against cp1252).
_COMPANY_TEXT = ("name", "country", "vat_id", "registration_id", "vat_label",
                 "registration_label", "email", "iban", "bic", "bank", "tax_name")
_COMPANY_REQUIRED = ("name", "address_lines", "country", "iban", "bank")
_COMPANY_FIELDS = set(_COMPANY_TEXT) | {
    "address_lines", "payment_terms_days", "default_vat_rate", "currency",
    "locale", "timezone", "numbering"}
_CUSTOMER_TEXT = ("name", "country", "vat_id", "registration_id", "vat_note")
_CUSTOMER_REQUIRED = ("id", "name", "address_lines", "country")
_CUSTOMER_FIELDS = set(_CUSTOMER_TEXT) | {
    "id", "address_lines", "email", "payment_terms_days", "vat_rate",
    "email_template", "language"}


# ---------------------------------------------------------------- text

def check_text(value: str, field: str) -> str:
    """`value` if every character prints on the invoice (cp1252), else ValidationError."""
    if not isinstance(value, str):
        raise ValidationError(f"{field}: must be text")
    for ch in value:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValidationError(
                f"{field}: control character {ch!r} is not allowed (single line text only)")
    try:
        value.encode("cp1252")
    except UnicodeEncodeError as e:
        ch = value[e.start]
        raise ValidationError(
            f"{field}: character {ch!r} cannot be printed on the invoice") from None
    return value


def _text(value, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field}: must be text")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{field}: is required")
    return check_text(value, field)


def _lines(value, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field}: must be a non-empty list of lines")
    out = [_text(v, f"{field}[{i}]") for i, v in enumerate(value)]
    if not all(out):
        raise ValidationError(f"{field}: lines must not be empty")
    return out


def _email(value, field: str) -> str:
    if not isinstance(value, str) or not _EMAIL.match(value.strip()):
        raise ValidationError(f"{field}: {value!r} is not an email address")
    return value.strip()


def _terms(value, field: str) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= MAX_PAYMENT_TERMS_DAYS):
        raise ValidationError(
            f"{field}: must be a whole number of days, 0 to {MAX_PAYMENT_TERMS_DAYS}")
    return value


def _rate(value, field: str) -> str:
    try:
        return canonical_rate(value)
    except ValidationError as e:
        raise ValidationError(f"{field}: {e}") from None


# ---------------------------------------------------------------- merges

def _apply(current: dict | None, patch: dict, base: dict, allowed: set, kind: str) -> dict:
    if not isinstance(patch, dict):
        raise ValidationError(f"{kind}: expected an object of fields")
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise ValidationError(f"{kind}: unknown field(s) {', '.join(unknown)}")
    out = copy.deepcopy(current) if current is not None else copy.deepcopy(base)
    for key, val in patch.items():
        if val is None:
            out.pop(key, None)           # clears an optional field
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **copy.deepcopy(val)}
        else:
            out[key] = copy.deepcopy(val)
    return out


def _require(rec: dict, fields, kind: str) -> None:
    for f in fields:
        v = rec.get(f)
        if v is None or v == "" or v == []:
            raise ValidationError(f"{kind} {f}: is required")


def merge_company(current: dict | None, patch: dict) -> dict:
    """`current` (or the defaults) updated by `patch`, then validated."""
    c = _apply(current, patch, COMPANY_DEFAULTS, _COMPANY_FIELDS, "company")
    # Fields the defaults supply are re-filled if a patch cleared them.
    for k, v in COMPANY_DEFAULTS.items():
        c.setdefault(k, copy.deepcopy(v))
    return validate_company(c)


def validate_company(c: dict) -> dict:
    _require(c, _COMPANY_REQUIRED, "company")
    for f in _COMPANY_TEXT:
        if f in c:
            c[f] = _text(c[f], f, required=f in _COMPANY_REQUIRED)
    if len(c.get("tax_name") or "") > MAX_TAX_NAME:
        raise ValidationError(f"tax_name: at most {MAX_TAX_NAME} characters, e.g. VAT, BTW, MwSt")
    c["address_lines"] = _lines(c["address_lines"], "address_lines")
    if c.get("email"):
        c["email"] = _email(c["email"], "email")
    c["payment_terms_days"] = _terms(c["payment_terms_days"], "payment_terms_days")
    c["default_vat_rate"] = _rate(c["default_vat_rate"], "default_vat_rate")
    try:
        c["currency"] = c["currency"].upper() if isinstance(c["currency"], str) else c["currency"]
        money.exponent(c["currency"])
    except money.MoneyError:
        raise ValidationError(f"currency: {c['currency']!r} is not a 3-letter currency code") from None
    _check_locale(c["locale"])
    _zone(c["timezone"])
    n = c["numbering"]
    if not isinstance(n, dict) or set(n) != {"pattern", "next_seq", "seq_year"}:
        raise ValidationError("numbering: needs pattern, next_seq and seq_year")
    if not isinstance(n["pattern"], str) or not n["pattern"].strip():
        raise ValidationError("numbering.pattern: must be non-empty text")
    check_text(n["pattern"], "numbering.pattern")
    if isinstance(n["next_seq"], bool) or not isinstance(n["next_seq"], int) or n["next_seq"] < 1:
        raise ValidationError("numbering.next_seq: must be a whole number from 1")
    if n["seq_year"] is not None and (isinstance(n["seq_year"], bool)
                                      or not isinstance(n["seq_year"], int)):
        raise ValidationError("numbering.seq_year: must be a year or null")
    return c


def merge_customer(current: dict | None, patch: dict) -> dict:
    """`current` (or a new customer) updated by `patch`, then validated."""
    if current is not None and "id" in patch and patch["id"] != current.get("id"):
        raise ValidationError("customer id: cannot be changed")
    c = _apply(current, patch, {}, _CUSTOMER_FIELDS, "customer")
    return validate_customer(c)


def validate_customer(c: dict) -> dict:
    cid = c.get("id")
    if not isinstance(cid, str) or not _CUSTOMER_ID.match(cid):
        raise ValidationError(
            "customer id: use 1-40 lowercase letters, digits or '-', starting with a letter or digit")
    _require(c, _CUSTOMER_REQUIRED, "customer")
    for f in _CUSTOMER_TEXT:
        if f in c:
            c[f] = _text(c[f], f, required=f in _CUSTOMER_REQUIRED)
    c["address_lines"] = _lines(c["address_lines"], "address_lines")
    email = c.get("email")
    if not isinstance(email, dict):
        raise ValidationError("email.to: at least one address is required")
    unknown = sorted(set(email) - {"to", "cc", "bcc"})
    if unknown:
        raise ValidationError(f"email: unknown field(s) {', '.join(unknown)}")
    out = {}
    for k in ("to", "cc", "bcc"):
        lst = email.get(k) or []
        if isinstance(lst, str):
            lst = [lst]
        if not isinstance(lst, list):
            raise ValidationError(f"email.{k}: must be a list of addresses")
        out[k] = [_email(a, f"email.{k}") for a in lst]
    if not out["to"]:
        raise ValidationError("email.to: at least one address is required")
    c["email"] = out
    if "payment_terms_days" in c:
        c["payment_terms_days"] = _terms(c["payment_terms_days"], "payment_terms_days")
    if "vat_rate" in c:
        c["vat_rate"] = _rate(c["vat_rate"], "vat_rate")
    for f in ("email_template", "language"):
        if f in c and not isinstance(c[f], str):
            raise ValidationError(f"{f}: must be text")
    return c


# ---------------------------------------------------------------- items & rates

def normalize_item(item: dict, existing_ids: set[str], currency: str) -> dict:
    """One line item as `{id, description, detail, qty, unit_price}`.

    `existing_ids` are the ids of the other lines on the draft.
    """
    if not isinstance(item, dict):
        raise ValidationError("line item: expected an object")
    unknown = sorted(set(item) - _ITEM_FIELDS)
    if unknown:
        raise ValidationError(f"line item: unknown field(s) {', '.join(unknown)}")
    if len(existing_ids) >= MAX_LINE_ITEMS:
        raise ValidationError(f"line items: at most {MAX_LINE_ITEMS} per invoice")
    iid = item.get("id")
    if iid is None:
        n = 1
        while f"L{n}" in existing_ids:
            n += 1
        iid = f"L{n}"
    elif not isinstance(iid, str) or not _ITEM_ID.match(iid):
        raise ValidationError(f"line item id: {iid!r} is not a valid id")
    elif iid in existing_ids:
        raise ValidationError(f"line item id: {iid} is already used")
    description = _text(item.get("description", ""), "description", required=True)
    detail = _text(item.get("detail") or "", "detail")
    for field, value, limit in (("description", description, MAX_DESCRIPTION),
                                ("detail", detail, MAX_DETAIL)):
        if len(value) > limit:
            raise ValidationError(f"{field}: at most {limit} characters (this one has {len(value)})")

    qty = item.get("qty")
    if isinstance(qty, int) and not isinstance(qty, bool):
        qty = str(qty)
    m = _QTY.match(qty.strip()) if isinstance(qty, str) else None
    if m is None:
        raise ValidationError(
            f"qty: {item.get('qty')!r} must be a number like \"2\" or \"1.5\" (at most 6 decimals)")
    whole, frac = m.group(1).lstrip("0") or "0", (m.group(2) or "").rstrip("0")
    millionths = int(whole) * 10**6 + int(frac.ljust(6, "0") or "0")
    if not 0 < millionths <= MAX_QTY * 10**6:
        raise ValidationError(f"qty: must be more than 0 and at most {MAX_QTY}")
    qty = whole + (f".{frac}" if frac else "")

    price = item.get("unit_price")
    try:
        unit_price = money.format_minor(money.to_minor(price, currency), currency)
    except money.MoneyError:
        raise ValidationError(
            f"unit_price: {price!r} must be a plain amount like \"13000.00\" "
            "(dot for decimals, no thousands separators)") from None
    return {"id": iid, "description": description, "detail": detail,
            "qty": qty, "unit_price": unit_price}


def rate_bp(rate: str) -> int:
    """A percent string ("21", "12.5") as basis points (2100, 1250), 0..10000."""
    try:
        bp = money.to_minor(rate.strip() if isinstance(rate, str) else rate, "EUR")
    except money.MoneyError:
        raise ValidationError(
            f"VAT rate {rate!r} must be a percentage like \"21\" or \"12.5\"") from None
    if not 0 <= bp <= 10000:
        raise ValidationError(f"VAT rate {rate!r} must be between 0 and 100")
    return bp


def canonical_rate(rate: str) -> str:
    """The rate's canonical percent string: "+021.0" -> "21", 1250 bp -> "12.5"."""
    bp = rate_bp(rate)
    return f"{bp // 100}.{bp % 100:02d}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- formatting

_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}


def _check_locale(locale: str) -> None:
    if locale not in SUPPORTED_LOCALES:
        raise ValidationError(
            f"locale: {locale!r} is not supported (use {', '.join(SUPPORTED_LOCALES)})")


def decimal_sep(locale: str) -> str:
    """The locale's decimal separator ("," for nl-NL/de-DE, "." otherwise)."""
    _check_locale(locale)
    return "," if locale in ("nl-NL", "de-DE") else "."


def format_money(minor: int, currency: str, locale: str) -> str:
    point = decimal_sep(locale)
    european = point == ","
    group = "." if european else ","
    exp = money.exponent(currency)
    digits = str(abs(minor)).rjust(exp + 1, "0")
    whole, frac = (digits[:-exp], digits[-exp:]) if exp else (digits, "")
    whole = f"{int(whole):,}".replace(",", group)
    number = whole + (point + frac if frac else "")
    code = currency.upper()
    symbol = _SYMBOLS.get(code)
    if symbol is None:
        prefix = code + " "
    else:
        prefix = symbol + (" " if european else "")
    return ("-" if minor < 0 else "") + prefix + number


def parse_date(iso: str, field: str = "date") -> datetime.date:
    """A strict YYYY-MM-DD date, else ValidationError naming `field`."""
    try:
        if not isinstance(iso, str) or not _DATE.match(iso):
            raise ValueError
        return datetime.date.fromisoformat(iso)
    except ValueError:
        raise ValidationError(f"{field}: {iso!r} is not a date like 2026-11-15") from None


def format_date(iso: str, locale: str) -> str:
    _check_locale(locale)
    d = parse_date(iso)
    if locale in ("nl-NL", "de-DE"):
        return f"{d.day:02d}-{d.month:02d}-{d.year}"
    if locale == "en-GB":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


# ---------------------------------------------------------------- dates & periods

def _zone(tz: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError(f"timezone: {tz!r} is not a known time zone") from None


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def today(tz: str) -> str:
    """Today's ISO date in time zone `tz`."""
    return _utcnow().astimezone(_zone(tz)).date().isoformat()


def add_days(iso: str, n: int) -> str:
    return (parse_date(iso) + datetime.timedelta(days=n)).isoformat()


def _period(period: str) -> tuple[int, int]:
    m = _PERIOD.match(period) if isinstance(period, str) else None
    if m is None or not 1 <= int(m.group(2)) <= 12:
        raise ValidationError(f"period: {period!r} must look like 2026-11 or \"November 2026\"")
    return int(m.group(1)), int(m.group(2))


def period_label(period: str) -> str:
    y, mo = _period(period)
    return f"{_MONTHS[mo - 1]} {y}"


def next_period(period: str) -> str:
    y, mo = _period(period)
    y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return f"{y:04d}-{mo:02d}"


def parse_period(text: str) -> str:
    """"2026-11" or "November 2026" (any case) as "2026-11"."""
    s = text.strip() if isinstance(text, str) else text
    m = _PERIOD_NAME.match(s) if isinstance(s, str) else None
    if m:
        names = [n.lower() for n in _MONTHS]
        if m.group(1).lower() in names:
            return f"{m.group(2)}-{names.index(m.group(1).lower()) + 1:02d}"
    y, mo = _period(s)
    return f"{y:04d}-{mo:02d}"
